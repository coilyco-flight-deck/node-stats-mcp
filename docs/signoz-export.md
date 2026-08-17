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

Continued in [export bounds and operation](signoz-export-bounds.md).
