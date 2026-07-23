"""FastMCP server exposing node-local host introspection over streamable-HTTP.

Read-only by construction: every tool is a read, no tool mutates the host. True
node stats (not the pod's cgroup view) rely on the deployment giving the pod
the host's namespaces - hostPID for processes, hostNetwork for net counters -
and CPU/memory come from the non-namespaced /proc/{stat,meminfo} directly. Disk
reads resolve under ROOTFS (the host root, mounted read-only at /host in the
pod). The k3s inventory tools read the Kubernetes API through a host-mounted
k3s admin kubeconfig when available, with a service-account fallback, and stay
read-only. See the deploy bundle in coilyco-bridge/deploy/services/node-stats-mcp.

File introspection is prefix-allowlisted, never arbitrary: stat_path and
read_text_head resolve the real path and refuse anything outside
NODE_STATS_READABLE_ROOTS (empty by default = file reads denied). This is the
enum-not-path discipline from the upstream node-introspection example,
generalized to a root allowlist, so the tool cannot be walked into /host/root/.ssh.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import ssl
import stat
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psutil
from mcp.server.fastmcp import FastMCP

# Host root inside the pod. The deployment mounts the node's / read-only at
# /host and sets ROOTFS=/host. Bare local runs leave it at / (the real root).
ROOTFS = os.environ.get("ROOTFS", "/")

# Colon-separated prefixes the file tools may read under, resolved against the
# real (symlink-collapsed) path. Empty = file reads are denied. The prefixes are
# interpreted inside ROOTFS, e.g. "/etc:/var/log" with ROOTFS=/host permits
# /host/etc and /host/var/log only.
_READABLE_ROOTS = [r for r in os.environ.get("NODE_STATS_READABLE_ROOTS", "").split(":") if r]

# Hard cap on read_text_head so a tool call can never stream a huge file.
_MAX_READ_BYTES = int(os.environ.get("NODE_STATS_MAX_READ_BYTES", "65536"))
_K8S_TIMEOUT_SECONDS = float(os.environ.get("NODE_STATS_K8S_TIMEOUT_SECONDS", "3"))
_KUBECONFIG_PATH = os.environ.get("NODE_STATS_KUBECONFIG", "/etc/rancher/k3s/k3s.yaml")

_DISK_WARN_PERCENT = float(os.environ.get("NODE_STATS_DISK_WARN_PERCENT", "80"))
_DISK_CRITICAL_PERCENT = float(os.environ.get("NODE_STATS_DISK_CRITICAL_PERCENT", "85"))

_DEFAULT_PRESSURE_PATHS = (
    "/home",
    "/srv",
    "/tmp",
    "/var/tmp",
    "/var/log",
    "/var/log/journal",
    "/var/lib/rancher/k3s",
    "/var/lib/rancher/k3s/agent/containerd",
    "/var/lib/kubelet",
    "/var/lib/containerd",
    "/var/lib/snapd",
)
_PRESSURE_PATHS = tuple(
    p
    for p in os.environ.get("NODE_STATS_PRESSURE_PATHS", ":".join(_DEFAULT_PRESSURE_PATHS)).split(
        ":"
    )
    if p
)
_MAX_DU_ENTRIES = int(os.environ.get("NODE_STATS_MAX_DU_ENTRIES", "200000"))
_MAX_DU_TOTAL_ENTRIES = int(os.environ.get("NODE_STATS_MAX_DU_TOTAL_ENTRIES", "200000"))
_DU_TIMEOUT_SECONDS = float(os.environ.get("NODE_STATS_DU_TIMEOUT_SECONDS", "10"))

mcp = FastMCP(
    "node-stats",
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8080")),
)


@dataclass(frozen=True)
class _K8sTransport:
    base_url: str
    headers: dict[str, str]
    ssl_context: ssl.SSLContext | None
    source: str


@dataclass(frozen=True)
class _K8sAuthRef:
    namespace: str | None
    pod: str | None
    container: str | None
    pod_uid: str | None
    container_id: str | None
    matched_by: str | None


@dataclass(frozen=True)
class _CgroupRefs:
    paths: list[str]
    pod_uid: str | None
    container_ids: list[str]


@dataclass
class _ScanBudget:
    """Mutable limits shared by every path in one pressure scan."""

    max_entries: int
    deadline: float
    entries_scanned: int = 0


PodContainerMatch = tuple[dict[str, Any], dict[str, Any]]
PodContainerIndex = dict[str, PodContainerMatch]
PodUidIndex = dict[str, dict[str, Any]]


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _host_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        root = Path(ROOTFS)
        try:
            candidate.relative_to(root)
        except ValueError:
            return root.joinpath(candidate.relative_to("/"))
        return candidate
    return Path(ROOTFS).joinpath(candidate)


def _read_host_text(path: str | Path) -> str | None:
    try:
        return _host_path(path).read_text()
    except OSError:
        return None


def _load_kubeconfig_transport() -> _K8sTransport | None:
    kubeconfig = _host_path(_KUBECONFIG_PATH)
    if not kubeconfig.exists():
        return None

    config = kubeconfig.read_text()
    server_match = re.search(r"^\s*server:\s*(\S+)\s*$", config, re.MULTILINE)
    if not server_match:
        return None
    base_url = _strip_quotes(server_match.group(1))

    def _first(pattern: str) -> str | None:
        match = re.search(pattern, config, re.MULTILINE)
        if not match:
            return None
        return _strip_quotes(match.group(1))

    ca_data = _first(r"^\s*certificate-authority-data:\s*(\S+)\s*$")
    ca_file = _first(r"^\s*certificate-authority:\s*(\S+)\s*$")
    client_cert_data = _first(r"^\s*client-certificate-data:\s*(\S+)\s*$")
    client_cert_file = _first(r"^\s*client-certificate:\s*(\S+)\s*$")
    client_key_data = _first(r"^\s*client-key-data:\s*(\S+)\s*$")
    client_key_file = _first(r"^\s*client-key:\s*(\S+)\s*$")
    token = _first(r"^\s*token:\s*(\S+)\s*$")

    ssl_context = ssl.create_default_context()
    if ca_data:
        ssl_context.load_verify_locations(cadata=base64.b64decode(ca_data).decode("utf-8"))
    elif ca_file:
        ssl_context.load_verify_locations(cafile=str(_host_path(ca_file)))

    if client_cert_data and client_key_data:
        cert_file = tempfile.NamedTemporaryFile(prefix="node-stats-k8s-cert-", delete=False)
        key_file = tempfile.NamedTemporaryFile(prefix="node-stats-k8s-key-", delete=False)
        try:
            cert_file.write(base64.b64decode(client_cert_data))
            cert_file.flush()
            key_file.write(base64.b64decode(client_key_data))
            key_file.flush()
        finally:
            cert_file.close()
            key_file.close()
        ssl_context.load_cert_chain(certfile=cert_file.name, keyfile=key_file.name)
    elif client_cert_file and client_key_file:
        ssl_context.load_cert_chain(
            certfile=str(_host_path(client_cert_file)),
            keyfile=str(_host_path(client_key_file)),
        )

    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return _K8sTransport(
        base_url=base_url.rstrip("/"),
        headers=headers,
        ssl_context=ssl_context,
        source=str(kubeconfig),
    )


def _load_service_account_transport() -> _K8sTransport | None:
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    if not token_path.exists():
        return None

    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        return None

    headers = {"Authorization": f"Bearer {token_path.read_text().strip()}"}
    ssl_context = ssl.create_default_context()
    if ca_path.exists():
        ssl_context.load_verify_locations(cafile=str(ca_path))
    return _K8sTransport(
        base_url=f"https://{host}:{port}",
        headers=headers,
        ssl_context=ssl_context,
        source=str(token_path),
    )


def _k8s_transport() -> _K8sTransport | None:
    for loader in (_load_kubeconfig_transport, _load_service_account_transport):
        try:
            transport = loader()
        except (OSError, ValueError, binascii.Error, ssl.SSLError):
            continue
        if transport is not None:
            return transport
    return None


def _k8s_request(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    transport = _k8s_transport()
    if transport is None:
        raise ValueError("Kubernetes API is unavailable")
    query = f"?{urlencode(params)}" if params else ""
    req = Request(
        f"{transport.base_url}{path}{query}",
        headers={**transport.headers, "Accept": "application/json"},
    )
    with urlopen(req, timeout=_K8S_TIMEOUT_SECONDS, context=transport.ssl_context) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _k8s_list(path: str) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    token: str | None = None
    while True:
        params = {"limit": "500"}
        if token:
            params["continue"] = token
        try:
            payload = _k8s_request(path, params)
        except (HTTPError, URLError, ValueError) as exc:
            errors.append(str(exc))
            break
        items.extend([item for item in payload.get("items", []) if isinstance(item, dict)])
        token = payload.get("metadata", {}).get("continue") or None
        if not token:
            break
    return items, errors


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_age(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    remaining = max(0, int(seconds))
    pieces: list[str] = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if remaining >= size:
            count, remaining = divmod(remaining, size)
            pieces.append(f"{count}{unit}")
    if not pieces:
        pieces.append(f"{remaining}s")
    return "".join(pieces[:2])


def _parse_quantity(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"(?i)(\d+(?:\.\d+)?)([kmgtpe]i?|)", value.strip())
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2).lower()
    binary = {
        "ki": 1024,
        "mi": 1024**2,
        "gi": 1024**3,
        "ti": 1024**4,
        "pi": 1024**5,
        "ei": 1024**6,
    }
    decimal = {
        "k": 1000,
        "m": 1000**2,
        "g": 1000**3,
        "t": 1000**4,
        "p": 1000**5,
        "e": 1000**6,
    }
    if suffix in binary:
        return int(number * binary[suffix])
    if suffix in decimal:
        return int(number * decimal[suffix])
    return int(number)


def _normalize_container_id(container_id: str | None) -> str | None:
    if not container_id:
        return None
    value = container_id.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    return value or None


_POD_UID_RE = re.compile(
    r"pod(?P<uid>[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})",
    re.IGNORECASE,
)
_CONTAINER_ID_RE = re.compile(
    r"(?:containerd|cri-containerd|cri-o|crio|docker)[-:/](?P<id>[0-9a-f]{12,64})(?:\.scope)?",
    re.IGNORECASE,
)


def _parse_cgroup_paths(text: str | None) -> _CgroupRefs:
    if not text:
        return _CgroupRefs(paths=[], pod_uid=None, container_ids=[])
    paths: list[str] = []
    container_ids: list[str] = []
    pod_uid: str | None = None
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        path = parts[2].strip()
        if not path:
            continue
        paths.append(path)
        if pod_uid is None:
            pod_match = _POD_UID_RE.search(path)
            if pod_match:
                pod_uid = pod_match.group("uid")
        for match in _CONTAINER_ID_RE.finditer(path):
            container_ids.append(match.group("id"))
        scope_match = re.search(r"([0-9a-f]{12,64})(?:\.scope)?$", path, re.IGNORECASE)
        if scope_match:
            container_ids.append(scope_match.group(1))
    return _CgroupRefs(
        paths=paths,
        pod_uid=pod_uid,
        container_ids=list(dict.fromkeys(container_ids)),
    )


def _normalize_pod(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata", {})
    status = item.get("status", {})
    spec = item.get("spec", {})
    containers: list[dict[str, Any]] = []
    restart_total = 0
    statuses = {
        c.get("name"): c for c in status.get("containerStatuses", []) if isinstance(c, dict)
    }
    for container in spec.get("containers", []):
        if not isinstance(container, dict):
            continue
        name = container.get("name")
        container_status = statuses.get(name, {})
        restart_count = int(container_status.get("restartCount") or 0)
        restart_total += restart_count
        containers.append(
            {
                "name": name,
                "image": container.get("image"),
                "ready": bool(container_status.get("ready")),
                "restart_count": restart_count,
                "container_id": _normalize_container_id(container_status.get("containerID")),
                "state": next(iter(container_status.get("state", {})), None),
            }
        )
    created = _parse_timestamp(metadata.get("creationTimestamp"))
    now = datetime.now(UTC)
    age_seconds = (now - created).total_seconds() if created else None
    return {
        "namespace": metadata.get("namespace"),
        "pod": metadata.get("name"),
        "phase": status.get("phase"),
        "node": spec.get("nodeName"),
        "restart_count": restart_total,
        "pod_ip": status.get("podIP"),
        "created_at": created.isoformat() if created else None,
        "age_seconds": int(age_seconds) if age_seconds is not None else None,
        "age": _format_age(age_seconds),
        "containers": containers,
    }


def _pod_indexes(pods: list[dict[str, Any]]) -> tuple[PodContainerIndex, PodUidIndex]:
    by_container_id: PodContainerIndex = {}
    by_pod_uid: PodUidIndex = {}
    for pod in pods:
        uid = None
        if isinstance(pod, dict):
            uid = pod.get("uid")
        if not uid:
            continue
        by_pod_uid[str(uid)] = pod
        for container in pod.get("containers", []):
            container_id = container.get("container_id")
            if container_id:
                by_container_id[str(container_id)] = (pod, container)
    return by_container_id, by_pod_uid


def _pod_uid_from_item(item: dict[str, Any]) -> str | None:
    metadata = item.get("metadata", {})
    uid = metadata.get("uid")
    return str(uid) if uid else None


def _normalize_pod_for_index(item: dict[str, Any]) -> dict[str, Any]:
    pod = _normalize_pod(item)
    pod["uid"] = _pod_uid_from_item(item)
    return pod


def _k8s_pod_inventory() -> tuple[list[dict[str, Any]], PodContainerIndex, PodUidIndex, list[str]]:
    items, errors = _k8s_list("/api/v1/pods")
    pods = [_normalize_pod_for_index(item) for item in items]
    by_container_id, by_pod_uid = _pod_indexes(pods)
    return pods, by_container_id, by_pod_uid, errors


def _match_pod_container(
    refs: _CgroupRefs,
    by_container_id: PodContainerIndex,
    by_pod_uid: PodUidIndex,
) -> _K8sAuthRef | None:
    for container_id in refs.container_ids:
        container_match = by_container_id.get(str(container_id))
        if container_match is not None:
            pod, container = container_match
            return _K8sAuthRef(
                namespace=pod.get("namespace"),
                pod=pod.get("pod"),
                container=container.get("name"),
                pod_uid=pod.get("uid"),
                container_id=str(container_id),
                matched_by="container_id",
            )
    pod_uid = refs.pod_uid
    if pod_uid:
        pod_entry = by_pod_uid.get(str(pod_uid))
        if pod_entry is not None:
            container_name = None
            if len(pod_entry.get("containers", [])) == 1:
                container_name = pod_entry["containers"][0].get("name")
            return _K8sAuthRef(
                namespace=pod_entry.get("namespace"),
                pod=pod_entry.get("pod"),
                container=container_name,
                pod_uid=pod_entry.get("uid"),
                container_id=None,
                matched_by="pod_uid",
            )
    return None


def _iter_host_processes() -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_percent"]):
        info = dict(proc.info)
        try:
            rss_bytes = proc.memory_info().rss
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            rss_bytes = None
        info["rss_bytes"] = rss_bytes
        processes.append(info)
    return processes


def _process_inventory(sort_by: str = "memory") -> tuple[list[dict[str, Any]], list[str]]:
    _, by_container_id, by_pod_uid, errors = _k8s_pod_inventory()
    processes = []
    for proc in _iter_host_processes():
        pid = proc.get("pid")
        cgroup_text = _read_host_text(f"/proc/{pid}/cgroup") if pid else None
        refs = _parse_cgroup_paths(cgroup_text)
        attribution = _match_pod_container(refs, by_container_id, by_pod_uid)
        if attribution:
            proc["kubernetes"] = {
                "namespace": attribution.namespace,
                "pod": attribution.pod,
                "container": attribution.container,
                "pod_uid": attribution.pod_uid,
                "container_id": attribution.container_id,
                "matched_by": attribution.matched_by,
            }
        else:
            proc["kubernetes"] = None
        proc["cgroup"] = {
            "pod_uid": refs.pod_uid,
            "container_ids": refs.container_ids,
            "paths": refs.paths,
        }
        processes.append(proc)
    if sort_by == "memory":
        processes.sort(key=lambda p: p.get("rss_bytes") or 0, reverse=True)
    else:
        processes.sort(key=lambda p: p.get("cpu_percent") or 0.0, reverse=True)
    return processes, errors


def _k8s_pod_metrics() -> tuple[list[dict[str, Any]], list[str]]:
    metrics, errors = _k8s_list("/apis/metrics.k8s.io/v1beta1/pods")
    return metrics, errors


def _k8s_container_memory_from_metrics() -> tuple[list[dict[str, Any]], list[str]]:
    metrics, errors = _k8s_pod_metrics()
    containers: list[dict[str, Any]] = []
    for item in metrics:
        metadata = item.get("metadata", {})
        for container in item.get("containers", []):
            usage = container.get("usage", {})
            memory_bytes = _parse_quantity(usage.get("memory"))
            containers.append(
                {
                    "namespace": metadata.get("namespace"),
                    "pod": metadata.get("name"),
                    "container": container.get("name"),
                    "memory_bytes": memory_bytes,
                    "source": "metrics-server",
                }
            )
    containers.sort(key=lambda entry: entry.get("memory_bytes") or 0, reverse=True)
    return containers, errors


def _k8s_container_memory_from_processes() -> tuple[list[dict[str, Any]], list[str]]:
    _, by_container_id, by_pod_uid, errors = _k8s_pod_inventory()
    container_totals: dict[tuple[str | None, str | None, str | None], int] = defaultdict(int)
    for proc in _iter_host_processes():
        pid = proc.get("pid")
        if not pid:
            continue
        cgroup_text = _read_host_text(f"/proc/{pid}/cgroup")
        refs = _parse_cgroup_paths(cgroup_text)
        attribution = _match_pod_container(refs, by_container_id, by_pod_uid)
        if not attribution:
            continue
        key = (attribution.namespace, attribution.pod, attribution.container)
        container_totals[key] += int(proc.get("rss_bytes") or 0)

    containers = [
        {
            "namespace": namespace,
            "pod": pod,
            "container": container,
            "memory_bytes": memory_bytes,
            "source": "cgroup-rss",
        }
        for (namespace, pod, container), memory_bytes in container_totals.items()
    ]
    containers.sort(key=lambda entry: entry.get("memory_bytes") or 0, reverse=True)
    return containers, errors


def _k8s_container_memory() -> dict[str, Any]:
    containers, errors = _k8s_container_memory_from_metrics()
    source = "metrics-server"
    if not containers:
        containers, fallback_errors = _k8s_container_memory_from_processes()
        errors.extend(fallback_errors)
        source = "cgroup-rss" if containers else "unavailable"
    return {"source": source, "containers": containers, "errors": errors}


def get_k3s_pods() -> dict[str, Any]:
    """Namespace, pod, container, and age inventory from the Kubernetes API."""
    pods, _, _, errors = _k8s_pod_inventory()
    return {"pods": pods, "errors": errors}


def get_k3s_container_memory() -> dict[str, Any]:
    """Approximate k3s container memory from metrics-server or host cgroup RSS."""
    return _k8s_container_memory()


def get_k3s_process_attribution(limit: int = 10, sort_by: str = "memory") -> dict[str, Any]:
    """Top host processes with cgroup-backed pod/container attribution when available."""
    if sort_by not in ("cpu", "memory"):
        raise ValueError("sort_by must be 'cpu' or 'memory'")
    processes, errors = _process_inventory(sort_by=sort_by)
    cap = max(1, min(limit, 100))
    return {"sort_by": sort_by, "processes": processes[:cap], "errors": errors}


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


def _allocated_bytes(path_stat: os.stat_result) -> int:
    blocks = getattr(path_stat, "st_blocks", 0)
    if blocks:
        return int(blocks) * 512
    return int(path_stat.st_size)


def _public_host_path(path: Path) -> str:
    try:
        relative = path.relative_to(Path(ROOTFS))
    except ValueError:
        return str(path)
    return "/" + str(relative)


def _filesystem_pressure(path: str) -> dict[str, Any]:
    target = _host_path(path)
    fs = os.statvfs(target)
    total = fs.f_frsize * fs.f_blocks
    free = fs.f_frsize * fs.f_bfree
    available = fs.f_frsize * fs.f_bavail
    reserved = free - available
    pressure_used = total - available
    used_percent = (pressure_used / total * 100.0) if total else 0.0
    warn_used = int(total * (_DISK_WARN_PERCENT / 100.0))
    critical_used = int(total * (_DISK_CRITICAL_PERCENT / 100.0))
    inode_percent = ((fs.f_files - fs.f_favail) / fs.f_files) * 100.0 if fs.f_files else 0.0
    if used_percent >= _DISK_CRITICAL_PERCENT:
        status = "critical"
    elif used_percent >= _DISK_WARN_PERCENT:
        status = "warning"
    else:
        status = "ok"
    return {
        "path": path,
        "total_bytes": total,
        "free_bytes": free,
        "available_bytes": available,
        "reserved_bytes": reserved,
        "pressure_used_bytes": pressure_used,
        "used_percent": used_percent,
        "status": status,
        "warn_percent": _DISK_WARN_PERCENT,
        "critical_percent": _DISK_CRITICAL_PERCENT,
        "bytes_until_warn": warn_used - pressure_used,
        "bytes_until_critical": critical_used - pressure_used,
        "bytes_over_warn": max(0, pressure_used - warn_used),
        "bytes_over_critical": max(0, pressure_used - critical_used),
        "inodes_total": fs.f_files,
        "inodes_free": fs.f_ffree,
        "inodes_available": fs.f_favail,
        "inodes_used_percent": inode_percent,
    }


def _scan_result(path: Path, exists: bool) -> dict[str, Any]:
    return {
        "path": _public_host_path(path),
        "exists": exists,
        "size_bytes": 0,
        "entries_scanned": 0,
        "permission_errors": 0,
        "scan_errors": 0,
        "errors": [],
        "skipped_different_filesystem": 0,
        "truncated": False,
        "truncation_reason": None,
        "timed_out": False,
    }


def _stop_reason(budget: _ScanBudget) -> str | None:
    if time.monotonic() >= budget.deadline:
        return "timeout"
    if budget.entries_scanned >= budget.max_entries:
        return "global_entry_budget"
    return None


def _record_scan_error(result: dict[str, Any], exc: OSError) -> None:
    result["scan_errors"] += 1
    if len(result["errors"]) < 10:
        result["errors"].append(str(exc))


def _du_path(
    path: Path, max_entries: int, root_device: int | None, budget: _ScanBudget
) -> dict[str, Any]:
    try:
        exists = path.exists()
    except OSError as exc:
        exists = False
        result = _scan_result(path, exists=False)
        _record_scan_error(result, exc)
        return result
    if not exists:
        return _scan_result(path, exists=False)

    result = _scan_result(path, exists=True)
    stack = [path]
    while stack:
        if reason := _stop_reason(budget):
            result["truncated"] = True
            result["truncation_reason"] = reason
            result["timed_out"] = reason == "timeout"
            break
        current = stack.pop()
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            continue
        except PermissionError:
            result["permission_errors"] += 1
            continue
        except OSError as exc:
            _record_scan_error(result, exc)
            continue
        if root_device is not None and current_stat.st_dev != root_device:
            result["skipped_different_filesystem"] += 1
            continue
        result["size_bytes"] += _allocated_bytes(current_stat)
        result["entries_scanned"] += 1
        budget.entries_scanned += 1
        if result["entries_scanned"] >= max_entries:
            result["truncated"] = True
            result["truncation_reason"] = "per_path_entry_cap"
            break
        if budget.entries_scanned >= budget.max_entries:
            result["truncated"] = True
            result["truncation_reason"] = "global_entry_budget"
            break
        if not stat.S_ISDIR(current_stat.st_mode):
            continue
        try:
            with os.scandir(current) as children:
                for child in children:
                    if reason := _stop_reason(budget):
                        result["truncated"] = True
                        result["truncation_reason"] = reason
                        result["timed_out"] = reason == "timeout"
                        break
                    stack.append(Path(child.path))
        except PermissionError:
            result["permission_errors"] += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            _record_scan_error(result, exc)

    return result


def _coalesced_pressure_paths() -> tuple[list[Path], list[dict[str, str]]]:
    """Keep only shallow fixed roots so nested paths are not scanned twice."""
    configured = [(_host_path(path), path) for path in _PRESSURE_PATHS]
    selected: list[Path] = []
    skipped: list[dict[str, str]] = []
    for path, configured_path in sorted(configured, key=lambda item: len(item[0].parts)):
        parent = next((candidate for candidate in selected if candidate in path.parents), None)
        if parent is None and path not in selected:
            selected.append(path)
            continue
        skipped.append(
            {
                "path": configured_path,
                "covered_by": _public_host_path(parent if parent is not None else path),
            }
        )
    return selected, skipped


def _pressure_path_usage(limit: int, max_entries_per_path: int) -> dict[str, Any]:
    """Run the fixed-path traversal in a worker thread with shared limits."""
    root = _host_path("/")
    try:
        root_device = root.stat().st_dev
    except FileNotFoundError:
        root_device = None
    entry_budget = max(1, min(max_entries_per_path, _MAX_DU_ENTRIES))
    total_budget = max(1, _MAX_DU_TOTAL_ENTRIES)
    timeout_seconds = max(0.001, _DU_TIMEOUT_SECONDS)
    budget = _ScanBudget(
        max_entries=total_budget,
        deadline=time.monotonic() + timeout_seconds,
    )
    scan_paths, skipped_nested_paths = _coalesced_pressure_paths()
    paths = [_du_path(path, entry_budget, root_device, budget) for path in scan_paths]
    paths.sort(key=lambda item: item["size_bytes"], reverse=True)
    timed_out = any(path["timed_out"] for path in paths)
    return {
        "paths": paths[: max(1, min(limit, 100))],
        "configured_paths": list(_PRESSURE_PATHS),
        "skipped_nested_paths": skipped_nested_paths,
        "max_entries_per_path": entry_budget,
        "max_total_entries": total_budget,
        "total_entries_scanned": budget.entries_scanned,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "truncated": any(path["truncated"] for path in paths),
        "scan_errors": sum(path["scan_errors"] for path in paths),
        "same_filesystem_only": True,
    }


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


def get_filesystem_pressure() -> dict[str, Any]:
    """Root filesystem runway, inode use, and configurable pressure thresholds.

    This is the fast disk-pressure view for the node root mounted at ROOTFS. The
    default thresholds are 80/85 percent because kubelet image garbage collection
    starts to matter around that range on a single-filesystem k3s node.
    """
    return {"root": _filesystem_pressure("/")}


async def get_pressure_path_usage(
    limit: int = 20, max_entries_per_path: int = _MAX_DU_ENTRIES
) -> dict[str, Any]:
    """Bounded usage scan for fixed host paths that commonly drive node pressure.

    Traversal runs in a worker thread, leaving the MCP event loop available for
    fast tools such as get_filesystem_pressure. Per-path, total-entry, and
    wall-clock limits return truncation, timeout, and error metadata. Nested
    configured paths are coalesced so the same tree is not walked twice. The
    caller can tune only the per-path cap and result count, never a raw path.
    """
    return await asyncio.to_thread(_pressure_path_usage, limit, max_entries_per_path)


def get_network_info() -> dict[str, Any]:
    """Aggregate and per-interface network I/O counters for this node.

    Reflects the node only when the pod runs with hostNetwork. Otherwise these
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
# directly invokable. Tests call them, and the mcp SDK's decorator return type has
# varied across versions, so we don't rely on it).
for _tool in (
    get_cpu_info,
    get_memory_info,
    get_disk_info,
    get_filesystem_pressure,
    get_pressure_path_usage,
    get_network_info,
    get_top_processes,
    get_system_snapshot,
    get_k3s_pods,
    get_k3s_container_memory,
    get_k3s_process_attribution,
    stat_path,
    read_text_head,
):
    mcp.tool()(_tool)


def main() -> None:
    """Run the MCP server over streamable-HTTP (endpoint served at /mcp)."""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
