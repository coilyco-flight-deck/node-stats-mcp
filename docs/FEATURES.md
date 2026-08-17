# node-stats-mcp features

Living inventory of what ships here: a read-only MCP server over host and k3s
diagnostics, with an OTLP exporter.

## Tools

All read-only: 30 host and node tools in [host tools](tools-host.md) and 8
cluster tools in [k3s tools](tools-k3s.md), covering CPU, memory, disk,
filesystem and PSI pressure, usage attribution, deleted open files, network,
processes, and the k3s pod, volume, scheduling, and health views.

## OTLP export

A same-image `node-stats-exporter` sidecar runs independently of the MCP
process, on fast and slow cadences, emitting stable metric dimensions and
bounded structured logs over independent signals. See
[OTLP export](otlp-export.md).

## Security and configuration

Read-only by construction, an allowlisted read surface, and bounded traversal:
[security](security.md). Settings are environment-only, in
[configuration](configuration.md) and
[scan configuration](configuration-scans.md).

## Deploy

Every push to canonical `main` publishes and verifies the private image at a
full source SHA. Rollout lives in
[deploy](https://forgejo.coilysiren.me/coilyco-bridge/deploy).

## See also

- [../README.md](../README.md) - human-facing intro.
- [../AGENTS.md](../AGENTS.md) - agent operating context.
- [../.ward/ward.yaml](../.ward/ward.yaml) - allowlisted commands + catalog block.

Cross-reference convention from [features-release-tooling.md](features-release-tooling.md).
