"""Behavioural tests for the node-stats tools.

The tools are registered with FastMCP without rebinding their names, so the
plain callables stay directly invokable here. The focus is the security
envelope: file reads are denied unless a root is allowlisted, and the allowlist
cannot be escaped.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest


def _load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_root: str,
    roots: str,
    pressure_paths: str | None = None,
    max_du_entries: str | None = None,
    max_du_total_entries: str | None = None,
    du_timeout_seconds: str | None = None,
) -> ModuleType:
    """Reimport the server module with ROOTFS + allowlist env applied at import time."""
    monkeypatch.setenv("ROOTFS", tmp_root)
    monkeypatch.setenv("NODE_STATS_READABLE_ROOTS", roots)
    monkeypatch.setenv("NODE_STATS_DISK_WARN_PERCENT", "80")
    monkeypatch.setenv("NODE_STATS_DISK_CRITICAL_PERCENT", "85")
    if pressure_paths is None:
        monkeypatch.delenv("NODE_STATS_PRESSURE_PATHS", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_PRESSURE_PATHS", pressure_paths)
    if max_du_entries is None:
        monkeypatch.delenv("NODE_STATS_MAX_DU_ENTRIES", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_MAX_DU_ENTRIES", max_du_entries)
    if max_du_total_entries is None:
        monkeypatch.delenv("NODE_STATS_MAX_DU_TOTAL_ENTRIES", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_MAX_DU_TOTAL_ENTRIES", max_du_total_entries)
    if du_timeout_seconds is None:
        monkeypatch.delenv("NODE_STATS_DU_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_DU_TIMEOUT_SECONDS", du_timeout_seconds)
    import node_stats_mcp.server as server

    return importlib.reload(server)


def test_cpu_and_memory_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load(monkeypatch, "/", "")
    cpu = server.get_cpu_info()
    assert "percent" in cpu
    assert cpu["logical_cores"] and cpu["logical_cores"] >= 1
    mem = server.get_memory_info()
    assert mem["virtual"]["total"] > 0


def test_file_reads_denied_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    server = _load(monkeypatch, str(tmp_path), "")  # empty allowlist
    with pytest.raises(ValueError, match="disabled"):
        server.stat_path("hello.txt")


def test_allowlisted_read_and_escape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "f.txt").write_text("payload")
    (tmp_path / "secret.txt").write_text("nope")
    server = _load(monkeypatch, str(tmp_path), "/ok")

    got = server.read_text_head("ok/f.txt")
    assert got["text"] == "payload"
    assert got["truncated"] is False

    # A path outside the single allowed root is refused.
    with pytest.raises(ValueError, match="outside"):
        server.stat_path("secret.txt")


def test_read_is_capped(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "big.txt").write_text("x" * 1000)
    server = _load(monkeypatch, str(tmp_path), "/")
    got = server.read_text_head("big.txt", max_bytes=100)
    assert got["bytes_returned"] == 100
    assert got["truncated"] is True


def test_filesystem_pressure_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _load(monkeypatch, str(tmp_path), "")
    got = server.get_filesystem_pressure()["root"]

    assert got["path"] == "/"
    assert got["total_bytes"] > 0
    assert got["available_bytes"] >= 0
    assert got["reserved_bytes"] >= 0
    assert got["pressure_used_bytes"] >= 0
    assert (
        got["bytes_until_critical"] == int(got["total_bytes"] * 0.85) - got["pressure_used_bytes"]
    )
    assert got["bytes_over_critical"] >= 0
    assert got["status"] in {"ok", "warning", "critical"}
    assert got["warn_percent"] == 80.0
    assert got["critical_percent"] == 85.0
    assert got["inodes_total"] >= 0
    assert got["inodes_used_percent"] >= 0.0


def test_pressure_path_usage_uses_fixed_env_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "hot").mkdir()
    (tmp_path / "cold").mkdir()
    (tmp_path / "hot" / "blob.bin").write_bytes(b"x" * 1024 * 1024)
    (tmp_path / "cold" / "tiny.txt").write_text("x")
    server = _load(monkeypatch, str(tmp_path), "", pressure_paths="/hot:/cold:/missing")

    got = asyncio.run(server.get_pressure_path_usage(limit=10, max_entries_per_path=1000))
    by_path = {entry["path"]: entry for entry in got["paths"]}

    assert got["configured_paths"] == ["/hot", "/cold", "/missing"]
    assert got["same_filesystem_only"] is True
    assert by_path["/missing"]["exists"] is False
    assert by_path["/hot"]["exists"] is True
    assert by_path["/hot"]["size_bytes"] > by_path["/cold"]["size_bytes"]
    assert by_path["/hot"]["permission_errors"] == 0
    assert by_path["/hot"]["scan_errors"] == 0
    assert by_path["/hot"]["skipped_different_filesystem"] == 0


def test_pressure_path_usage_caps_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "hot").mkdir()
    for idx in range(10):
        (tmp_path / "hot" / f"{idx}.txt").write_text("x")
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        pressure_paths="/hot",
        max_du_entries="3",
    )

    got = asyncio.run(server.get_pressure_path_usage(limit=1, max_entries_per_path=1000))
    path = got["paths"][0]

    assert got["max_entries_per_path"] == 3
    assert path["path"] == "/hot"
    assert path["entries_scanned"] == 3
    assert path["truncated"] is True
    assert path["truncation_reason"] == "per_path_entry_cap"


def test_pressure_path_usage_caps_total_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("hot", "cold"):
        directory = tmp_path / name
        directory.mkdir()
        for idx in range(5):
            (directory / f"{idx}.txt").write_text("x")
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        pressure_paths="/hot:/cold",
        max_du_entries="100",
        max_du_total_entries="4",
    )

    got = asyncio.run(server.get_pressure_path_usage(limit=10, max_entries_per_path=100))

    assert got["max_total_entries"] == 4
    assert got["total_entries_scanned"] == 4
    assert got["truncated"] is True
    assert any(path["truncation_reason"] == "global_entry_budget" for path in got["paths"])


def test_pressure_path_usage_reports_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "hot"
    directory.mkdir()
    (directory / "slow.txt").write_text("x")
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        pressure_paths="/hot",
        du_timeout_seconds="0.001",
    )
    original_scandir = server.os.scandir

    def slow_scandir(path):
        time.sleep(0.02)
        return original_scandir(path)

    monkeypatch.setattr(server.os, "scandir", slow_scandir)

    got = asyncio.run(server.get_pressure_path_usage(limit=1, max_entries_per_path=100))

    assert got["timed_out"] is True
    assert got["truncated"] is True
    assert got["paths"][0]["timed_out"] is True
    assert got["paths"][0]["truncation_reason"] == "timeout"


def test_pressure_path_usage_coalesces_nested_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    nested = tmp_path / "hot" / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_text("x")
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        pressure_paths="/hot/nested:/hot",
    )

    got = asyncio.run(server.get_pressure_path_usage(limit=10, max_entries_per_path=100))

    assert [path["path"] for path in got["paths"]] == ["/hot"]
    assert got["skipped_nested_paths"] == [{"path": "/hot/nested", "covered_by": "/hot"}]


def test_filesystem_pressure_stays_responsive_during_path_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "hot"
    directory.mkdir()
    (directory / "payload.txt").write_text("x")
    server = _load(monkeypatch, str(tmp_path), "", pressure_paths="/hot")
    original_du_path = server._du_path
    scan_started = threading.Event()

    def slow_du_path(*args, **kwargs):
        scan_started.set()
        time.sleep(0.25)
        return original_du_path(*args, **kwargs)

    monkeypatch.setattr(server, "_du_path", slow_du_path)

    async def run_concurrently() -> float:
        scan = asyncio.create_task(
            server.get_pressure_path_usage(limit=1, max_entries_per_path=100)
        )
        assert await asyncio.to_thread(scan_started.wait, 1)
        started = time.monotonic()
        assert server.get_filesystem_pressure()["root"]["total_bytes"] > 0
        elapsed = time.monotonic() - started
        await scan
        return elapsed

    assert asyncio.run(run_concurrently()) < 0.1


def test_top_processes_validates_sort(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load(monkeypatch, "/", "")
    with pytest.raises(ValueError, match="sort_by"):
        server.get_top_processes(sort_by="bogus")


def test_k3s_pods_normalize_api_items(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load(monkeypatch, "/", "")
    monkeypatch.setattr(
        server,
        "_k8s_list",
        lambda path: (
            [
                {
                    "metadata": {
                        "name": "open-webui-123",
                        "namespace": "ai",
                        "uid": "11111111-1111-1111-1111-111111111111",
                        "creationTimestamp": "2026-07-09T20:00:00Z",
                    },
                    "spec": {
                        "nodeName": "kai-server",
                        "containers": [
                            {"name": "web", "image": "ghcr.io/open-webui/open-webui:latest"}
                        ],
                    },
                    "status": {
                        "phase": "Running",
                        "podIP": "10.0.0.10",
                        "containerStatuses": [
                            {
                                "name": "web",
                                "restartCount": 2,
                                "ready": True,
                                "containerID": "containerd://a1b2c3d4e5f6",
                                "state": {"running": {}},
                            }
                        ],
                    },
                }
            ],
            [],
        ),
    )

    got = server.get_k3s_pods()
    assert got["errors"] == []
    assert got["pods"][0]["namespace"] == "ai"
    assert got["pods"][0]["restart_count"] == 2
    assert got["pods"][0]["age_seconds"] is not None
    assert got["pods"][0]["containers"][0]["container_id"] == "a1b2c3d4e5f6"


def test_k3s_process_attribution_uses_cgroup_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load(monkeypatch, "/", "")
    pod = {
        "namespace": "ai",
        "pod": "open-webui-123",
        "uid": "11111111-1111-1111-1111-111111111111",
        "containers": [
            {
                "name": "web",
                "image": "ghcr.io/open-webui/open-webui:latest",
                "container_id": "a1b2c3d4e5f6",
            }
        ],
    }
    monkeypatch.setattr(
        server,
        "_k8s_pod_inventory",
        lambda: (
            [pod],
            {"a1b2c3d4e5f6": (pod, pod["containers"][0])},
            {pod["uid"]: pod},
            [],
        ),
    )
    monkeypatch.setattr(
        server,
        "_iter_host_processes",
        lambda: [
            {
                "pid": 42,
                "name": "python",
                "username": "root",
                "cpu_percent": 0.0,
                "memory_percent": 1.5,
                "rss_bytes": 123456,
            }
        ],
    )
    monkeypatch.setattr(
        server,
        "_read_host_text",
        lambda path: (
            "0::/kubepods.slice/kubepods-besteffort.slice/"
            "kubepods-besteffort-pod11111111-1111-1111-1111-111111111111.slice/"
            "cri-containerd-a1b2c3d4e5f6.scope"
            if path == "/proc/42/cgroup"
            else None
        ),
    )

    got = server.get_k3s_process_attribution(limit=1)
    proc = got["processes"][0]
    assert proc["name"] == "python"
    assert proc["kubernetes"] == {
        "namespace": "ai",
        "pod": "open-webui-123",
        "container": "web",
        "pod_uid": "11111111-1111-1111-1111-111111111111",
        "container_id": "a1b2c3d4e5f6",
        "matched_by": "container_id",
    }


def test_k3s_container_memory_falls_back_to_cgroup_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load(monkeypatch, "/", "")
    pod = {
        "namespace": "ai",
        "pod": "reddit-mcp",
        "uid": "22222222-2222-2222-2222-222222222222",
        "containers": [
            {
                "name": "app",
                "image": "ghcr.io/example/reddit-mcp:latest",
                "container_id": "f1e2d3c4b5a6",
            }
        ],
    }
    monkeypatch.setattr(
        server,
        "_k8s_pod_inventory",
        lambda: (
            [pod],
            {"f1e2d3c4b5a6": (pod, pod["containers"][0])},
            {pod["uid"]: pod},
            [],
        ),
    )
    monkeypatch.setattr(server, "_k8s_pod_metrics", lambda: ([], ["metrics unavailable"]))
    monkeypatch.setattr(
        server,
        "_iter_host_processes",
        lambda: [
            {"pid": 7, "rss_bytes": 2048, "name": "python", "cpu_percent": 0.0},
            {"pid": 8, "rss_bytes": 1024, "name": "uvicorn", "cpu_percent": 0.0},
        ],
    )
    monkeypatch.setattr(
        server,
        "_read_host_text",
        lambda path: (
            "0::/kubepods.slice/kubepods-besteffort.slice/"
            "kubepods-besteffort-pod22222222-2222-2222-2222-222222222222.slice/"
            "cri-containerd-f1e2d3c4b5a6.scope"
            if path in {"/proc/7/cgroup", "/proc/8/cgroup"}
            else None
        ),
    )

    got = server.get_k3s_container_memory()
    assert got["source"] == "cgroup-rss"
    assert got["containers"][0]["memory_bytes"] == 3072
