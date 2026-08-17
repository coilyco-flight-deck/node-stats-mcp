# OTLP export

## Behaviour

- **Same-image sidecar** - `node-stats-exporter` runs independently from the MCP process, so collector or export failures cannot take down the tool surface.
- **Fast and slow cadences** - contention, kubelet resource usage, node health and events, scheduled work, freshness, configured conditions, and root-filesystem pressure export every minute by default. Bounded local-volume attribution exports every 15 minutes by default.
- **Stable metrics** - node, namespace, device, CronJob, configured check, configured resource, and PVC dimensions support dashboards and alerts without pod, container, process, event-object, or generated PV names.
- **Detailed structured logs** - one OTLP log per source retains the bounded source snapshot. Oversized records become valid JSON truncation envelopes.
- **Independent signals** - metrics and logs use separate OTLP/HTTP requests. One signal may fail without cancelling the other, and the next cycle continues.
- **Payload bounds** - collection limits, point caps, per-log caps, total signal payload caps, and HTTP timeouts are server-owned configuration.

## Settings

- `NODE_STATS_OTLP_ENDPOINT` - collector base URL or OTLP signal URL. The exporter normalizes it to `/v1/metrics` and `/v1/logs`.
- `NODE_STATS_EXPORT_INTERVAL_SECONDS` (default 60, bounded 15 to 3600).
- `NODE_STATS_EXPORT_VOLUME_INTERVAL_SECONDS` (default 900, bounded to at least the fast interval and at most 86400).
- `NODE_STATS_EXPORT_LIMIT` (default 50, bounded 1 to 100) - shared source result limit.
- `NODE_STATS_EXPORT_MAX_LOG_BYTES` (default 262144, bounded 2048 to 1048576 and never above the payload cap).
- `NODE_STATS_EXPORT_MAX_PAYLOAD_BYTES` (default 1048576, bounded 65536 to 4194304) - independent cap for each metrics or logs request.
- `NODE_STATS_EXPORT_MAX_METRIC_POINTS` (default 2000, bounded 100 to 5000).
- `NODE_STATS_OTLP_TIMEOUT_SECONDS` (default 5, bounded 1 to 30).
