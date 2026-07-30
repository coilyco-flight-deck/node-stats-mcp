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
import json
import os
import stat
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

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
    k3s_node_name: str | None = None,
    freshness_checks: str | None = None,
    k3s_condition_resources: str | None = None,
    host_usage_profiles: str | None = None,
    host_log_paths: str | None = None,
    journal_paths: str | None = None,
    max_host_log_entries: str | None = None,
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
    if k3s_node_name is None:
        monkeypatch.delenv("NODE_STATS_K3S_NODE_NAME", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_K3S_NODE_NAME", k3s_node_name)
    if freshness_checks is None:
        monkeypatch.delenv("NODE_STATS_FRESHNESS_CHECKS", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_FRESHNESS_CHECKS", freshness_checks)
    if k3s_condition_resources is None:
        monkeypatch.delenv("NODE_STATS_K3S_CONDITION_RESOURCES", raising=False)
    else:
        monkeypatch.setenv(
            "NODE_STATS_K3S_CONDITION_RESOURCES",
            k3s_condition_resources,
        )
    if host_usage_profiles is None:
        monkeypatch.delenv("NODE_STATS_HOST_USAGE_PROFILES", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_HOST_USAGE_PROFILES", host_usage_profiles)
    if host_log_paths is None:
        monkeypatch.delenv("NODE_STATS_HOST_LOG_PATHS", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_HOST_LOG_PATHS", host_log_paths)
    if journal_paths is None:
        monkeypatch.delenv("NODE_STATS_JOURNAL_PATHS", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_JOURNAL_PATHS", journal_paths)
    if max_host_log_entries is None:
        monkeypatch.delenv("NODE_STATS_MAX_HOST_LOG_ENTRIES", raising=False)
    else:
        monkeypatch.setenv("NODE_STATS_MAX_HOST_LOG_ENTRIES", max_host_log_entries)
    import node_stats_mcp.server as server

    return importlib.reload(server)


def _write_mountinfo(tmp_path: Path, extra_records: list[str] | None = None) -> str:
    mountinfo = tmp_path / "proc" / "self" / "mountinfo"
    mountinfo.parent.mkdir(parents=True, exist_ok=True)
    device = tmp_path.stat().st_dev
    device_id = f"{os.major(device)}:{os.minor(device)}"
    records = [f"1 0 {device_id} / / rw,relatime - ext4 /dev/root rw"]
    records.extend(extra_records or [])
    mountinfo.write_text("\n".join(records) + "\n")
    return device_id


def _wait_for_host_snapshot(server: ModuleType, profile: str) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = server.get_host_usage_breakdown(profile=profile, limit=100)
        if result["snapshot"] is not None and not result["refresh"]["running"]:
            return result
        time.sleep(0.01)
    raise AssertionError(f"host usage snapshot {profile!r} did not finish")


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


def test_host_usage_profile_background_snapshot_is_fixed_and_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage_root = tmp_path / "var" / "lib" / "rancher" / "k3s" / "storage"
    for name, size in (("forgejo", 300_000), ("runner", 200_000), ("registry", 100_000)):
        volume = storage_root / name
        volume.mkdir(parents=True)
        (volume / "payload.bin").write_bytes(b"x" * size)
    _write_mountinfo(tmp_path)
    profiles = json.dumps(
        [
            {
                "name": "k3s-storage",
                "path": "/var/lib/rancher/k3s/storage",
                "stale_after_seconds": 60,
                "max_entries": 1000,
                "timeout_seconds": 10,
            }
        ]
    )
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        host_usage_profiles=profiles,
    )

    pending = server.get_host_usage_breakdown(profile="k3s-storage")
    got = _wait_for_host_snapshot(server, "k3s-storage")
    snapshot = got["snapshot"]

    assert pending["snapshot_status"] == "pending"
    assert pending["refresh"]["running"] is True
    assert got["snapshot_stale"] is False
    assert got["snapshot_age_seconds"] is not None
    assert snapshot["status"] == "complete"
    assert snapshot["complete"] is True
    assert snapshot["totals_are_lower_bounds"] is False
    assert [Path(child["path"]).name for child in snapshot["children"][:3]] == [
        "forgejo",
        "runner",
        "registry",
    ]
    assert "path" not in inspect.signature(server.get_host_usage_breakdown).parameters
    with pytest.raises(ValueError, match="unknown host usage profile"):
        server.get_host_usage_breakdown(profile="/var/lib")


def test_host_usage_snapshot_deduplicates_bind_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    physical = tmp_path / "var" / "lib" / "rancher" / "k3s" / "storage" / "forgejo"
    alias = tmp_path / "var" / "lib" / "kubelet" / "pods" / "forgejo"
    physical.mkdir(parents=True)
    alias.mkdir(parents=True)
    (physical / "data.bin").write_bytes(b"x" * 100_000)
    (alias / "data.bin").write_bytes(b"x" * 100_000)
    device_id = _write_mountinfo(tmp_path)
    mountinfo = tmp_path / "proc" / "self" / "mountinfo"
    mountinfo.write_text(
        mountinfo.read_text()
        + (
            f"2 1 {device_id} /var/lib/rancher/k3s/storage/forgejo "
            "/var/lib/kubelet/pods/forgejo rw,relatime - ext4 /dev/root rw\n"
        )
    )
    profiles = json.dumps(
        [
            {
                "name": "var-lib",
                "path": "/var/lib",
                "max_entries": 1000,
                "timeout_seconds": 10,
            }
        ]
    )
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        host_usage_profiles=profiles,
    )

    snapshot = _wait_for_host_snapshot(server, "var-lib")["snapshot"]
    children = {Path(child["path"]).name: child for child in snapshot["children"]}
    excluded = snapshot["excluded_mounts"][0]

    assert snapshot["filesystem"]["filesystem_id"] == device_id
    assert excluded["path"] == "/var/lib/kubelet/pods/forgejo"
    assert excluded["deduplicated"] is True
    assert excluded["mount"]["mount_source"] == "/dev/root"
    assert excluded["mount"]["mount_type"] == "ext4"
    assert children["rancher"]["size_bytes"] > children["kubelet"]["size_bytes"]


def test_host_usage_snapshot_marks_entry_cap_as_lower_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = tmp_path / "large"
    tree.mkdir()
    for index in range(10):
        (tree / f"{index}.bin").write_bytes(b"x" * 1024)
    _write_mountinfo(tmp_path)
    profiles = json.dumps(
        [
            {
                "name": "root",
                "path": "/",
                "max_entries": 2,
                "timeout_seconds": 10,
            }
        ]
    )
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        host_usage_profiles=profiles,
    )

    snapshot = _wait_for_host_snapshot(server, "root")["snapshot"]

    assert snapshot["status"] == "incomplete"
    assert snapshot["complete"] is False
    assert snapshot["totals_are_lower_bounds"] is True
    assert snapshot["truncated"] is True
    assert snapshot["entries_scanned"] == 2


def test_host_usage_background_scan_does_not_block_fast_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "data").mkdir()
    _write_mountinfo(tmp_path)
    profiles = json.dumps([{"name": "root", "path": "/"}])
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        host_usage_profiles=profiles,
    )
    original_scan = server.storage.scan_usage_profile
    scan_started = threading.Event()

    def slow_scan(*args, **kwargs):
        scan_started.set()
        time.sleep(0.25)
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(server.storage, "scan_usage_profile", slow_scan)
    started = time.monotonic()
    pending = server.get_host_usage_breakdown(profile="root")
    elapsed = time.monotonic() - started

    assert scan_started.wait(1)
    assert elapsed < 0.1
    assert pending["snapshot_status"] == "pending"
    assert server.get_filesystem_pressure()["root"]["total_bytes"] > 0
    _wait_for_host_snapshot(server, "root")


def test_host_log_usage_separates_journald_and_counts_sparse_allocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app_log = tmp_path / "var" / "log" / "app" / "app.log"
    journal = tmp_path / "var" / "log" / "journal" / "system.journal"
    app_log.parent.mkdir(parents=True)
    journal.parent.mkdir(parents=True)
    with app_log.open("wb") as sparse:
        sparse.truncate(128 * 1024 * 1024)
    journal.write_bytes(b"x" * 4096)
    _write_mountinfo(tmp_path)
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        host_log_paths="/var/log",
        journal_paths="/var/log/journal",
        max_host_log_entries="1000",
    )

    got = asyncio.run(server.get_host_log_usage(limit=20))
    app_child = next(
        child for child in got["log_roots"][0]["children"] if child["path"] == "/var/log/app"
    )

    assert got["complete"] is True
    assert got["totals_are_lower_bounds"] is False
    assert got["journald_roots"][0]["path"] == "/var/log/journal"
    assert got["journald_roots"][0]["size_bytes"] > 0
    assert app_child["apparent_bytes"] > app_child["size_bytes"]
    assert got["log_roots"][0]["excluded_mounts"][0]["path"] == "/var/log/journal"
    assert "path" not in inspect.signature(server.get_host_log_usage).parameters


def test_host_log_usage_rejects_configured_root_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside = tmp_path.parent / "outside-logs"
    outside.mkdir(exist_ok=True)
    (outside / "secret.log").write_text("not scanned")
    _write_mountinfo(tmp_path)
    server = _load(
        monkeypatch,
        str(tmp_path),
        "",
        host_log_paths="/../outside-logs",
        journal_paths="",
    )

    got = asyncio.run(server.get_host_log_usage())

    assert got["log_roots"] == []
    assert got["complete"] is False
    assert got["totals_are_lower_bounds"] is True
    assert "escapes ROOTFS" in got["configuration_errors"][0]


def test_deleted_open_file_summary_excludes_memory_and_overlay_classes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from node_stats_mcp import storage

    server = _load(monkeypatch, str(tmp_path), "")
    assert "path" not in inspect.signature(server.get_deleted_open_files).parameters
    proc_fds = tmp_path / "proc" / "100" / "fd"
    proc_fds.mkdir(parents=True)
    for fd_name in ("3", "4", "5", "6"):
        (proc_fds / fd_name).symlink_to("/missing")
    disk_file = tmp_path / "deleted.log"
    disk_file.write_bytes(b"x" * 8192)
    disk_fd = os.open(disk_file, os.O_RDONLY)
    os.unlink(disk_file)
    disk_stat = os.fstat(disk_fd)
    disk_device = disk_stat.st_dev
    tmpfs_device = os.makedev(0, 91)
    overlay_device = os.makedev(0, 92)

    def changed_stat(device: int, inode: int) -> SimpleNamespace:
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_dev=device,
            st_ino=inode,
            st_nlink=0,
            st_size=disk_stat.st_size,
            st_blocks=16,
        )

    fake_stats = {
        "3": changed_stat(disk_device, 103),
        "4": changed_stat(disk_device, 104),
        "5": changed_stat(tmpfs_device, 105),
        "6": changed_stat(overlay_device, 106),
    }
    targets = {
        "3": "/var/log/deleted.log (deleted)",
        "4": "/memfd:buffer (deleted)",
        "5": "/run/tmp/deleted (deleted)",
        "6": "/var/lib/containerd/deleted (deleted)",
    }
    disk_id = f"{os.major(disk_device)}:{os.minor(disk_device)}"
    tmpfs_id = f"{os.major(tmpfs_device)}:{os.minor(tmpfs_device)}"
    overlay_id = f"{os.major(overlay_device)}:{os.minor(overlay_device)}"
    _write_mountinfo(
        tmp_path,
        [
            f"2 1 {tmpfs_id} / /run rw - tmpfs tmpfs rw",
            f"3 1 {overlay_id} / /var/lib/containerd rw - overlay overlay rw",
        ],
    )
    mountinfo = tmp_path / "proc" / "self" / "mountinfo"
    mountinfo.write_text(
        mountinfo.read_text().replace(
            mountinfo.read_text().split()[2],
            disk_id,
            1,
        )
    )
    original_readlink = storage.os.readlink
    original_stat = Path.stat

    def fake_readlink(path: str | os.PathLike[str]) -> str:
        candidate = Path(path)
        if candidate.parent.name == "fd":
            return targets[candidate.name]
        return original_readlink(path)

    def fake_path_stat(path: Path, *args, **kwargs):
        if path.parent.name == "fd":
            return fake_stats[path.name]
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(storage.os, "readlink", fake_readlink)
    monkeypatch.setattr(Path, "stat", fake_path_stat)
    try:
        got = storage.deleted_open_files(
            tmp_path,
            max_pids=10,
            max_fds_per_process=10,
            timeout_seconds=2,
            limit=10,
        )
    finally:
        os.close(disk_fd)

    categories = {entry["category"]: entry for entry in got["categories"]}
    assert got["disk_backed_reclaimable_files"] == 1
    assert got["disk_backed_reclaimable_bytes"] == fake_stats["3"].st_size
    assert {"disk_backed_reclaimable", "memfd", "tmpfs", "container_overlay"} <= categories.keys()
    assert got["paths_returned"] is False
    assert got["contents_read"] is False


def test_top_processes_validates_sort(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _load(monkeypatch, "/", "")
    with pytest.raises(ValueError, match="sort_by"):
        server.get_top_processes(sort_by="bogus")


def test_node_pressure_stalls_normalizes_fixed_host_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pressure_root = tmp_path / "proc" / "pressure"
    pressure_root.mkdir(parents=True)
    (pressure_root / "cpu").write_text("some avg10=1.25 avg60=0.50 avg300=0.10 total=1234\n")
    (pressure_root / "memory").write_text(
        "some avg10=0.20 avg60=0.10 avg300=0.05 total=500\n"
        "full avg10=0.10 avg60=0.05 avg300=0.01 total=200\n"
    )
    (pressure_root / "io").write_text("some avg10=2.00 avg60=1.00 avg300=0.50 total=900\n")
    (tmp_path / "proc" / "vmstat").write_text(
        "pgmajfault 42\npswpin 3\npswpout 7\nnr_free_pages 999\n"
    )
    server = _load(monkeypatch, str(tmp_path), "")

    class Counter:
        def _asdict(self):
            return {
                "read_count": 10,
                "write_count": 20,
                "read_bytes": 100,
                "write_bytes": 200,
                "busy_time": 30,
            }

    monkeypatch.setattr(
        server.psutil,
        "disk_io_counters",
        lambda perdisk: {"nvme0n1": Counter()},
    )

    got = server.get_node_pressure_stalls()

    assert got["errors"] == []
    assert got["pressure_stall_information"]["cpu"]["some"]["avg10"] == 1.25
    assert got["pressure_stall_information"]["memory"]["full"]["total"] == 200
    assert got["vm_pressure_counters"] == {
        "pgmajfault": 42,
        "pswpin": 3,
        "pswpout": 7,
    }
    assert got["block_devices"][0]["device"] == "nvme0n1"
    assert "path" not in inspect.signature(server.get_node_pressure_stalls).parameters


def test_k3s_resource_usage_normalizes_and_bounds_kubelet_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _load(monkeypatch, "/", "")
    monkeypatch.setattr(
        server,
        "_k3s_node",
        lambda: ({"metadata": {"name": "kai-server"}}, []),
    )
    requested_paths: list[str] = []

    def fake_request(path: str, params=None):
        requested_paths.append(path)
        return {
            "node": {
                "nodeName": "kai-server",
                "cpu": {"usageNanoCores": 1_000_000},
                "memory": {"workingSetBytes": 4096},
                "fs": {"capacityBytes": 10_000, "usedBytes": 6000},
                "runtime": {"imageFs": {"usedBytes": 1000}},
            },
            "pods": [
                {
                    "podRef": {"namespace": "apps", "name": "small", "uid": "small-uid"},
                    "memory": {"workingSetBytes": 100},
                    "containers": [{"name": "worker", "memory": {"workingSetBytes": 80}}],
                },
                {
                    "podRef": {"namespace": "ai", "name": "large", "uid": "large-uid"},
                    "memory": {"workingSetBytes": 200},
                    "ephemeral-storage": {"usedBytes": 75},
                    "volume": [{"name": "data", "usedBytes": 50}],
                    "containers": [{"name": "web", "memory": {"workingSetBytes": 150}}],
                },
            ],
        }

    monkeypatch.setattr(server, "_k8s_request", fake_request)

    got = asyncio.run(server.get_k3s_resource_usage(limit=1))

    assert requested_paths == ["/api/v1/nodes/kai-server/proxy/stats/summary"]
    assert got["source"] == "kubelet-summary"
    assert got["node"]["filesystem"]["usedBytes"] == 6000
    assert got["pod_count"] == 2
    assert got["returned_pod_count"] == 1
    assert got["pods"][0]["pod"] == "large"
    assert got["pods"][0]["ephemeral_storage"]["usedBytes"] == 75
    assert got["pods"][0]["volumes"][0]["usage"]["usedBytes"] == 50
    assert set(inspect.signature(server.get_k3s_resource_usage).parameters) == {"limit"}


def test_k3s_node_health_filters_and_bounds_recent_relevant_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _load(monkeypatch, "/", "")
    node = {
        "metadata": {
            "name": "kai-server",
            "creationTimestamp": "2026-07-01T00:00:00Z",
        },
        "spec": {
            "taints": [{"key": "maintenance", "effect": "NoSchedule"}],
        },
        "status": {
            "capacity": {"cpu": "8", "memory": "32Gi"},
            "allocatable": {"cpu": "7500m", "memory": "30Gi"},
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "reason": "KubeletReady",
                    "lastTransitionTime": "2026-07-20T00:00:00Z",
                }
            ],
        },
    }
    pod = {
        "metadata": {"name": "app", "namespace": "apps", "uid": "pod-uid"},
        "spec": {"nodeName": "kai-server"},
    }
    events = [
        {
            "metadata": {
                "namespace": "apps",
                "creationTimestamp": "2099-07-25T00:00:00Z",
            },
            "type": "Normal",
            "reason": "Pulled",
            "message": "image present",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "apps",
                "name": "app",
                "uid": "pod-uid",
            },
        },
        {
            "metadata": {"creationTimestamp": "2099-07-25T00:00:00Z"},
            "type": "Warning",
            "reason": "VolumeFailed",
            "message": "volume failed",
            "involvedObject": {"kind": "PersistentVolume", "name": "data"},
        },
        {
            "metadata": {"creationTimestamp": "2099-07-25T00:00:00Z"},
            "type": "Normal",
            "reason": "Unrelated",
            "involvedObject": {"kind": "Pod", "name": "other"},
        },
    ]
    monkeypatch.setattr(server, "_k3s_node", lambda: (node, []))
    monkeypatch.setattr(
        server,
        "_k8s_list",
        lambda path: (
            ([pod] if path == "/api/v1/pods" else events),
            [],
        ),
    )

    got = asyncio.run(server.get_k3s_node_health(limit=1, max_age_hours=24))

    assert got["node"]["conditions"][0]["type"] == "Ready"
    assert got["node"]["taints"][0]["effect"] == "NoSchedule"
    assert got["event_count"] == 2
    assert got["returned_event_count"] == 1
    assert set(inspect.signature(server.get_k3s_node_health).parameters) == {
        "limit",
        "max_age_hours",
    }


def test_k3s_scheduled_work_reports_job_and_cronjob_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _load(monkeypatch, "/", "")
    jobs = [
        {
            "metadata": {
                "name": "backup-123",
                "namespace": "ops",
                "creationTimestamp": "2026-07-25T01:00:00Z",
                "ownerReferences": [{"kind": "CronJob", "name": "backup"}],
            },
            "spec": {"backoffLimit": 3},
            "status": {
                "startTime": "2026-07-25T01:00:00Z",
                "completionTime": "2026-07-25T01:05:00Z",
                "succeeded": 1,
                "conditions": [{"type": "Complete", "status": "True"}],
            },
        }
    ]
    cronjobs = [
        {
            "metadata": {
                "name": "backup",
                "namespace": "ops",
                "creationTimestamp": "2026-07-01T00:00:00Z",
            },
            "spec": {"schedule": "0 1 * * *", "concurrencyPolicy": "Forbid"},
            "status": {
                "lastScheduleTime": "2026-07-25T01:00:00Z",
                "lastSuccessfulTime": "2026-07-25T01:05:00Z",
            },
        }
    ]
    monkeypatch.setattr(
        server,
        "_k8s_list",
        lambda path: (
            jobs if path == "/apis/batch/v1/jobs" else cronjobs,
            [],
        ),
    )

    got = asyncio.run(server.get_k3s_scheduled_work())

    assert got["jobs"][0]["cronjob"] == "backup"
    assert got["jobs"][0]["duration_seconds"] == 300
    assert got["jobs"][0]["succeeded"] == 1
    assert got["cronjobs"][0]["last_successful_age_seconds"] is not None
    assert set(inspect.signature(server.get_k3s_scheduled_work).parameters) == {"limit"}


def test_configured_freshness_uses_server_owned_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "var" / "lib" / "backup" / "last-success"
    marker.parent.mkdir(parents=True)
    marker.write_text("success details are not returned")
    os.utime(marker, (time.time() - 600, time.time() - 600))
    config = json.dumps(
        [
            {
                "name": "backup",
                "path": "/var/lib/backup/last-success",
                "max_age_seconds": 300,
            },
            {
                "name": "missing",
                "path": "/var/lib/backup/missing",
                "max_age_seconds": 300,
            },
        ]
    )
    server = _load(monkeypatch, str(tmp_path), "", freshness_checks=config)

    got = server.get_configured_freshness()

    assert got["errors"] == []
    assert got["checks"][0]["status"] == "stale"
    assert got["checks"][0]["age_seconds"] >= 599
    assert "text" not in got["checks"][0]
    assert got["checks"][1]["status"] == "missing"
    assert inspect.signature(server.get_configured_freshness).parameters == {}


def test_k3s_configured_conditions_uses_server_owned_resource_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = json.dumps(
        [
            {
                "name": "external-secrets",
                "group": "external-secrets.io",
                "version": "v1beta1",
                "resource": "externalsecrets",
                "namespace": "ops",
            }
        ]
    )
    server = _load(
        monkeypatch,
        "/",
        "",
        k3s_condition_resources=config,
    )
    requested_paths: list[str] = []

    def fake_list(path: str):
        requested_paths.append(path)
        return (
            [
                {
                    "metadata": {
                        "name": "registry",
                        "namespace": "ops",
                        "generation": 3,
                        "creationTimestamp": "2026-07-01T00:00:00Z",
                    },
                    "status": {
                        "observedGeneration": 3,
                        "conditions": [
                            {
                                "type": "Ready",
                                "status": "False",
                                "reason": "SecretSyncedError",
                            }
                        ],
                    },
                }
            ],
            [],
        )

    monkeypatch.setattr(server, "_k8s_list", fake_list)

    got = asyncio.run(server.get_k3s_configured_conditions())

    assert requested_paths == ["/apis/external-secrets.io/v1beta1/namespaces/ops/externalsecrets"]
    assert got["sources"][0]["items"][0]["ready"] == "False"
    assert got["sources"][0]["items"][0]["ready_condition_type"] == "Ready"
    assert set(inspect.signature(server.get_k3s_configured_conditions).parameters) == {
        "limit_per_source"
    }


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
                    "finalizers": ["kubernetes.io/pvc-protection"],
                },
                "spec": {
                    "volumeName": volume_name,
                    "storageClassName": "local-path",
                    "accessModes": ["ReadWriteOnce"],
                    "volumeMode": "Filesystem",
                    "resources": {"requests": {"storage": "1Gi"}},
                },
                "status": {
                    "phase": "Bound",
                    "capacity": {"storage": "1Gi"},
                    "conditions": [{"type": "FileSystemResizePending", "status": "False"}],
                },
            }
        )
        pvs.append(
            {
                "metadata": {
                    "name": volume_name,
                    "uid": f"volume-uid-{index}",
                    "finalizers": ["kubernetes.io/pv-protection"],
                },
                "spec": {
                    "claimRef": {
                        "namespace": "apps",
                        "name": claim_name,
                        "uid": f"claim-uid-{index}",
                    },
                    "storageClassName": "local-path",
                    "accessModes": ["ReadWriteOnce"],
                    "volumeMode": "Filesystem",
                    "persistentVolumeReclaimPolicy": "Delete",
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
    assert volume["pvc_finalizers"] == ["kubernetes.io/pvc-protection"]
    assert volume["pv_finalizers"] == ["kubernetes.io/pv-protection"]
    assert volume["pvc_conditions"][0]["type"] == "FileSystemResizePending"
    assert volume["access_modes"] == ["ReadWriteOnce"]
    assert volume["volume_mode"] == "Filesystem"
    assert volume["reclaim_policy"] == "Delete"
    assert volume["scan_status"] == "scanned"
    assert volume["usage_complete"] is True
    assert volume["usage_is_lower_bound"] is False
    assert volume["usage"]["size_bytes"] > 0
    assert {mount["pod"] for mount in volume["pod_mounts"]} == {"pod-0", "pod-shared"}
    assert {mount["mount_path"] for mount in volume["pod_mounts"]} == {"/data"}
    assert got["namespaces"][0]["used_bytes"] == volume["usage"]["size_bytes"]
    assert got["namespaces"][0]["used_bytes_complete"] is True
    assert got["namespaces"][0]["used_bytes_is_lower_bound"] is False
    assert got["namespaces"][0]["pod_count"] == 2
    assert got["unattributed_paths"][0]["path"].replace("\\", "/").endswith("/volumes/orphan")
    assert got["complete"] is True
    assert got["totals_are_lower_bounds"] is False


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
    assert volume["usage_complete"] is False
    assert volume["usage_is_lower_bound"] is True
    assert volume["usage"] is None
    assert "local_path" not in volume
    assert got["paths_scanned"] == 0
    assert got["complete"] is False
    assert got["totals_are_lower_bounds"] is True


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
    assert all(volume["usage_complete"] is False for volume in got["volumes"])
    assert got["complete"] is False
    assert got["totals_are_lower_bounds"] is True


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
