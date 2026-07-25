# node-stats-mcp

A node-local MCP battery. It reads the node it runs on - CPU, memory, disk, disk-pressure runway, load, network, top processes, Kubernetes storage attribution, and bounded file metadata - and serves that over MCP (streamable-HTTP).

This is the generic node-introspection spine, first instance of the upstream pattern: a per-node MCP agent, the same shape as node-exporter (DaemonSet-or-node-pinned + hostPath + host namespaces), but exposing a tool surface instead of Prometheus metrics.

## Node view, not pod view

True node stats need the pod to borrow the host's namespaces: **hostPID** so process listings see the node, **hostNetwork** so net counters are the node's, and a read-only **hostPath** of `/` at `/host` (with `ROOTFS=/host`) for disk. CPU and memory come from the non-namespaced `/proc/{stat,meminfo}` regardless. The deploy bundle wires all of this - see below.

## k3s view

The server also exposes a read-only k3s inventory surface for host-to-pod attribution:

- `get_k3s_pods` - namespace, pod, phase, node, restart count, container names/images, pod IP, age.
- `get_k3s_container_memory` - per-container memory from metrics-server when available, else approximate RSS summed from host cgroups.
- `get_k3s_process_attribution` - top host processes annotated with the owning pod/container when cgroup data and pod metadata line up.
- `get_k3s_volume_usage` - bounded local-volume disk usage joined to namespaces, PVCs, PVs, and pod/container mount paths, plus unattributed storage directories.

The API read path prefers the host-mounted k3s admin kubeconfig at `/host/etc/rancher/k3s/k3s.yaml` and falls back to the pod's service account when needed. All four tools stay read-only.

## Safety

Read-only by construction: every tool is a read, none mutate the host. File introspection (`stat_path`, `read_text_head`) is **prefix-allowlisted** via `NODE_STATS_READABLE_ROOTS` (empty by default = file reads denied) and size-capped, so a tool can never be walked into `/host/root/.ssh`. Disk pressure scans (`get_pressure_path_usage`) use fixed configured roots and server-discovered immediate children. Kubernetes volume scans resolve API-reported PV paths beneath `NODE_STATS_K3S_VOLUME_ROOTS` and reject every path outside those roots. Callers cannot turn either tool into a root filesystem browser. Reach is gated at the network layer (the tailnet / node), not by the tool.

## Disk pressure

`get_filesystem_pressure` reports root filesystem capacity, available bytes, inode use, and byte runway to configurable warning and critical thresholds. `get_pressure_path_usage` attributes each fixed node-pressure root, such as logs, journald, kubelet, k3s, or containerd storage, to its immediate children. Every child result reports size, entries scanned, permission and scan errors, cross-filesystem skips, timeout, and truncation details. Root discovery is capped by `NODE_STATS_MAX_PRESSURE_CHILDREN_PER_ROOT`. The `limit` argument caps returned children per root, while `max_entries_per_path` caps work per child.

Traversal runs in a worker thread, so fast tools remain responsive. The request divides its total entry and wall-clock budgets across roots and children before scanning, preventing one large storage tree from starving its siblings. Nested configured roots are coalesced, with skipped overlaps reported instead of walking the same subtree twice. Callers select neither roots nor children.

`get_k3s_volume_usage` narrows that disk view to local persistent volumes. It joins Kubernetes pod, PVC, and PV metadata to server-approved host paths, reports pod/container mount points, rolls unique volume bytes up by namespace, and separately reports storage-root children that no current PV owns. Fair entry and time slices keep one large volume from starving its siblings.

## Run it locally

```sh
ward sync
ward run     # streamable-HTTP MCP on :8080, endpoint at /mcp
```

## Commands

Dev commands are declared in [`.ward/ward.yaml`](.ward/ward.yaml). Run them as `ward <verb>`.

## See also

- [AGENTS.md](AGENTS.md) - agent operating context for this repo.
- [docs/FEATURES.md](docs/FEATURES.md) - inventory of what ships today.
- [.ward/ward.yaml](.ward/ward.yaml) - allowlisted commands + catalog block.
- [coilyco-bridge/deploy `services/node-stats-mcp`](https://forgejo.coilysiren.me/coilyco-bridge/deploy) - the k3s deploy surface.

Cross-reference convention from [features-release-tooling.md](docs/features-release-tooling.md).
