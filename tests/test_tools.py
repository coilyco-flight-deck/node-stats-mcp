"""Behavioural tests for the node-stats tools.

The tools are registered with FastMCP without rebinding their names, so the
plain callables stay directly invokable here. The focus is the security
envelope: file reads are denied unless a root is allowlisted, and the allowlist
cannot be escaped.
"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest


def _load(monkeypatch: pytest.MonkeyPatch, tmp_root: str, roots: str) -> ModuleType:
    """Reimport the server module with ROOTFS + allowlist env applied at import time."""
    monkeypatch.setenv("ROOTFS", tmp_root)
    monkeypatch.setenv("NODE_STATS_READABLE_ROOTS", roots)
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
