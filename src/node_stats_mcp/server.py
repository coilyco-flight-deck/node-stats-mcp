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
_DEFAULT_K3S_VOLUME_ROOTS = ("/var/lib/rancher/k3s/storage",)
_K3S_VOLUME_ROOTS = tuple(
    path
    for path in os.environ.get(
        "NODE_STATS_K3S_VOLUME_ROOTS", ":".join(_DEFAULT_K3S_VOLUME_ROOTS)
    ).split(":")
    if path
)
_MAX_K3S_VOLUME_PATHS = int(os.environ.get("NODE_STATS_MAX_K3S_VOLUME_PATHS", "1000"))

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
_MAX_PRESSURE_CHILDREN_PER_ROOT = int(
    os.environ.get("NODE_STATS_MAX_PRESSURE_CHILDREN_PER_ROOT", "1000")
)
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
    raw_path = os.fspath(path)
    candidate = Path(raw_path)
    root = Path(ROOTFS)
    if raw_path.startswith("/"):
        try:
            candidate.relative_to(root)
        except ValueError:
            return root.joinpath(raw_path.lstrip("/"))
        return candidate
    if candidate.is_absolute():
        return candidate
    return root.joinpath(candidate)


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


def _pod_pvc_mounts(items: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    mounts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        namespace = metadata.get("namespace")
        pod = metadata.get("name")
        if not namespace or not pod:
            continue
        claims_by_volume: dict[str, str] = {}
        for volume in spec.get("volumes", []):
            if not isinstance(volume, dict):
                continue
            claim = volume.get("persistentVolumeClaim")
            if not isinstance(claim, dict):
                continue
            volume_name = volume.get("name")
            claim_name = claim.get("claimName")
            if volume_name and claim_name:
                claims_by_volume[str(volume_name)] = str(claim_name)
        for container_type, containers in (
            ("container", spec.get("containers", [])),
            ("init_container", spec.get("initContainers", [])),
            ("ephemeral_container", spec.get("ephemeralContainers", [])),
        ):
            for container in containers:
                if not isinstance(container, dict):
                    continue
                for mount in container.get("volumeMounts", []):
                    if not isinstance(mount, dict):
                        continue
                    volume_name = mount.get("name")
                    claim_name = claims_by_volume.get(str(volume_name))
                    if not claim_name:
                        continue
                    mounts[(str(namespace), claim_name)].append(
                        {
                            "pod": str(pod),
                            "pod_uid": _pod_uid_from_item(item),
                            "phase": status.get("phase"),
                            "container": container.get("name"),
                            "container_type": container_type,
                            "volume": volume_name,
                            "mount_path": mount.get("mountPath"),
                            "read_only": bool(mount.get("readOnly")),
                        }
                    )
    return mounts


def _normalize_pvc(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    return {
        "namespace": metadata.get("namespace"),
        "persistent_volume_claim": metadata.get("name"),
        "persistent_volume_claim_uid": metadata.get("uid"),
        "persistent_volume": spec.get("volumeName"),
        "storage_class": spec.get("storageClassName"),
        "phase": status.get("phase"),
        "requested_bytes": _parse_quantity(
            spec.get("resources", {}).get("requests", {}).get("storage")
        ),
        "capacity_bytes": _parse_quantity(status.get("capacity", {}).get("storage")),
    }


def _normalize_pv(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    claim = spec.get("claimRef", {})
    host_path = spec.get("hostPath", {}).get("path")
    local_path = spec.get("local", {}).get("path")
    return {
        "persistent_volume": metadata.get("name"),
        "persistent_volume_uid": metadata.get("uid"),
        "namespace": claim.get("namespace"),
        "persistent_volume_claim": claim.get("name"),
        "storage_class": spec.get("storageClassName"),
        "phase": status.get("phase"),
        "capacity_bytes": _parse_quantity(spec.get("capacity", {}).get("storage")),
        "local_path": host_path or local_path,
        "local_path_source": "hostPath" if host_path else "local" if local_path else None,
    }


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


async def get_k3s_volume_usage(
    limit: int = 20, max_entries_per_volume: int = _MAX_DU_ENTRIES
) -> dict[str, Any]:
    """Bounded local-volume disk usage joined to PVCs, PVs, namespaces, and pod mounts.

    The server scans only local paths beneath NODE_STATS_K3S_VOLUME_ROOTS. Callers
    can tune result and entry caps but cannot supply a filesystem path. The API
    reads and traversal run in a worker thread so fast node tools stay responsive.
    """
    return await asyncio.to_thread(_k3s_volume_usage, limit, max_entries_per_volume)


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
    return "/" + relative.as_posix()


def _filesystem_pressure(path: str) -> dict[str, Any]:
    target = _host_path(path)
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        disk = psutil.disk_usage(str(target))
        total = disk.total
        free = disk.free
        available = disk.free
        reserved = 0
        inodes_total = 0
        inodes_free = 0
        inodes_available = 0
    else:
        fs = statvfs(target)
        total = fs.f_frsize * fs.f_blocks
        free = fs.f_frsize * fs.f_bfree
        available = fs.f_frsize * fs.f_bavail
        reserved = free - available
        inodes_total = fs.f_files
        inodes_free = fs.f_ffree
        inodes_available = fs.f_favail
    pressure_used = total - available
    used_percent = (pressure_used / total * 100.0) if total else 0.0
    warn_used = int(total * (_DISK_WARN_PERCENT / 100.0))
    critical_used = int(total * (_DISK_CRITICAL_PERCENT / 100.0))
    inode_percent = (
        ((inodes_total - inodes_available) / inodes_total) * 100.0 if inodes_total else 0.0
    )
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
        "inodes_total": inodes_total,
        "inodes_free": inodes_free,
        "inodes_available": inodes_available,
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


def _pressure_root_result(path: Path, exists: bool) -> dict[str, Any]:
    return {
        **_scan_result(path, exists),
        "children": [],
        "children_discovered": 0,
        "children_returned": 0,
        "child_results_truncated": False,
        "discovery_truncated": False,
        "discovery_truncation_reason": None,
        "discovery_timed_out": False,
    }


def _pressure_root_children(
    root: Path, max_children: int, deadline: float
) -> tuple[dict[str, Any], list[tuple[Path, int | None]]]:
    """Discover one level beneath one fixed root without following symlinks."""
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return _pressure_root_result(root, exists=False), []
    except PermissionError:
        result = _pressure_root_result(root, exists=False)
        result["permission_errors"] += 1
        return result, []
    except OSError as exc:
        result = _pressure_root_result(root, exists=False)
        _record_scan_error(result, exc)
        return result, []

    result = _pressure_root_result(root, exists=True)
    root_device = root_stat.st_dev
    if not stat.S_ISDIR(root_stat.st_mode):
        result["children_discovered"] = 1
        return result, [(root, root_device)]

    children: list[tuple[Path, int | None]] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    result["discovery_truncated"] = True
                    result["discovery_truncation_reason"] = "timeout"
                    result["discovery_timed_out"] = True
                    break
                if len(children) >= max_children:
                    result["discovery_truncated"] = True
                    result["discovery_truncation_reason"] = "child_cap"
                    break
                children.append((Path(entry.path), root_device))
    except PermissionError:
        result["permission_errors"] += 1
    except FileNotFoundError:
        result["exists"] = False
    except OSError as exc:
        _record_scan_error(result, exc)
    children.sort(key=lambda item: str(item[0]))
    result["children_discovered"] = len(children)
    return result, children


def _scan_pressure_children(
    children: list[tuple[Path, int | None]],
    max_entries_per_child: int,
    deadline: float,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Give every discovered child an independent fair share of remaining work."""
    total_budget = max(1, _MAX_DU_TOTAL_ENTRIES)
    if not children:
        return {}, {
            "max_entries_per_child": 0,
            "total_entries_scanned": 0,
            "time_slice_seconds": 0.0,
            "timed_out": False,
            "truncated": False,
            "scan_errors": 0,
            "permission_errors": 0,
            "skipped_different_filesystem": 0,
        }

    selected_children = children[:total_budget]
    unscanned_children = children[total_budget:]
    entry_budget = max(
        1,
        min(
            max_entries_per_child,
            _MAX_DU_ENTRIES,
            max(1, total_budget // len(selected_children)),
        ),
    )
    remaining_seconds = max(0.001, deadline - time.monotonic())
    time_slice_seconds = max(0.001, remaining_seconds / len(selected_children))
    scans: dict[str, dict[str, Any]] = {}
    total_entries_scanned = 0
    for path, root_device in selected_children:
        budget = _ScanBudget(
            max_entries=entry_budget,
            deadline=time.monotonic() + time_slice_seconds,
        )
        scan = _du_path(path, entry_budget, root_device, budget)
        scans[str(path)] = scan
        total_entries_scanned += budget.entries_scanned
    for path, _ in unscanned_children:
        scan = _scan_result(path, exists=True)
        scan["truncated"] = True
        scan["truncation_reason"] = "global_entry_budget"
        scans[str(path)] = scan
    return scans, {
        "max_entries_per_child": entry_budget,
        "total_entries_scanned": total_entries_scanned,
        "time_slice_seconds": time_slice_seconds,
        "timed_out": any(scan["timed_out"] for scan in scans.values()),
        "truncated": any(scan["truncated"] for scan in scans.values()),
        "scan_errors": sum(scan["scan_errors"] for scan in scans.values()),
        "permission_errors": sum(scan["permission_errors"] for scan in scans.values()),
        "skipped_different_filesystem": sum(
            scan["skipped_different_filesystem"] for scan in scans.values()
        ),
    }


def _pressure_path_usage(limit: int, max_entries_per_path: int) -> dict[str, Any]:
    """Attribute fixed pressure roots to their immediate children with fair limits."""
    child_limit = max(1, min(limit, 100))
    max_children_per_root = max(1, _MAX_PRESSURE_CHILDREN_PER_ROOT)
    total_budget = max(1, _MAX_DU_TOTAL_ENTRIES)
    timeout_seconds = max(0.001, _DU_TIMEOUT_SECONDS)
    request_deadline = time.monotonic() + timeout_seconds
    scan_paths, skipped_nested_paths = _coalesced_pressure_paths()
    discovery_time_slice_seconds = (
        max(0.001, timeout_seconds / (2 * len(scan_paths))) if scan_paths else 0.0
    )
    roots: list[tuple[dict[str, Any], list[tuple[Path, int | None]]]] = []
    for path in scan_paths:
        root, root_children = _pressure_root_children(
            path,
            max_children_per_root,
            time.monotonic() + discovery_time_slice_seconds,
        )
        roots.append((root, root_children))

    children = [
        root_children[index]
        for index in range(max((len(root_children) for _, root_children in roots), default=0))
        for _, root_children in roots
        if index < len(root_children)
    ]

    scans, scan_summary = _scan_pressure_children(
        children,
        max_entries_per_path,
        request_deadline,
    )
    paths: list[dict[str, Any]] = []
    for root, root_children in roots:
        child_scans = [scans[str(path)] for path, _ in root_children if str(path) in scans]
        child_scans.sort(key=lambda item: (-item["size_bytes"], item["path"]))
        root["children"] = child_scans[:child_limit]
        root["children_returned"] = len(root["children"])
        root["child_results_truncated"] = len(child_scans) > child_limit
        root["size_bytes"] = sum(child["size_bytes"] for child in child_scans)
        root["entries_scanned"] = sum(child["entries_scanned"] for child in child_scans)
        root["permission_errors"] += sum(child["permission_errors"] for child in child_scans)
        root["scan_errors"] += sum(child["scan_errors"] for child in child_scans)
        root["skipped_different_filesystem"] = sum(
            child["skipped_different_filesystem"] for child in child_scans
        )
        child_truncated = any(child["truncated"] for child in child_scans)
        root["truncated"] = root["discovery_truncated"] or child_truncated
        root["truncation_reason"] = root["discovery_truncation_reason"]
        if root["truncation_reason"] is None and child_truncated:
            root["truncation_reason"] = "child_scan_truncated"
        root["timed_out"] = root["discovery_timed_out"] or any(
            child["timed_out"] for child in child_scans
        )
        paths.append(root)

    discovery_scan_errors = (
        sum(root["scan_errors"] for root, _ in roots) - scan_summary["scan_errors"]
    )
    discovery_permission_errors = (
        sum(root["permission_errors"] for root, _ in roots) - scan_summary["permission_errors"]
    )
    discovery_truncated = any(root["discovery_truncated"] for root, _ in roots)
    timed_out = any(root["timed_out"] for root, _ in roots)
    return {
        "paths": paths,
        "configured_paths": list(_PRESSURE_PATHS),
        "skipped_nested_paths": skipped_nested_paths,
        "limit_per_root": child_limit,
        "max_children_per_root": max_children_per_root,
        "max_entries_per_path": scan_summary["max_entries_per_child"],
        "max_entries_per_child": scan_summary["max_entries_per_child"],
        "max_total_entries": total_budget,
        "total_entries_scanned": scan_summary["total_entries_scanned"],
        "timeout_seconds": timeout_seconds,
        "discovery_time_slice_seconds": discovery_time_slice_seconds,
        "time_slice_seconds": scan_summary["time_slice_seconds"],
        "timed_out": timed_out,
        "truncated": discovery_truncated or scan_summary["truncated"],
        "scan_errors": discovery_scan_errors + scan_summary["scan_errors"],
        "permission_errors": discovery_permission_errors + scan_summary["permission_errors"],
        "skipped_different_filesystem": scan_summary["skipped_different_filesystem"],
        "same_filesystem_only": True,
    }


def _resolved_k3s_volume_roots() -> list[Path]:
    roots: list[Path] = []
    for configured in _K3S_VOLUME_ROOTS:
        root = _host_path(configured).resolve()
        if root not in roots:
            roots.append(root)
    return roots


def _safe_k3s_volume_path(path: str | None, roots: list[Path]) -> Path | None:
    if not path:
        return None
    candidate = _host_path(path).resolve()
    if any(root in candidate.parents for root in roots):
        return candidate
    return None


def _k3s_volume_root_children(
    roots: list[Path],
) -> tuple[list[Path], list[str], bool]:
    children: list[Path] = []
    errors: list[str] = []
    truncated = False
    cap = max(1, _MAX_K3S_VOLUME_PATHS)
    for root in roots:
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    child = Path(entry.path).resolve()
                    if root not in child.parents:
                        continue
                    if len(children) >= cap:
                        truncated = True
                        break
                    children.append(child)
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{_public_host_path(root)}: {exc}")
    children.sort(key=str)
    return children, errors, truncated


def _k3s_volume_inventory() -> tuple[list[dict[str, Any]], list[str]]:
    pod_items, pod_errors = _k8s_list("/api/v1/pods")
    pvc_items, pvc_errors = _k8s_list("/api/v1/persistentvolumeclaims")
    pv_items, pv_errors = _k8s_list("/api/v1/persistentvolumes")
    errors = [
        *(f"pods: {error}" for error in pod_errors),
        *(f"persistent volume claims: {error}" for error in pvc_errors),
        *(f"persistent volumes: {error}" for error in pv_errors),
    ]
    mounts = _pod_pvc_mounts(pod_items)
    pv_by_name = {
        str(pv["persistent_volume"]): pv
        for item in pv_items
        if (pv := _normalize_pv(item))["persistent_volume"]
    }
    volumes: list[dict[str, Any]] = []
    claimed_pvs: set[str] = set()
    for item in pvc_items:
        pvc = _normalize_pvc(item)
        namespace = pvc["namespace"]
        claim_name = pvc["persistent_volume_claim"]
        pv_name = pvc["persistent_volume"]
        pv = pv_by_name.get(str(pv_name), {}) if pv_name else {}
        if pv_name:
            claimed_pvs.add(str(pv_name))
        mount_key = (str(namespace), str(claim_name))
        volumes.append(
            {
                "namespace": namespace,
                "persistent_volume_claim": claim_name,
                "persistent_volume_claim_uid": pvc["persistent_volume_claim_uid"],
                "persistent_volume": pv_name,
                "persistent_volume_uid": pv.get("persistent_volume_uid"),
                "storage_class": pvc["storage_class"] or pv.get("storage_class"),
                "pvc_phase": pvc["phase"],
                "pv_phase": pv.get("phase"),
                "requested_bytes": pvc["requested_bytes"],
                "capacity_bytes": pvc["capacity_bytes"] or pv.get("capacity_bytes"),
                "local_path_source": pv.get("local_path_source"),
                "pod_mounts": mounts.get(mount_key, []),
                "_candidate_local_path": pv.get("local_path"),
            }
        )
    for pv_name, pv in pv_by_name.items():
        if pv_name in claimed_pvs:
            continue
        namespace = pv["namespace"]
        claim_name = pv["persistent_volume_claim"]
        mount_key = (str(namespace), str(claim_name))
        volumes.append(
            {
                "namespace": namespace,
                "persistent_volume_claim": claim_name,
                "persistent_volume_claim_uid": None,
                "persistent_volume": pv_name,
                "persistent_volume_uid": pv["persistent_volume_uid"],
                "storage_class": pv["storage_class"],
                "pvc_phase": None,
                "pv_phase": pv["phase"],
                "requested_bytes": None,
                "capacity_bytes": pv["capacity_bytes"],
                "local_path_source": pv["local_path_source"],
                "pod_mounts": mounts.get(mount_key, []),
                "_candidate_local_path": pv["local_path"],
            }
        )
    volumes.sort(
        key=lambda volume: (
            str(volume.get("namespace") or ""),
            str(volume.get("persistent_volume_claim") or ""),
            str(volume.get("persistent_volume") or ""),
        )
    )
    return volumes, errors


def _scan_k3s_volume_paths(
    paths: list[Path], max_entries_per_volume: int
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not paths:
        return {}, {
            "max_entries_per_volume": 0,
            "max_total_entries": max(1, _MAX_DU_TOTAL_ENTRIES),
            "total_entries_scanned": 0,
            "timeout_seconds": max(0.001, _DU_TIMEOUT_SECONDS),
            "time_slice_seconds": 0.0,
            "timed_out": False,
            "truncated": False,
            "scan_errors": 0,
        }
    total_budget = max(1, _MAX_DU_TOTAL_ENTRIES)
    entry_budget = max(
        1,
        min(
            max_entries_per_volume,
            _MAX_DU_ENTRIES,
            max(1, total_budget // len(paths)),
        ),
    )
    timeout_seconds = max(0.001, _DU_TIMEOUT_SECONDS)
    time_slice_seconds = max(0.001, timeout_seconds / len(paths))
    scans: dict[str, dict[str, Any]] = {}
    total_entries_scanned = 0
    for path in paths:
        try:
            root_device = path.lstat().st_dev
        except OSError:
            root_device = None
        budget = _ScanBudget(
            max_entries=entry_budget,
            deadline=time.monotonic() + time_slice_seconds,
        )
        scan = _du_path(path, entry_budget, root_device, budget)
        scans[str(path)] = scan
        total_entries_scanned += budget.entries_scanned
    return scans, {
        "max_entries_per_volume": entry_budget,
        "max_total_entries": total_budget,
        "total_entries_scanned": total_entries_scanned,
        "timeout_seconds": timeout_seconds,
        "time_slice_seconds": time_slice_seconds,
        "timed_out": any(scan["timed_out"] for scan in scans.values()),
        "truncated": any(scan["truncated"] for scan in scans.values()),
        "scan_errors": sum(scan["scan_errors"] for scan in scans.values()),
    }


def _k3s_volume_usage(limit: int, max_entries_per_volume: int) -> dict[str, Any]:
    volumes, errors = _k3s_volume_inventory()
    roots = _resolved_k3s_volume_roots()
    paths_by_key: dict[str, Path] = {}
    attributed_paths: set[Path] = set()
    for volume in volumes:
        candidate = volume.pop("_candidate_local_path")
        safe_path = _safe_k3s_volume_path(candidate, roots)
        if safe_path is None:
            volume["usage"] = None
            volume["scan_status"] = "outside_configured_roots" if candidate else "no_local_path"
            continue
        volume["_local_path_key"] = str(safe_path)
        volume["local_path"] = _public_host_path(safe_path)
        paths_by_key.setdefault(str(safe_path), safe_path)
        attributed_paths.add(safe_path)

    root_children, discovery_errors, discovery_truncated = _k3s_volume_root_children(roots)
    errors.extend(f"volume discovery: {error}" for error in discovery_errors)
    unattributed_paths = [
        child
        for child in root_children
        if child not in attributed_paths
        and not any(child in attributed.parents for attributed in attributed_paths)
    ]
    for path in unattributed_paths:
        paths_by_key.setdefault(str(path), path)

    path_cap = max(1, _MAX_K3S_VOLUME_PATHS)
    scan_paths = list(paths_by_key.values())[:path_cap]
    path_limit_truncated = len(paths_by_key) > path_cap
    scans, scan_summary = _scan_k3s_volume_paths(scan_paths, max_entries_per_volume)

    counted_paths: set[str] = set()
    namespace_rollups: dict[str, dict[str, Any]] = {}
    for volume in volumes:
        path_key = volume.pop("_local_path_key", None)
        scan = scans.get(path_key) if path_key else None
        if path_key and scan is None:
            volume["usage"] = None
            volume["scan_status"] = "path_limit"
        elif scan is not None:
            volume["usage"] = scan
            volume["scan_status"] = "missing" if not scan["exists"] else "scanned"
        namespace = volume.get("namespace")
        if not namespace:
            continue
        rollup = namespace_rollups.setdefault(
            str(namespace),
            {
                "namespace": str(namespace),
                "used_bytes": 0,
                "volume_count": 0,
                "truncated_volume_count": 0,
                "_pods": set(),
            },
        )
        rollup["volume_count"] += 1
        rollup["_pods"].update(
            str(mount["pod"]) for mount in volume["pod_mounts"] if mount.get("pod")
        )
        if scan is None or path_key in counted_paths:
            continue
        counted_paths.add(path_key)
        rollup["used_bytes"] += scan["size_bytes"]
        if scan["truncated"]:
            rollup["truncated_volume_count"] += 1

    namespaces = []
    for rollup in namespace_rollups.values():
        pods = rollup.pop("_pods")
        rollup["pod_count"] = len(pods)
        namespaces.append(rollup)
    namespaces.sort(key=lambda item: item["used_bytes"], reverse=True)

    volumes.sort(
        key=lambda volume: (
            volume["usage"]["size_bytes"] if volume.get("usage") else -1,
            str(volume.get("namespace") or ""),
            str(volume.get("persistent_volume_claim") or ""),
        ),
        reverse=True,
    )
    unattributed = [scans[str(path)] for path in unattributed_paths if str(path) in scans]
    unattributed.sort(key=lambda item: item["size_bytes"], reverse=True)
    cap = max(1, min(limit, 100))
    return {
        "volumes": volumes[:cap],
        "namespaces": namespaces,
        "unattributed_paths": unattributed[:cap],
        "configured_volume_roots": list(_K3S_VOLUME_ROOTS),
        "volume_count": len(volumes),
        "unattributed_path_count": len(unattributed_paths),
        "path_limit": path_cap,
        "paths_scanned": len(scan_paths),
        "discovery_truncated": discovery_truncated or path_limit_truncated,
        **scan_summary,
        "errors": errors,
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
    """Bounded child usage beneath fixed host paths that commonly drive pressure.

    Traversal runs in a worker thread, leaving the MCP event loop available for
    fast tools such as get_filesystem_pressure. Every configured root gets a
    bounded discovery slice, then every immediate child gets a fair entry and
    time slice. Per-child results carry truncation, timeout, filesystem-skip,
    permission, and scan-error metadata. Nested configured roots are coalesced.
    The caller can tune only the child entry cap and per-root result count,
    never a raw path.
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
    get_k3s_volume_usage,
    stat_path,
    read_text_head,
):
    mcp.tool()(_tool)


def main() -> None:
    """Run the MCP server over streamable-HTTP (endpoint served at /mcp)."""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
