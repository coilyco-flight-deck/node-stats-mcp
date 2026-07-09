"""FastMCP server exposing node-local host introspection over streamable-HTTP.

Read-only by construction: every tool is a read, no tool mutates the host. True
*node* stats (not the pod's cgroup view) rely on the deployment giving the pod
the host's namespaces - hostPID for processes, hostNetwork for net counters -
and CPU/memory come from the non-namespaced /proc/{stat,meminfo} directly. Disk
reads resolve under ROOTFS (the host root, mounted read-only at /host in the
pod). See the deploy bundle in coilyco-bridge/deploy/services/node-stats-mcp.

File introspection is prefix-allowlisted, never arbitrary: stat_path and
read_text_head resolve the real path and refuse anything outside
NODE_STATS_READABLE_ROOTS (empty by default = file reads denied). This is the
enum-not-path discipline from the upstream node-introspection example,
generalized to a root allowlist, so the tool cannot be walked into /host/root/.ssh.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import psutil
from mcp.server.fastmcp import FastMCP

# Host root inside the pod. The deployment mounts the node's / read-only at
# /host and sets ROOTFS=/host; bare local runs leave it at / (the real root).
ROOTFS = os.environ.get("ROOTFS", "/")

# Colon-separated prefixes the file tools may read under, resolved against the
# real (symlink-collapsed) path. Empty = file reads are denied. The prefixes are
# interpreted inside ROOTFS, e.g. "/etc:/var/log" with ROOTFS=/host permits
# /host/etc and /host/var/log only.
_READABLE_ROOTS = [r for r in os.environ.get("NODE_STATS_READABLE_ROOTS", "").split(":") if r]

# Hard cap on read_text_head so a tool call can never stream a huge file.
_MAX_READ_BYTES = int(os.environ.get("NODE_STATS_MAX_READ_BYTES", "65536"))

mcp = FastMCP(
    "node-stats",
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8080")),
)


def _allowed_roots() -> list[Path]:
    return [Path(ROOTFS).joinpath(r.lstrip("/")).resolve() for r in _READABLE_ROOTS]


def _resolve_readable(path: str) -> Path:
    """Resolve `path` under ROOTFS and confirm it sits under an allowed root.

    Raises ValueError (surfaced to the caller as a tool error) when file reads
    are disabled or the target escapes the allowlist.
    """
    roots = _allowed_roots()
    if not roots:
        raise ValueError("file reads are disabled (NODE_STATS_READABLE_ROOTS is empty)")
    target = Path(ROOTFS).joinpath(path.lstrip("/")).resolve()
    if not any(target == root or root in target.parents for root in roots):
        raise ValueError(f"path {path!r} is outside the readable-root allowlist")
    return target


def get_cpu_info() -> dict[str, Any]:
    """CPU utilization, core counts, and per-core percentages for this node."""
    return {
        "percent": psutil.cpu_percent(interval=0.3),
        "per_core_percent": psutil.cpu_percent(interval=0.3, percpu=True),
        "logical_cores": psutil.cpu_count(),
        "physical_cores": psutil.cpu_count(logical=False),
        "load_avg_1_5_15": list(psutil.getloadavg()),
    }


def get_memory_info() -> dict[str, Any]:
    """Virtual and swap memory for this node (bytes and percent used)."""
    return {"virtual": psutil.virtual_memory()._asdict(), "swap": psutil.swap_memory()._asdict()}


def get_disk_info() -> dict[str, Any]:
    """Usage and mount info for the node's real filesystems (under ROOTFS)."""
    partitions = []
    for part in psutil.disk_partitions(all=False):
        mount = Path(ROOTFS).joinpath(part.mountpoint.lstrip("/"))
        try:
            usage = psutil.disk_usage(str(mount))._asdict()
        except (PermissionError, FileNotFoundError, OSError):
            usage = {}
        partitions.append(
            {
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "usage": usage,
            }
        )
    return {"partitions": partitions}


def get_network_info() -> dict[str, Any]:
    """Aggregate and per-interface network I/O counters for this node.

    Reflects the node only when the pod runs with hostNetwork; otherwise these
    are the pod's own interface counters.
    """
    return {
        "total": psutil.net_io_counters()._asdict(),
        "per_interface": {
            name: c._asdict() for name, c in psutil.net_io_counters(pernic=True).items()
        },
    }


def get_top_processes(limit: int = 10, sort_by: str = "cpu") -> dict[str, Any]:
    """Top processes by 'cpu' or 'memory'. Needs hostPID to see node processes."""
    if sort_by not in ("cpu", "memory"):
        raise ValueError("sort_by must be 'cpu' or 'memory'")
    procs = []
    for proc in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_percent"]):
        procs.append(proc.info)
    key = "cpu_percent" if sort_by == "cpu" else "memory_percent"
    procs.sort(key=lambda p: p.get(key) or 0.0, reverse=True)
    return {"sort_by": sort_by, "processes": procs[: max(1, min(limit, 100))]}


def get_system_snapshot() -> dict[str, Any]:
    """One-shot node overview: cpu, memory, load, boot time, uptime, users."""
    boot = psutil.boot_time()
    now = time.time()
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "memory": psutil.virtual_memory()._asdict(),
        "load_avg_1_5_15": list(psutil.getloadavg()),
        "boot_time_epoch": boot,
        "uptime_seconds": now - boot,
        "logged_in_users": [u._asdict() for u in psutil.users()],
    }


def stat_path(path: str) -> dict[str, Any]:
    """Metadata (size, mode, mtime, type) for a path under the readable-root allowlist."""
    target = _resolve_readable(path)
    st = target.stat()
    return {
        "path": path,
        "exists": True,
        "size_bytes": st.st_size,
        "mode_octal": oct(st.st_mode & 0o777),
        "mtime_epoch": st.st_mtime,
        "is_dir": target.is_dir(),
        "is_file": target.is_file(),
    }


def read_text_head(path: str, max_bytes: int = _MAX_READ_BYTES) -> dict[str, Any]:
    """Read up to max_bytes of a text file under the readable-root allowlist (capped)."""
    target = _resolve_readable(path)
    cap = max(1, min(max_bytes, _MAX_READ_BYTES))
    data = target.read_bytes()[:cap]
    return {
        "path": path,
        "bytes_returned": len(data),
        "truncated": target.stat().st_size > len(data),
        "text": data.decode("utf-8", errors="replace"),
    }


# Register each tool without rebinding its name, so the plain callables stay
# directly invokable (tests call them; the mcp SDK's decorator return type has
# varied across versions, so we don't rely on it).
for _tool in (
    get_cpu_info,
    get_memory_info,
    get_disk_info,
    get_network_info,
    get_top_processes,
    get_system_snapshot,
    stat_path,
    read_text_head,
):
    mcp.tool()(_tool)


def main() -> None:
    """Run the MCP server over streamable-HTTP (endpoint served at /mcp)."""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
