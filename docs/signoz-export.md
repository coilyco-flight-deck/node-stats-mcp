# SigNoz export

`node-stats-exporter` turns the bounded MCP node views into OTLP/HTTP JSON metrics and structured logs. A deployment runs it from the same image as a sidecar, keeping collector availability independent from the MCP endpoint.

## Cadence

The fast cycle collects these sources concurrently every 60 seconds by default:

- Linux pressure stalls, selected VM counters, and block-device counters.
- Bounded Kubelet Summary API usage.
- Node conditions, taints, and recent events.
- Jobs, CronJobs, and configured freshness.
- Configured custom-resource conditions.
- Root filesystem runway and inode pressure.

The first cycle also collects local-volume usage and lifecycle state. Later volume scans run every 900 seconds. Cycles never overlap, so a slow scan may delay but cannot duplicate the next cycle.

## Signal model

The exporter sends independent OTLP requests to `/v1/metrics` and `/v1/logs`.

Metrics provide low-cardinality operational series:

- PSI, VM, and block-I/O series by stable resource, counter, or device.
- Kubelet node gauges and pod usage aggregated by namespace.
- Node conditions and event counts by event type and reason.
- Job totals by namespace and CronJob timing by configured name.
- Freshness and resource conditions by configured stable names.
- Volume state by namespace and PVC.
- Root filesystem and per-source health.

Metrics deliberately exclude pod, container, process, event-object, and generated PV names. Detailed source output remains available as one structured OTLP log per source. Each log body is JSON with `source` and `snapshot` fields.

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

`NODE_STATS_OTLP_ENDPOINT` is required. It accepts a collector base or signal URL and derives both signal paths. The deployment owns it because node placement determines routing.

The exporter recognizes:

- `NODE_STATS_EXPORT_INTERVAL_SECONDS`, default `60`, bounded `15..3600`.
- `NODE_STATS_EXPORT_VOLUME_INTERVAL_SECONDS`, default `900`, bounded to the fast interval through `86400`.
- `NODE_STATS_EXPORT_LIMIT`, default `50`, bounded `1..100`.
- `NODE_STATS_EXPORT_MAX_LOG_BYTES`, default `262144`, bounded `2048..1048576` and never above the payload cap.
- `NODE_STATS_EXPORT_MAX_PAYLOAD_BYTES`, default `1048576`, bounded `65536..4194304`.
- `NODE_STATS_EXPORT_MAX_METRIC_POINTS`, default `2000`, bounded `100..5000`.
- `NODE_STATS_OTLP_TIMEOUT_SECONDS`, default `5`, bounded `1..30`.

The sidecar shares the MCP server's host, Kubernetes, freshness, and traversal configuration.

## Entrypoint

`node-stats-exporter` runs continuously. `node-stats-exporter --once` performs one collection and export. `node-stats-exporter --dry-run` performs one collection, sends nothing, and prints only a bounded cycle summary.

The exporter adds no runtime dependency. Its encoder follows OTLP JSON conventions for lower-camel field names, integer strings, numeric enums, and the standard signal paths.

The production sidecar manifests live in `coilyco-bridge/deploy/services/node-stats-mcp`.
