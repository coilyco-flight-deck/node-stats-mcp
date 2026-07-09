# node-stats-mcp

A node-local MCP battery. It reads the node it runs on - CPU, memory, disk, load, network, top processes, and bounded file metadata - and serves that over MCP (streamable-HTTP), so an agent can inspect a node without a host bind mount.

This is the generic node-introspection spine, first instance of the upstream pattern: a per-node MCP agent, the same shape as node-exporter (DaemonSet-or-node-pinned + hostPath + host namespaces), but exposing a tool surface instead of Prometheus metrics.

## Node view, not pod view

True node stats need the pod to borrow the host's namespaces: **hostPID** so process listings see the node, **hostNetwork** so net counters are the node's, and a read-only **hostPath** of `/` at `/host` (with `ROOTFS=/host`) for disk. CPU and memory come from the non-namespaced `/proc/{stat,meminfo}` regardless. The deploy bundle wires all of this - see below.

## k3s view

The server also exposes a read-only k3s inventory surface for host-to-pod attribution:

- `get_k3s_pods` - namespace, pod, phase, node, restart count, container names/images, pod IP, age.
- `get_k3s_container_memory` - per-container memory from metrics-server when available, else approximate RSS summed from host cgroups.
- `get_k3s_process_attribution` - top host processes annotated with the owning pod/container when cgroup data and pod metadata line up.

The API read path prefers the host-mounted k3s admin kubeconfig at `/host/etc/rancher/k3s/k3s.yaml` and falls back to the pod's service account when needed. All three tools stay read-only.

## Safety

Read-only by construction: every tool is a read, none mutate the host. File introspection (`stat_path`, `read_text_head`) is **prefix-allowlisted** via `NODE_STATS_READABLE_ROOTS` (empty by default = file reads denied) and size-capped, so a tool can never be walked into `/host/root/.ssh`. Reach is gated at the network layer (the tailnet / node), not by the tool.

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
