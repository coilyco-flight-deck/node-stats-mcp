# node-stats-mcp features

Living inventory of what ships from this repo. One image, one process: a FastMCP server on port 8080, streamable-HTTP endpoint at `/mcp`.

## Tools

- **get_cpu_info** - utilization, logical/physical core counts, per-core percentages, load average.
- **get_memory_info** - virtual and swap memory (bytes + percent).
- **get_disk_info** - per-partition usage and mount info, resolved under `ROOTFS`.
- **get_network_info** - aggregate and per-interface I/O counters (node-wide under hostNetwork).
- **get_top_processes** - top N by cpu or memory (node-wide under hostPID).
- **get_system_snapshot** - one-shot overview: cpu, memory, load, boot time, uptime, logged-in users.
- **stat_path** - size/mode/mtime/type for a path under the readable-root allowlist.
- **read_text_head** - up to `max_bytes` (capped) of a text file under the allowlist.

## Security envelope

- **Read-only** - no tool mutates the host.
- **Prefix-allowlisted file access** - `NODE_STATS_READABLE_ROOTS` (colon-separated, empty by default) gates `stat_path` / `read_text_head`; paths resolve real (symlinks collapsed) and must sit under an allowed root. `NODE_STATS_MAX_READ_BYTES` caps read size.
- **Network-gated reach** - the endpoint is meant to sit behind the tailnet / node boundary, not public.

## Configuration (env)

- `PORT` (default 8080), `HOST` (default 0.0.0.0).
- `ROOTFS` (default `/`) - where the host root is mounted in the pod (`/host` in the deploy).
- `NODE_STATS_READABLE_ROOTS` - colon-separated read allowlist, interpreted inside `ROOTFS`.
- `NODE_STATS_MAX_READ_BYTES` (default 65536).

## Deploy

Node-pinned hostPID + hostNetwork pod, image published to the in-cluster registry by [`.forgejo/workflows/build-publish.yml`](../.forgejo/workflows/build-publish.yml). Rollout lives in [coilyco-bridge/deploy](https://forgejo.coilysiren.me/coilyco-bridge/deploy) `services/node-stats-mcp`.

## See also

- [../README.md](../README.md) - human-facing intro.
- [../AGENTS.md](../AGENTS.md) - agent operating context.
- [../.ward/ward.yaml](../.ward/ward.yaml) - allowlisted commands + catalog block.

Cross-reference convention from [coilysiren/agentic-os#59](https://github.com/coilyco-flight-deck/agentic-os/issues/59).
