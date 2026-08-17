# k3s inventory

`node-stats-mcp` exposes eight read-only tools for Kubernetes inventory, health, and host-to-workload attribution:

- `get_k3s_pods` - list pods with namespace, phase, node, restart count, pod IP, age, and container names/images.
- `get_k3s_container_memory` - report approximate container memory from metrics-server when available, else sum host process RSS by cgroup match.
- `get_k3s_process_attribution` - annotate the existing host process view with namespace/pod/container data when cgroup metadata resolves to a pod.
- `get_k3s_resource_usage` - report bounded node, runtime-filesystem, pod, container, volume, network, and ephemeral-storage usage from the kubelet Summary API.
- `get_k3s_node_health` - report node conditions, taints, capacity, allocatable resources, and recent node-relevant or cluster-warning events.
- `get_k3s_volume_usage` - join local persistent-volume disk usage and lifecycle state to namespaces, PVCs, PVs, and every pod/container mount, while listing storage directories that no current PV owns.
- `get_k3s_scheduled_work` - report Jobs and CronJobs with failure, activity, duration, last-schedule, and last-success timing.
- `get_k3s_configured_conditions` - normalize conditions for custom-resource types fixed in server configuration.

## Data sources

Where each read comes from and how it falls back:
[k3s data sources](k3s-data-sources.md).

## Safety

- Read-only only. No exec, logs, secret reads, deletes, patches, or rollout calls.
- Missing Kubernetes metadata is reported as an empty or partial result instead of becoming a write or shell escape.
- Kubernetes API paths outside the configured local-volume roots are reported but never scanned or echoed.
- Custom-resource group, version, resource, and namespace segments are validated before the server derives an API path.
- Callers can tune result, age, and entry limits. Callers cannot supply a node, raw filesystem path, API path, or Kubernetes resource type.

## Notes

- The deploy surface must still give the pod access to the host root at `/host` so the kubeconfig path can be read.
- The kubelet Summary API requires Kubernetes authorization for the selected node's `nodes/proxy` subresource.
- The deployment's Kubernetes identity needs read access only for the core, batch, metrics, selected-node proxy, and explicitly configured custom-resource APIs that the tools use.
- Volume scans run in a worker thread and divide the request's entry and time budgets across discovered paths.
- Truncation, timeout, scan errors, permission errors, and cross-filesystem skips stay visible per volume.
