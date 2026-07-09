# node-stats-mcp

A node-local MCP battery. It reads the node it runs on - CPU, memory, disk, load, network, top processes, and bounded file metadata - and serves that over MCP (streamable-HTTP), so an agent can inspect a node without a host bind mount.

This is the generic node-introspection spine, first instance of the upstream pattern: a per-node MCP agent, the same shape as node-exporter (DaemonSet-or-node-pinned + hostPath + host namespaces), but exposing a tool surface instead of Prometheus metrics.

## Node view, not pod view

True node stats need the pod to borrow the host's namespaces: **hostPID** so process listings see the node, **hostNetwork** so net counters are the node's, and a read-only **hostPath** of `/` at `/host` (with `ROOTFS=/host`) for disk. CPU and memory come from the non-namespaced `/proc/{stat,meminfo}` regardless. The deploy bundle wires all of this - see below.

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

Cross-reference convention from [coilysiren/agentic-os#59](https://github.com/coilyco-flight-deck/agentic-os/issues/59).
