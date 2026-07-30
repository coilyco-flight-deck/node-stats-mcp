"""Mount-aware, read-only host storage attribution primitives.

The module reads filesystem and fixed proc mount metadata only. It never opens
scanned target contents and it never accepts configuration from an MCP caller.
Server-owned profiles provide the paths and scan bounds.
"""

from __future__ import annotations

import copy
import os
import re
import stat
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

_MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_MEMORY_FILESYSTEMS = {"tmpfs", "ramfs"}
_OVERLAY_FILESYSTEMS = {"overlay", "overlayfs"}


@dataclass(frozen=True)
class MountRecord:
    """One Linux mountinfo record normalized to a host-visible path."""

    mount_id: int
    parent_id: int
    device_id: str
    root: str
    mountpoint: str
    mount_options: tuple[str, ...]
    optional_fields: tuple[str, ...]
    filesystem_type: str
    source: str
    super_options: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {
            "mount_id": self.mount_id,
            "parent_mount_id": self.parent_id,
            "filesystem_id": self.device_id,
            "filesystem_root": self.root,
            "mountpoint": self.mountpoint,
            "mount_source": self.source,
            "mount_type": self.filesystem_type,
            "mount_options": list(self.mount_options),
        }


@dataclass(frozen=True)
class UsageProfile:
    """A server-owned recursive usage profile."""

    name: str
    path: str
    exclude_paths: tuple[str, ...] = ()
    stale_after_seconds: int = 900
    max_entries: int = 5_000_000
    timeout_seconds: float = 900.0
    max_children: int = 10_000


@dataclass
class _Budget:
    max_entries: int | None
    deadline: float | None
    entries_scanned: int = 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _unescape_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _normalize_public_path(path: str) -> str:
    normalized = str(PurePosixPath("/").joinpath(path.lstrip("/")))
    return normalized if normalized != "/." else "/"


def _rooted(rootfs: str | Path, public_path: str) -> Path:
    root = Path(rootfs)
    if public_path == "/":
        return root
    return root.joinpath(public_path.lstrip("/"))


def _public_path(rootfs: str | Path, actual_path: Path) -> str:
    try:
        relative = actual_path.relative_to(Path(rootfs))
    except ValueError:
        return _normalize_public_path(str(actual_path))
    if str(relative) in {"", "."}:
        return "/"
    return "/" + relative.as_posix()


def _mountpoint_to_public(rootfs: str | Path, mountpoint: str) -> str:
    root = str(Path(rootfs))
    if root != "/" and (mountpoint == root or mountpoint.startswith(f"{root}/")):
        suffix = mountpoint[len(root) :]
        return _normalize_public_path(suffix or "/")
    return _normalize_public_path(mountpoint)


def parse_mountinfo(text: str, rootfs: str | Path) -> list[MountRecord]:
    """Parse Linux mountinfo without invoking findmnt or reading mount contents."""
    records: list[MountRecord] = []
    for line in text.splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        trailing = right.split()
        if len(fields) < 6 or len(trailing) < 3:
            continue
        try:
            mount_id = int(fields[0])
            parent_id = int(fields[1])
        except ValueError:
            continue
        records.append(
            MountRecord(
                mount_id=mount_id,
                parent_id=parent_id,
                device_id=fields[2],
                root=_unescape_mount_field(fields[3]),
                mountpoint=_mountpoint_to_public(
                    rootfs,
                    _unescape_mount_field(fields[4]),
                ),
                mount_options=tuple(fields[5].split(",")),
                optional_fields=tuple(fields[6:]),
                filesystem_type=trailing[0],
                source=_unescape_mount_field(trailing[1]),
                super_options=tuple(trailing[2].split(",")),
            )
        )
    records.sort(key=lambda record: (len(PurePosixPath(record.mountpoint).parts), record.mount_id))
    return records


def read_mountinfo(rootfs: str | Path) -> tuple[list[MountRecord], list[str]]:
    """Read the host-mounted mount table when it is available."""
    path = _rooted(rootfs, "/proc/self/mountinfo")
    try:
        text = path.read_text()
    except OSError as exc:
        return [], [f"/proc/self/mountinfo: {exc}"]
    return parse_mountinfo(text, rootfs), []


def _path_contains(parent: str, child: str) -> bool:
    parent_path = PurePosixPath(parent)
    child_path = PurePosixPath(child)
    return child_path == parent_path or parent_path in child_path.parents


def _owning_mount(path: str, mounts: list[MountRecord]) -> MountRecord | None:
    candidates = [mount for mount in mounts if _path_contains(mount.mountpoint, path)]
    if not candidates:
        return None
    return max(candidates, key=lambda mount: len(PurePosixPath(mount.mountpoint).parts))


def _device_id(path_stat: os.stat_result) -> str:
    try:
        return f"{os.major(path_stat.st_dev)}:{os.minor(path_stat.st_dev)}"
    except (AttributeError, ValueError):
        return str(path_stat.st_dev)


def _filesystem(
    path: str,
    path_stat: os.stat_result,
    mounts: list[MountRecord],
) -> dict[str, Any]:
    mount = _owning_mount(path, mounts)
    result: dict[str, Any] = {
        "filesystem_id": _device_id(path_stat),
        "device_number": int(path_stat.st_dev),
        "mountpoint": mount.mountpoint if mount else None,
        "mount_source": mount.source if mount else None,
        "mount_type": mount.filesystem_type if mount else None,
        "filesystem_root": mount.root if mount else None,
        "mount_id": mount.mount_id if mount else None,
    }
    return result


def _allocated_bytes(path_stat: os.stat_result) -> int:
    blocks = getattr(path_stat, "st_blocks", None)
    if blocks is not None:
        return int(blocks) * 512
    return int(path_stat.st_size)


def _stop_reason(budget: _Budget) -> str | None:
    if budget.deadline is not None and time.monotonic() >= budget.deadline:
        return "timeout"
    if budget.max_entries is not None and budget.entries_scanned >= budget.max_entries:
        return "entry_cap"
    return None


def _scan_result(path: str) -> dict[str, Any]:
    return {
        "path": path,
        "exists": True,
        "size_bytes": 0,
        "apparent_bytes": 0,
        "entries_scanned": 0,
        "deduplicated_entries": 0,
        "permission_errors": 0,
        "scan_errors": 0,
        "errors": [],
        "excluded_mount_count": 0,
        "truncated": False,
        "truncation_reason": None,
        "timed_out": False,
        "complete": True,
        "totals_are_lower_bounds": False,
    }


def _record_error(result: dict[str, Any], exc: OSError) -> None:
    result["scan_errors"] += 1
    if len(result["errors"]) < 10:
        result["errors"].append(f"{type(exc).__name__}: {exc}")


def _mount_exclusions(
    profile: UsageProfile,
    target_filesystem_id: str,
    mounts: list[MountRecord],
) -> tuple[dict[Path, dict[str, Any]], list[dict[str, Any]]]:
    exclusions: dict[Path, dict[str, Any]] = {}
    reported: list[dict[str, Any]] = []
    for configured in profile.exclude_paths:
        if configured == profile.path or not _path_contains(profile.path, configured):
            continue
        item = {
            "path": configured,
            "reason": "configured_exclusion",
            "deduplicated": False,
            "mount": None,
        }
        exclusions[_rooted("/", configured)] = item
        reported.append(item)

    owning = _owning_mount(profile.path, mounts)
    seen_physical_mounts: dict[tuple[str, str], str] = (
        {(owning.device_id, owning.root): owning.mountpoint} if owning else {}
    )
    for mount in mounts:
        if mount.mountpoint == profile.path or not _path_contains(
            profile.path,
            mount.mountpoint,
        ):
            continue
        physical_key = (mount.device_id, mount.root)
        duplicate_of = seen_physical_mounts.get(physical_key)
        seen_physical_mounts.setdefault(physical_key, mount.mountpoint)
        if mount.device_id != target_filesystem_id:
            reason = "different_filesystem"
            deduplicated = False
        elif mount.root != "/" or duplicate_of is not None:
            reason = "bind_or_subtree_mount"
            deduplicated = True
        else:
            continue
        item = {
            "path": mount.mountpoint,
            "reason": reason,
            "deduplicated": deduplicated,
            "duplicate_of": duplicate_of,
            "mount": mount.public(),
        }
        exclusions[_rooted("/", mount.mountpoint)] = item
        reported.append(item)
    reported.sort(key=lambda item: str(item["path"]))
    return exclusions, reported


def _actual_exclusions(
    rootfs: str | Path,
    public_exclusions: dict[Path, dict[str, Any]],
) -> dict[Path, dict[str, Any]]:
    return {
        _rooted(rootfs, str(public_path)): item for public_path, item in public_exclusions.items()
    }


def _scan_tree(
    rootfs: str | Path,
    path: Path,
    budget: _Budget,
    exclusions: dict[Path, dict[str, Any]],
    seen_inodes: set[tuple[int, int]],
) -> dict[str, Any]:
    public = _public_path(rootfs, path)
    result = _scan_result(public)
    stack = [path]
    while stack:
        if reason := _stop_reason(budget):
            result["truncated"] = True
            result["truncation_reason"] = reason
            result["timed_out"] = reason == "timeout"
            break
        current = stack.pop()
        if current in exclusions:
            result["excluded_mount_count"] += 1
            continue
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            continue
        except PermissionError:
            result["permission_errors"] += 1
            continue
        except OSError as exc:
            _record_error(result, exc)
            continue

        is_directory = stat.S_ISDIR(current_stat.st_mode)
        inode_key = (int(current_stat.st_dev), int(current_stat.st_ino))
        if not is_directory and inode_key in seen_inodes:
            result["deduplicated_entries"] += 1
            continue
        if not is_directory:
            seen_inodes.add(inode_key)

        result["size_bytes"] += _allocated_bytes(current_stat)
        result["apparent_bytes"] += int(current_stat.st_size)
        result["entries_scanned"] += 1
        budget.entries_scanned += 1
        if not is_directory:
            continue
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if reason := _stop_reason(budget):
                        result["truncated"] = True
                        result["truncation_reason"] = reason
                        result["timed_out"] = reason == "timeout"
                        break
                    if (
                        budget.max_entries is not None
                        and budget.entries_scanned + len(stack) >= budget.max_entries
                    ):
                        result["truncated"] = True
                        result["truncation_reason"] = "entry_cap"
                        break
                    stack.append(Path(entry.path))
        except PermissionError:
            result["permission_errors"] += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            _record_error(result, exc)

    result["complete"] = not (
        result["truncated"] or result["permission_errors"] or result["scan_errors"]
    )
    result["totals_are_lower_bounds"] = not result["complete"]
    return result


def scan_usage_profile(
    rootfs: str | Path,
    profile: UsageProfile,
    *,
    mountinfo: list[MountRecord] | None = None,
) -> dict[str, Any]:
    """Scan one fixed profile to completion or an explicit lower bound."""
    started_at = _iso_now()
    started_monotonic = time.monotonic()
    mounts, mount_errors = (mountinfo, []) if mountinfo is not None else read_mountinfo(rootfs)
    target = _rooted(rootfs, profile.path)
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        complete = not mount_errors
        return {
            "profile": profile.name,
            "path": profile.path,
            "exists": False,
            "status": "missing" if complete else "incomplete",
            "complete": complete,
            "totals_are_lower_bounds": not complete,
            "captured_at": _iso_now(),
            "started_at": started_at,
            "duration_seconds": time.monotonic() - started_monotonic,
            "filesystem": None,
            "children": [],
            "child_count": 0,
            "size_bytes": 0,
            "apparent_bytes": 0,
            "entries_scanned": 0,
            "permission_errors": 0,
            "scan_errors": 0,
            "timed_out": False,
            "truncated": False,
            "discovery_truncated": False,
            "excluded_mounts": [],
            "excluded_mount_count": 0,
            "errors": mount_errors,
            "scan_limits": {
                "max_entries": profile.max_entries,
                "timeout_seconds": profile.timeout_seconds,
                "max_children": profile.max_children,
            },
        }
    except OSError as exc:
        return {
            "profile": profile.name,
            "path": profile.path,
            "exists": None,
            "status": "failed",
            "complete": False,
            "totals_are_lower_bounds": True,
            "captured_at": _iso_now(),
            "started_at": started_at,
            "duration_seconds": time.monotonic() - started_monotonic,
            "filesystem": None,
            "children": [],
            "child_count": 0,
            "size_bytes": 0,
            "entries_scanned": 0,
            "excluded_mounts": [],
            "errors": [*mount_errors, f"{type(exc).__name__}: {exc}"],
        }

    filesystem = _filesystem(profile.path, target_stat, mounts)
    public_exclusions, excluded_mounts = _mount_exclusions(
        profile,
        str(filesystem["filesystem_id"]),
        mounts,
    )
    exclusions = _actual_exclusions(rootfs, public_exclusions)
    errors = list(mount_errors)
    if filesystem["mount_id"] is None:
        errors.append(f"{profile.path}: no matching mountinfo record")
    budget = _Budget(
        max_entries=profile.max_entries if profile.max_entries > 0 else None,
        deadline=(
            time.monotonic() + profile.timeout_seconds if profile.timeout_seconds > 0 else None
        ),
    )
    children: list[Path] = []
    discovery_truncated = False
    discovery_timed_out = False
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                if budget.deadline is not None and time.monotonic() >= budget.deadline:
                    discovery_truncated = True
                    discovery_timed_out = True
                    break
                if len(children) >= max(1, profile.max_children):
                    discovery_truncated = True
                    break
                children.append(Path(entry.path))
    except OSError as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    children.sort(key=str)
    seen_inodes: set[tuple[int, int]] = set()
    child_results = [
        _scan_tree(rootfs, child, budget, exclusions, seen_inodes) for child in children
    ]
    child_results.sort(key=lambda item: (-int(item["size_bytes"]), str(item["path"])))
    permission_errors = sum(int(item["permission_errors"]) for item in child_results)
    scan_errors = sum(int(item["scan_errors"]) for item in child_results)
    truncated = discovery_truncated or any(bool(item["truncated"]) for item in child_results)
    complete = not (errors or permission_errors or scan_errors or truncated)
    return {
        "profile": profile.name,
        "path": profile.path,
        "exists": True,
        "status": "complete" if complete else "incomplete",
        "complete": complete,
        "totals_are_lower_bounds": not complete,
        "started_at": started_at,
        "captured_at": _iso_now(),
        "duration_seconds": time.monotonic() - started_monotonic,
        "stale_after_seconds": profile.stale_after_seconds,
        "filesystem": filesystem,
        "children": child_results,
        "child_count": len(child_results),
        "size_bytes": sum(int(item["size_bytes"]) for item in child_results),
        "apparent_bytes": sum(int(item["apparent_bytes"]) for item in child_results),
        "entries_scanned": budget.entries_scanned,
        "permission_errors": permission_errors,
        "scan_errors": scan_errors,
        "timed_out": discovery_timed_out or any(bool(item["timed_out"]) for item in child_results),
        "truncated": truncated,
        "discovery_truncated": discovery_truncated,
        "excluded_mounts": excluded_mounts,
        "excluded_mount_count": len(excluded_mounts),
        "errors": errors,
        "scan_limits": {
            "max_entries": profile.max_entries,
            "timeout_seconds": profile.timeout_seconds,
            "max_children": profile.max_children,
        },
    }


class HostUsageSnapshots:
    """Thread-safe lazy background snapshots for fixed host usage profiles."""

    def __init__(self, rootfs: str | Path, profiles: list[UsageProfile]):
        self.rootfs = str(rootfs)
        self.profiles = {profile.name: profile for profile in profiles}
        self._lock = threading.Lock()
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._refresh_started: dict[str, float] = {}

    def configured_profiles(self) -> list[dict[str, Any]]:
        return [
            {
                "profile": profile.name,
                "path": profile.path,
                "stale_after_seconds": profile.stale_after_seconds,
                "max_entries": profile.max_entries,
                "timeout_seconds": profile.timeout_seconds,
                "max_children": profile.max_children,
            }
            for profile in self.profiles.values()
        ]

    def _refresh(self, profile: UsageProfile) -> None:
        try:
            snapshot = scan_usage_profile(self.rootfs, profile)
        except Exception as exc:  # pragma: no cover - defensive thread boundary
            snapshot = {
                "profile": profile.name,
                "path": profile.path,
                "status": "failed",
                "complete": False,
                "totals_are_lower_bounds": True,
                "captured_at": _iso_now(),
                "children": [],
                "child_count": 0,
                "size_bytes": 0,
                "entries_scanned": 0,
                "errors": [f"{type(exc).__name__}: {exc}"],
            }
        with self._lock:
            self._snapshots[profile.name] = snapshot
            self._refresh_started.pop(profile.name, None)

    def request(self, name: str, *, limit: int, refresh: bool) -> dict[str, Any]:
        """Return cached state immediately and schedule refreshes in the background."""
        profile = self.profiles.get(name)
        if profile is None:
            choices = ", ".join(sorted(self.profiles)) or "(none)"
            raise ValueError(f"unknown host usage profile {name!r}. Configured profiles: {choices}")
        now = time.time()
        thread: threading.Thread | None = None
        with self._lock:
            snapshot = copy.deepcopy(self._snapshots.get(name))
            refresh_started = self._refresh_started.get(name)
            captured_at = (
                datetime.fromisoformat(str(snapshot["captured_at"])).timestamp()
                if snapshot and snapshot.get("captured_at")
                else None
            )
            age_seconds = max(0, int(now - captured_at)) if captured_at is not None else None
            stale = age_seconds is None or age_seconds >= profile.stale_after_seconds
            if (refresh or stale) and refresh_started is None:
                self._refresh_started[name] = now
                refresh_started = now
                thread = threading.Thread(
                    target=self._refresh,
                    args=(profile,),
                    name=f"host-usage-{name}",
                    daemon=True,
                )
        if thread is not None:
            thread.start()

        cap = max(1, min(limit, 100))
        if snapshot is not None:
            children = snapshot.get("children")
            if isinstance(children, list):
                snapshot["returned_child_count"] = min(len(children), cap)
                snapshot["child_results_truncated"] = len(children) > cap
                snapshot["children"] = children[:cap]
        return {
            "profile": name,
            "configured_profiles": self.configured_profiles(),
            "snapshot_status": snapshot.get("status") if snapshot else "pending",
            "snapshot_age_seconds": age_seconds,
            "snapshot_stale": stale,
            "snapshot": snapshot,
            "refresh": {
                "running": refresh_started is not None,
                "started_at": (
                    datetime.fromtimestamp(refresh_started, UTC).isoformat()
                    if refresh_started is not None
                    else None
                ),
                "requested": refresh,
            },
        }


def _coalesced_paths(paths: tuple[str, ...]) -> tuple[list[str], list[dict[str, str]]]:
    selected: list[str] = []
    skipped: list[dict[str, str]] = []
    for path in sorted(dict.fromkeys(paths), key=lambda item: len(PurePosixPath(item).parts)):
        covering = next(
            (candidate for candidate in selected if _path_contains(candidate, path)),
            None,
        )
        if covering is None:
            selected.append(path)
        else:
            skipped.append({"path": path, "covered_by": covering})
    return selected, skipped


def scan_log_usage(
    rootfs: str | Path,
    log_paths: tuple[str, ...],
    journal_paths: tuple[str, ...],
    *,
    max_entries: int,
    timeout_seconds: float,
    max_children: int,
    limit: int,
) -> dict[str, Any]:
    """Scan configured log roots and journald roots without double counting."""
    started = time.monotonic()
    logs, skipped_logs = _coalesced_paths(log_paths)
    journals, skipped_journals = _coalesced_paths(journal_paths)
    targets = [("log", path) for path in logs] + [("journal", path) for path in journals]
    count = max(1, len(targets))
    entry_slice = max(1, max_entries // count)
    time_slice = max(0.001, timeout_seconds / count)
    results: dict[str, list[dict[str, Any]]] = {"log": [], "journal": []}
    journal_exclusions = tuple(journals)
    for kind, path in targets:
        excludes = (
            tuple(journal for journal in journal_exclusions if _path_contains(path, journal))
            if kind == "log"
            else ()
        )
        profile = UsageProfile(
            name=f"{kind}:{path}",
            path=path,
            exclude_paths=excludes,
            stale_after_seconds=0,
            max_entries=entry_slice,
            timeout_seconds=time_slice,
            max_children=max_children,
        )
        results[kind].append(scan_usage_profile(rootfs, profile))

    all_results = [*results["log"], *results["journal"]]
    complete = all(bool(result["complete"]) for result in all_results)
    cap = max(1, min(limit, 100))
    for result in all_results:
        children = result.get("children", [])
        result["returned_child_count"] = min(len(children), cap)
        result["child_results_truncated"] = len(children) > cap
        result["children"] = children[:cap]
    return {
        "log_roots": results["log"],
        "journald_roots": results["journal"],
        "configured_log_paths": list(log_paths),
        "configured_journal_paths": list(journal_paths),
        "skipped_nested_paths": [*skipped_logs, *skipped_journals],
        "size_bytes": sum(int(result["size_bytes"]) for result in all_results),
        "entries_scanned": sum(int(result["entries_scanned"]) for result in all_results),
        "complete": complete,
        "totals_are_lower_bounds": not complete,
        "timed_out": any(bool(result.get("timed_out")) for result in all_results),
        "truncated": any(bool(result.get("truncated")) for result in all_results),
        "errors": [str(error) for result in all_results for error in result.get("errors", [])],
        "duration_seconds": time.monotonic() - started,
        "scan_limits": {
            "max_entries": max_entries,
            "timeout_seconds": timeout_seconds,
            "max_children_per_root": max_children,
        },
    }


def _mounts_by_device(mounts: list[MountRecord]) -> dict[str, list[MountRecord]]:
    grouped: dict[str, list[MountRecord]] = defaultdict(list)
    for mount in mounts:
        grouped[mount.device_id].append(mount)
    return grouped


def _deleted_file_mount(
    target: str,
    device_id: str,
    mounts_by_device: dict[str, list[MountRecord]],
) -> MountRecord | None:
    clean_target = target.removesuffix(" (deleted)")
    candidates = mounts_by_device.get(device_id, [])
    path_matches = [
        mount
        for mount in candidates
        if clean_target.startswith("/") and _path_contains(mount.mountpoint, clean_target)
    ]
    if path_matches:
        return max(path_matches, key=lambda mount: len(PurePosixPath(mount.mountpoint).parts))
    return candidates[0] if candidates else None


def _deleted_category(
    target: str,
    path_stat: os.stat_result,
    mount: MountRecord | None,
) -> str:
    normalized = target.lstrip("/")
    if normalized.startswith("memfd:"):
        return "memfd"
    if stat.S_ISCHR(path_stat.st_mode) or stat.S_ISBLK(path_stat.st_mode):
        return "device"
    filesystem_type = mount.filesystem_type if mount else ""
    if filesystem_type in _MEMORY_FILESYSTEMS:
        return "tmpfs"
    if filesystem_type in _OVERLAY_FILESYSTEMS:
        return "container_overlay"
    if stat.S_ISREG(path_stat.st_mode):
        if mount is None:
            return "unknown_filesystem"
        return "disk_backed_reclaimable" if path_stat.st_nlink == 0 else "disk_backed_linked"
    return "other_non_disk"


def _process_mounts(
    pid_path: Path,
    rootfs: str | Path,
    fallback: dict[str, list[MountRecord]],
) -> dict[str, list[MountRecord]]:
    try:
        text = (pid_path / "mountinfo").read_text()
    except OSError:
        return fallback
    mounts = parse_mountinfo(text, rootfs)
    return _mounts_by_device(mounts) if mounts else fallback


def deleted_open_files(
    rootfs: str | Path,
    *,
    max_pids: int,
    max_fds_per_process: int,
    timeout_seconds: float,
    limit: int,
) -> dict[str, Any]:
    """Summarize deleted open files by storage class without returning filenames."""
    started = time.monotonic()
    deadline = started + max(0.001, timeout_seconds)
    mounts, mount_errors = read_mountinfo(rootfs)
    mounts_by_device = _mounts_by_device(mounts)
    proc_root = _rooted(rootfs, "/proc")
    errors = list(mount_errors)
    pids: list[Path] = []
    pid_truncated = False
    timed_out = False
    try:
        with os.scandir(proc_root) as proc_entries:
            for entry in proc_entries:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                if not entry.name.isdigit():
                    continue
                if len(pids) >= max(1, max_pids):
                    pid_truncated = True
                    break
                pids.append(Path(entry.path))
    except OSError as exc:
        errors.append(f"/proc: {type(exc).__name__}: {exc}")
    pids.sort(key=lambda path: int(path.name))
    records: dict[tuple[int, int], dict[str, Any]] = {}
    fds_examined = 0
    pids_examined = 0
    fd_truncated_processes = 0
    permission_errors = 0
    scan_errors = 0
    for pid_path in pids:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        pids_examined += 1
        fd_root = pid_path / "fd"
        fds: list[Path] = []
        try:
            with os.scandir(fd_root) as fd_entries:
                for entry in fd_entries:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    if len(fds) >= max(1, max_fds_per_process):
                        fd_truncated_processes += 1
                        break
                    fds.append(Path(entry.path))
        except PermissionError:
            permission_errors += 1
            continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            scan_errors += 1
            continue
        if timed_out:
            break
        process_mounts = _process_mounts(pid_path, rootfs, mounts_by_device)
        for fd_path in fds:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            fds_examined += 1
            try:
                target = os.readlink(fd_path)
                if " (deleted)" not in target:
                    continue
                path_stat = fd_path.stat()
            except PermissionError:
                permission_errors += 1
                continue
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                scan_errors += 1
                continue
            inode_key = (int(path_stat.st_dev), int(path_stat.st_ino))
            device_id = _device_id(path_stat)
            mount = _deleted_file_mount(target, device_id, process_mounts)
            category = _deleted_category(target, path_stat, mount)
            record = records.setdefault(
                inode_key,
                {
                    "category": category,
                    "size_bytes": _allocated_bytes(path_stat),
                    "apparent_bytes": int(path_stat.st_size),
                    "reference_count": 0,
                    "_pids": set(),
                    "filesystem": {
                        "filesystem_id": device_id,
                        "mountpoint": mount.mountpoint if mount else None,
                        "mount_source": mount.source if mount else None,
                        "mount_type": mount.filesystem_type if mount else None,
                    },
                },
            )
            record["reference_count"] += 1
            record["_pids"].add(pid_path.name)

    categories: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    for record in records.values():
        pids_for_record = record.pop("_pids")
        record["process_count"] = len(pids_for_record)
        entries.append(record)
        rollup = categories.setdefault(
            str(record["category"]),
            {
                "category": record["category"],
                "file_count": 0,
                "size_bytes": 0,
                "apparent_bytes": 0,
                "reference_count": 0,
            },
        )
        rollup["file_count"] += 1
        rollup["size_bytes"] += int(record["size_bytes"])
        rollup["apparent_bytes"] += int(record["apparent_bytes"])
        rollup["reference_count"] += int(record["reference_count"])
    entries.sort(key=lambda item: int(item["size_bytes"]), reverse=True)
    category_list = sorted(
        categories.values(),
        key=lambda item: int(item["size_bytes"]),
        reverse=True,
    )
    cap = max(1, min(limit, 100))
    complete = not (
        timed_out
        or pid_truncated
        or fd_truncated_processes
        or permission_errors
        or scan_errors
        or errors
    )
    reclaimable = categories.get("disk_backed_reclaimable", {})
    return {
        "categories": category_list,
        "deleted_files": entries[:cap],
        "deleted_file_count": len(entries),
        "returned_deleted_file_count": min(len(entries), cap),
        "disk_backed_reclaimable_bytes": int(reclaimable.get("size_bytes", 0)),
        "disk_backed_reclaimable_files": int(reclaimable.get("file_count", 0)),
        "pids_examined": pids_examined,
        "fds_examined": fds_examined,
        "pid_scan_truncated": pid_truncated,
        "fd_truncated_processes": fd_truncated_processes,
        "permission_errors": permission_errors,
        "scan_errors": scan_errors,
        "timed_out": timed_out,
        "complete": complete,
        "totals_are_lower_bounds": not complete,
        "errors": errors,
        "duration_seconds": time.monotonic() - started,
        "scan_limits": {
            "max_pids": max_pids,
            "max_fds_per_process": max_fds_per_process,
            "timeout_seconds": timeout_seconds,
        },
        "paths_returned": False,
        "contents_read": False,
    }
