"""Behavioural tests for the node-stats tools.

The tools are registered with FastMCP without rebinding their names, so the
plain callables stay directly invokable here. The focus is the security
envelope: file reads are denied unless a root is allowlisted, and the allowlist
cannot be escaped.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
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
    max_pressure_children_per_root: str | None = None,
    du_timeout_seconds: str | None = None,
    k3s_volume_roots: str | None = None,
    max_k3s_volume_paths: str | None = None,
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
    if max_pressure_children_per_root is None:
        monkeypatch.delenv("NODE_STATS_MAX_PRESSURE_CHILDREN_PER_ROOT", raising=False)
    else:
        monkeypatch.setenv(
            "NODE_STATS_MAX_PRESSURE_CHILDREN_PER_ROOT",
            max_pressure_children_per_root,
        )
    if du_timeout_seconds is None:
        monkeypatch.delenv("NODE_STATS_DU_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_DU_TIMEOUT_SECONDS", du_timeout_seconds)
    if k3s_volume_roots is None:
        monkeypatch.delenv("NODE_STATS_K3S_VOLUME_ROOTS", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_K3S_VOLUME_ROOTS", k3s_volume_roots)
    if max_k3s_volume_paths is None:
        monkeypatch.delenv("NODE_STATS_MAX_K3S_VOLUME_PATHS", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_MAX_K3S_VOLUME_PATHS", max_k3s_volume_paths)
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
    hot_child = by_path["/hot"]["children"][0]
    assert hot_child["path"] == "/hot/blob.bin"
    assert hot_child["permission_errors"] == 0
    assert hot_child["scan_errors"] == 0
    assert hot_child["skipped_different_filesystem"] == 0
    assert {
        "size_bytes",
        "entries_scanned",
        "permission_errors",
        "scan_errors",
        "skipped_different_filesystem",
        "timed_out",
        "truncated",
    } <= hot_child.keys()


def test_pressure_path_usage_truncates_large_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    large = tmp_path / "hot" / "large"
    large.mkdir(parents=True)
    for idx in range(10):
        (large / f"{idx}.txt").write_text("x")
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        pressure_paths="/hot",
        max_du_entries="3",
    )

    got = asyncio.run(server.get_pressure_path_usage(limit=1, max_entries_per_path=1000))
    child = got["paths"][0]["children"][0]

    assert got["max_entries_per_path"] == 3
    assert got["max_entries_per_child"] == 3
    assert child["path"] == "/hot/large"
    assert child["entries_scanned"] == 3
    assert child["truncated"] is True
    assert child["truncation_reason"] == "per_path_entry_cap"


def test_pressure_path_usage_shares_budget_across_roots_and_children(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("hot", "cold"):
        directory = tmp_path / name / "large"
        directory.mkdir(parents=True)
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
    assert got["max_entries_per_child"] == 2
    assert got["truncated"] is True
    children = [path["children"][0] for path in got["paths"]]
    assert {child["path"] for child in children} == {"/hot/large", "/cold/large"}
    assert all(child["entries_scanned"] == 2 for child in children)
    assert all(child["truncated"] is True for child in children)


def test_pressure_path_usage_bounds_discovery_per_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("hot", "cold"):
        root = tmp_path / name
        root.mkdir()
        for child in ("a", "b", "c"):
            (root / child).mkdir()
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        pressure_paths="/hot:/cold",
        max_pressure_children_per_root="1",
    )

    got = asyncio.run(server.get_pressure_path_usage(limit=10, max_entries_per_path=100))

    assert got["max_children_per_root"] == 1
    assert [path["path"] for path in got["paths"]] == ["/hot", "/cold"]
    assert all(path["children_discovered"] == 1 for path in got["paths"])
    assert all(path["discovery_truncated"] is True for path in got["paths"])
    assert all(path["discovery_truncation_reason"] == "child_cap" for path in got["paths"])


def test_pressure_path_usage_reports_unscanned_children_fairly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("hot", "cold"):
        root = tmp_path / name
        root.mkdir()
        for child in ("a", "b"):
            (root / child).mkdir()
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        pressure_paths="/hot:/cold",
        max_du_total_entries="2",
    )

    got = asyncio.run(server.get_pressure_path_usage(limit=10, max_entries_per_path=100))

    assert got["total_entries_scanned"] == 2
    for root in got["paths"]:
        assert sum(child["entries_scanned"] for child in root["children"]) == 1
        assert (
            sum(child["truncation_reason"] == "global_entry_budget" for child in root["children"])
            == 1
        )


def test_pressure_path_usage_reports_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "hot" / "large"
    directory.mkdir(parents=True)
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
        if Path(path).name == "large":
            time.sleep(0.02)
        return original_scandir(path)

    monkeypatch.setattr(server.os, "scandir", slow_scandir)

    got = asyncio.run(server.get_pressure_path_usage(limit=1, max_entries_per_path=100))

    assert got["timed_out"] is True
    assert got["truncated"] is True
    child = got["paths"][0]["children"][0]
    assert child["timed_out"] is True
    assert child["truncation_reason"] == "timeout"


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
    assert [child["path"] for child in got["paths"][0]["children"]] == ["/hot/nested"]
    assert got["skipped_nested_paths"] == [{"path": "/hot/nested", "covered_by": "/hot"}]


def test_pressure_path_usage_has_no_arbitrary_path_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "hot" / "inside").mkdir(parents=True)
    (tmp_path / "outside" / "secret").mkdir(parents=True)
    server = _load(monkeypatch, str(tmp_path), "", pressure_paths="/hot")

    got = asyncio.run(server.get_pressure_path_usage())
    reported = {child["path"] for root in got["paths"] for child in root["children"]}

    assert "path" not in inspect.signature(server.get_pressure_path_usage).parameters
    assert reported == {"/hot/inside"}


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


def _k3s_volume_api_items(claims: list[tuple[str, str]]) -> dict[str, list[dict]]:
    pods: list[dict] = []
    pvcs: list[dict] = []
    pvs: list[dict] = []
    for index, (claim_name, local_path) in enumerate(claims):
        volume_name = f"volume-{index}"
        pods.append(
            {
                "metadata": {
                    "name": f"pod-{index}",
                    "namespace": "apps",
                    "uid": f"pod-uid-{index}",
                },
                "spec": {
                    "volumes": [
                        {
                            "name": "data",
                            "persistentVolumeClaim": {"claimName": claim_name},
                        }
                    ],
                    "containers": [
                        {
                            "name": "app",
                            "volumeMounts": [
                                {
                                    "name": "data",
                                    "mountPath": "/data",
                                }
                            ],
                        }
                    ],
                },
                "status": {"phase": "Running"},
            }
        )
        pvcs.append(
            {
                "metadata": {
                    "name": claim_name,
                    "namespace": "apps",
                    "uid": f"claim-uid-{index}",
                },
                "spec": {
                    "volumeName": volume_name,
                    "storageClassName": "local-path",
                    "resources": {"requests": {"storage": "1Gi"}},
                },
                "status": {"phase": "Bound", "capacity": {"storage": "1Gi"}},
            }
        )
        pvs.append(
            {
                "metadata": {
                    "name": volume_name,
                    "uid": f"volume-uid-{index}",
                },
                "spec": {
                    "claimRef": {
                        "namespace": "apps",
                        "name": claim_name,
                    },
                    "storageClassName": "local-path",
                    "capacity": {"storage": "1Gi"},
                    "hostPath": {"path": local_path},
                },
                "status": {"phase": "Bound"},
            }
        )
    return {
        "/api/v1/pods": pods,
        "/api/v1/persistentvolumeclaims": pvcs,
        "/api/v1/persistentvolumes": pvs,
    }


def test_k3s_volume_usage_joins_pvcs_pvs_and_pod_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    volume_root = tmp_path / "volumes"
    claim_path = volume_root / "claim-a"
    orphan_path = volume_root / "orphan"
    claim_path.mkdir(parents=True)
    orphan_path.mkdir()
    (claim_path / "payload.bin").write_bytes(b"x" * 4096)
    (orphan_path / "payload.bin").write_bytes(b"x" * 1024)
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        k3s_volume_roots="volumes",
    )
    api_items = _k3s_volume_api_items([("claim-a", "volumes/claim-a")])
    second_pod = {
        **api_items["/api/v1/pods"][0],
        "metadata": {
            "name": "pod-shared",
            "namespace": "apps",
            "uid": "pod-uid-shared",
        },
    }
    api_items["/api/v1/pods"].append(second_pod)
    monkeypatch.setattr(server, "_k8s_list", lambda path: (api_items[path], []))

    got = asyncio.run(server.get_k3s_volume_usage(limit=10, max_entries_per_volume=1000))
    volume = got["volumes"][0]

    assert got["errors"] == []
    assert volume["namespace"] == "apps"
    assert volume["persistent_volume_claim"] == "claim-a"
    assert volume["persistent_volume"] == "volume-0"
    assert volume["storage_class"] == "local-path"
    assert volume["requested_bytes"] == 1024**3
    assert volume["capacity_bytes"] == 1024**3
    assert volume["scan_status"] == "scanned"
    assert volume["usage"]["size_bytes"] > 0
    assert {mount["pod"] for mount in volume["pod_mounts"]} == {"pod-0", "pod-shared"}
    assert {mount["mount_path"] for mount in volume["pod_mounts"]} == {"/data"}
    assert got["namespaces"][0]["used_bytes"] == volume["usage"]["size_bytes"]
    assert got["namespaces"][0]["pod_count"] == 2
    assert got["unattributed_paths"][0]["path"].replace("\\", "/").endswith("/volumes/orphan")


def test_k3s_volume_usage_rejects_paths_outside_fixed_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "volumes").mkdir()
    outside = tmp_path / "outside" / "claim-a"
    outside.mkdir(parents=True)
    (outside / "payload.bin").write_bytes(b"x" * 4096)
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        k3s_volume_roots="volumes",
    )
    api_items = _k3s_volume_api_items([("claim-a", "outside/claim-a")])
    monkeypatch.setattr(server, "_k8s_list", lambda path: (api_items[path], []))

    got = asyncio.run(server.get_k3s_volume_usage())
    volume = got["volumes"][0]

    assert "path" not in inspect.signature(server.get_k3s_volume_usage).parameters
    assert volume["scan_status"] == "outside_configured_roots"
    assert volume["usage"] is None
    assert "local_path" not in volume
    assert got["paths_scanned"] == 0


def test_k3s_volume_usage_shares_scan_budget_fairly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    volume_root = tmp_path / "volumes"
    claims = []
    for claim_name in ("claim-a", "claim-b"):
        claim_path = volume_root / claim_name
        claim_path.mkdir(parents=True)
        for index in range(10):
            (claim_path / f"{index}.bin").write_bytes(b"x")
        claims.append((claim_name, f"volumes/{claim_name}"))
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        max_du_total_entries="6",
        k3s_volume_roots="volumes",
    )
    api_items = _k3s_volume_api_items(claims)
    monkeypatch.setattr(server, "_k8s_list", lambda path: (api_items[path], []))

    got = asyncio.run(server.get_k3s_volume_usage(limit=10, max_entries_per_volume=100))

    assert got["total_entries_scanned"] == 6
    assert got["max_entries_per_volume"] == 3
    assert all(volume["usage"]["entries_scanned"] == 3 for volume in got["volumes"])
    assert all(volume["usage"]["truncated"] is True for volume in got["volumes"])


def test_k3s_volume_usage_keeps_fast_tools_responsive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _load(monkeypatch, str(tmp_path), "", k3s_volume_roots="volumes")
    scan_started = threading.Event()

    def slow_volume_usage(*args, **kwargs):
        scan_started.set()
        time.sleep(0.25)
        return {"volumes": []}

    monkeypatch.setattr(server, "_k3s_volume_usage", slow_volume_usage)

    async def run_concurrently() -> float:
        scan = asyncio.create_task(server.get_k3s_volume_usage())
        assert await asyncio.to_thread(scan_started.wait, 1)
        started = time.monotonic()
        assert server.get_filesystem_pressure()["root"]["total_bytes"] > 0
        elapsed = time.monotonic() - started
        await scan
        return elapsed

    assert asyncio.run(run_concurrently()) < 0.1
