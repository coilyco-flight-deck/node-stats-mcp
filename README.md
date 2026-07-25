# node-stats-mcp

A node-local MCP battery. It reads the node it runs on - CPU, memory, Linux contention, disk-pressure runway, block I/O, load, network, top processes, Kubernetes health and storage attribution, scheduled-work freshness, and bounded file metadata - and serves that over MCP (streamable-HTTP).

This is the generic node-introspection spine, first instance of the upstream pattern: a per-node MCP agent, the same shape as node-exporter (DaemonSet-or-node-pinned + hostPath + host namespaces), but exposing a tool surface instead of Prometheus metrics.

## Node view, not pod view

True node stats need the pod to borrow the host's namespaces: **hostPID** so process listings see the node, **hostNetwork** so net counters are the node's, and a read-only **hostPath** of `/` at `/host` (with `ROOTFS=/host`) for disk. CPU and memory come from the non-namespaced `/proc/{stat,meminfo}` regardless. The deploy bundle wires all of this - see below.

## k3s view

The server also exposes a read-only k3s inventory and health surface:

- `get_k3s_pods` - namespace, pod, phase, node, restart count, container names/images, pod IP, age.
- `get_k3s_container_memory` - per-container memory from metrics-server when available, else approximate RSS summed from host cgroups.
- `get_k3s_process_attribution` - top host processes annotated with the owning pod/container when cgroup data and pod metadata line up.
- `get_k3s_resource_usage` - bounded kubelet Summary API usage for the selected node, system containers, pods, containers, volumes, and ephemeral storage.
- `get_k3s_node_health` - node conditions, taints, capacity, and recent node-relevant or warning events.
- `get_k3s_volume_usage` - bounded local-volume disk usage and lifecycle state joined to namespaces, PVCs, PVs, and pod/container mount paths, plus unattributed storage directories.
- `get_k3s_scheduled_work` - Jobs and CronJobs with failures, activity, duration, and last-schedule or last-success timing.
- `get_k3s_configured_conditions` - normalized conditions for custom-resource types selected by server configuration.

The API read path prefers the host-mounted k3s admin kubeconfig at `/host/etc/rancher/k3s/k3s.yaml` and falls back to the pod's service account when needed. Every tool stays read-only.

## Safety

Read-only by construction: every tool is a read, none mutate the host. File introspection (`stat_path`, `read_text_head`) is **prefix-allowlisted** via `NODE_STATS_READABLE_ROOTS` (empty by default = file reads denied) and size-capped, so a tool can never be walked into `/host/root/.ssh`. Disk pressure scans (`get_pressure_path_usage`) use fixed configured roots and server-discovered immediate children. Kubernetes volume scans resolve API-reported PV paths beneath `NODE_STATS_K3S_VOLUME_ROOTS` and reject every path outside those roots. Freshness markers and custom-resource types are also selected by server configuration. Callers cannot turn these tools into a filesystem or Kubernetes API browser. Reach is gated at the network layer (the tailnet / node), not by the tool.

## Disk pressure

`get_filesystem_pressure` reports root filesystem capacity, available bytes, inode use, and byte runway to configurable warning and critical thresholds. `get_pressure_path_usage` attributes each fixed node-pressure root, such as logs, journald, kubelet, k3s, or containerd storage, to its immediate children. Every child result reports size, entries scanned, permission and scan errors, cross-filesystem skips, timeout, and truncation details. Root discovery is capped by `NODE_STATS_MAX_PRESSURE_CHILDREN_PER_ROOT`. The `limit` argument caps returned children per root, while `max_entries_per_path` caps work per child.

Traversal runs in a worker thread, so fast tools remain responsive. The request divides its total entry and wall-clock budgets across roots and children before scanning, preventing one large storage tree from starving its siblings. Nested configured roots are coalesced, with skipped overlaps reported instead of walking the same subtree twice. Callers select neither roots nor children.

`get_k3s_volume_usage` narrows that disk view to local persistent volumes. It joins Kubernetes pod, PVC, and PV metadata to server-approved host paths, reports pod/container mount points, rolls unique volume bytes up by namespace, and separately reports storage-root children that no current PV owns. Fair entry and time slices keep one large volume from starving its siblings.

## Contention and freshness

`get_node_pressure_stalls` reads fixed Linux PSI files, selected VM pressure counters, and bounded per-device block I/O counters. It exposes CPU, memory, and I/O stalls plus swap, major-fault, reclaim, and OOM evidence without accepting a path.

`get_configured_freshness` reports whether server-declared success markers are fresh, stale, missing, or affected by clock skew. It returns metadata only, never marker contents. Kubernetes scheduled-work timing and configured custom-resource conditions provide the corresponding cluster view.

## SigNoz export

The same image includes `node-stats-exporter`, a dependency-free OTLP/HTTP JSON exporter intended to run as a sidecar beside the MCP server. It collects the fast contention, kubelet, health, scheduled-work, freshness, configured-condition, and root-filesystem sources every minute by default. The slower local-volume scan runs every 15 minutes by default.

Metrics use stable node, namespace, configured-resource, device, CronJob, freshness-check, and PVC attributes. Pod, container, process, event-object, and generated PV names stay out of metric attributes. One bounded structured log per source retains the detailed snapshot, including pod and event detail. Oversized logs become valid truncation envelopes rather than invalid partial JSON.

The two processes fail independently. Collector errors do not stop collection, and exporter errors cannot take down the MCP server. See [docs/signoz-export.md](docs/signoz-export.md) for the data model, bounds, and configuration.

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
- [docs/signoz-export.md](docs/signoz-export.md) - bounded OTLP metrics and structured logs.
- [.ward/ward.yaml](.ward/ward.yaml) - allowlisted commands + catalog block.
- [coilyco-bridge/deploy `services/node-stats-mcp`](https://forgejo.coilysiren.me/coilyco-bridge/deploy) - the k3s deploy surface.

Cross-reference convention from [features-release-tooling.md](docs/features-release-tooling.md).
