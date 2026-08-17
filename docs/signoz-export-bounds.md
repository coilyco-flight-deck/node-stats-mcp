# Export bounds and operation

What the exporter caps, how it fails, and how it runs.

## Bounds and failure behavior

The exporter adds to each source's own bounds:

- A shared result limit of 50 items per bounded source.
- A 2,000-point metric cap.
- A 256 KiB cap per structured source log.
- A 1 MiB cap for each metrics or logs request.
- A five-second HTTP timeout.

Configured values remain bounded. An oversized log becomes valid JSON with its source, original size, summary, and `truncated: true`. Tail records or points drop only when the total signal payload still exceeds its cap.

Sources collect independently. An exception produces a partial log and failed source-health metric. Signal failures go to stderr, and a long-running exporter continues.

## Configuration

Every export setting is in [OTLP export](otlp-export.md).
## Entrypoint

`node-stats-exporter` runs continuously. `node-stats-exporter --once` performs one collection and export. `node-stats-exporter --dry-run` performs one collection, sends nothing, and prints only a bounded cycle summary.

The exporter adds no runtime dependency. Its encoder follows OTLP JSON conventions for lower-camel field names, integer strings, numeric enums, and the standard signal paths.

The production sidecar manifests live in `coilyco-bridge/deploy/services/node-stats-mcp`.
