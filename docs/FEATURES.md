# node-stats-mcp features

Living inventory of what ships from this repo. One image, one process: a FastMCP server on port 8080, streamable-HTTP endpoint at `/mcp`.

## Tools

- **get_cpu_info** - utilization, logical/physical core counts, per-core percentages, load average.
- **get_memory_info** - virtual and swap memory (bytes + percent).
- **get_disk_info** - per-partition usage and mount info, resolved under `ROOTFS`.
- **get_filesystem_pressure** - root filesystem capacity, available bytes, inode pressure, and byte runway to warning/critical thresholds.
- **get_pressure_path_usage** - worker-thread one-level attribution beneath configured node-pressure roots such as logs, journald, kubelet, k3s, and containerd storage. Every discovered child receives a fair entry and time slice, preventing a large root or child from starving siblings. Per-child results include size, entries scanned, permission/scan errors, skipped different-filesystem entries, and timeout/truncation metadata.
- **get_network_info** - aggregate and per-interface I/O counters (node-wide under hostNetwork).
- **get_top_processes** - top N by cpu or memory (node-wide under hostPID).
- **get_k3s_pods** - read-only namespace/pod/container inventory from the k3s API.
- **get_k3s_container_memory** - approximate per-container memory from metrics-server or host cgroups.
- **get_k3s_process_attribution** - top host processes annotated with namespace/pod/container when cgroup metadata resolves.
- **get_k3s_volume_usage** - worker-thread local-volume scan joined to namespaces, PVCs, PVs, and pod/container mount paths. Namespace totals count each volume once, server-owned roots constrain every scan, fair per-volume budgets prevent starvation, and unowned root children remain visible as unattributed storage.
- **get_system_snapshot** - one-shot overview: cpu, memory, load, boot time, uptime, logged-in users.
- **stat_path** - size/mode/mtime/type for a path under the readable-root allowlist.
- **read_text_head** - up to `max_bytes` (capped) of a text file under the allowlist.

## Security envelope

- **Read-only** - no tool mutates the host.
- **Prefix-allowlisted file access** - `NODE_STATS_READABLE_ROOTS` (colon-separated, empty by default) gates `stat_path` / `read_text_head`. Paths resolve real (symlinks collapsed) and must sit under an allowed root. `NODE_STATS_MAX_READ_BYTES` caps read size.
- **Fixed pressure scan paths** - `get_pressure_path_usage` only discovers immediate children beneath `NODE_STATS_PRESSURE_PATHS`, never a caller-supplied raw path. Nested configured paths are skipped when an ancestor already covers them. Root discovery, per-child traversal, total entries, and wall-clock time are capped.
- **Fixed Kubernetes volume roots** - `get_k3s_volume_usage` accepts no path argument. The server resolves PV paths beneath `NODE_STATS_K3S_VOLUME_ROOTS`, rejects paths outside those roots, and bounds both one-level orphan discovery and recursive usage scans.
- **Network-gated reach** - the endpoint is meant to sit behind the tailnet / node boundary, not public.

## Configuration (env)

- `PORT` (default 8080), `HOST` (default 0.0.0.0).
- `ROOTFS` (default `/`) - where the host root is mounted in the pod (`/host` in the deploy).
- `NODE_STATS_READABLE_ROOTS` - colon-separated read allowlist, interpreted inside `ROOTFS`.
- `NODE_STATS_MAX_READ_BYTES` (default 65536).
- `NODE_STATS_KUBECONFIG` (default `/etc/rancher/k3s/k3s.yaml`, interpreted inside `ROOTFS`) - host kubeconfig used for the k3s inventory when present.
- `NODE_STATS_K8S_TIMEOUT_SECONDS` (default 3) - timeout for Kubernetes API reads.
- `NODE_STATS_K3S_VOLUME_ROOTS` (default `/var/lib/rancher/k3s/storage`) - colon-separated fixed roots that may contain local PV paths.
- `NODE_STATS_MAX_K3S_VOLUME_PATHS` (default 1000) - cap on local PV and unattributed child paths considered by one volume-usage request.
- `NODE_STATS_DISK_WARN_PERCENT` (default 80).
- `NODE_STATS_DISK_CRITICAL_PERCENT` (default 85).
- `NODE_STATS_PRESSURE_PATHS` - colon-separated fixed paths for pressure scans, interpreted inside `ROOTFS`.
- `NODE_STATS_MAX_PRESSURE_CHILDREN_PER_ROOT` (default 1000) - one-level discovery cap for each configured pressure root.
- `NODE_STATS_MAX_DU_ENTRIES` (default 200000) - per-child traversal cap for pressure scans.
- `NODE_STATS_MAX_DU_TOTAL_ENTRIES` (default 200000) - shared traversal cap across all pressure children in one request.
- `NODE_STATS_DU_TIMEOUT_SECONDS` (default 10) - wall-clock cap for one pressure request; timeout is returned as root and child metadata.

## Deploy

Node-pinned hostPID + hostNetwork pod, image published to the in-cluster registry by [`.forgejo/workflows/build-publish.yml`](../.forgejo/workflows/build-publish.yml). Rollout lives in [coilyco-bridge/deploy](https://forgejo.coilysiren.me/coilyco-bridge/deploy) `services/node-stats-mcp`.

## See also

- [../README.md](../README.md) - human-facing intro.
- [../AGENTS.md](../AGENTS.md) - agent operating context.
- [k3s-inventory.md](k3s-inventory.md) - k3s pod, container, and attribution walkthrough.
- [../.ward/ward.yaml](../.ward/ward.yaml) - allowlisted commands + catalog block.

Cross-reference convention from [features-release-tooling.md](features-release-tooling.md).
