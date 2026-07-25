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

- The preferred path is the host-mounted k3s admin kubeconfig at `/host/etc/rancher/k3s/k3s.yaml`.
- If that file is unavailable, the server falls back to the pod's service account token.
- Container memory prefers `metrics.k8s.io`, then falls back to cgroup-backed RSS from host PID data.
- Kubelet summary and node health select `NODE_STATS_K3S_NODE_NAME` when configured. Without it, those reads require the Kubernetes API to return exactly one node.
- Node events include events attached to the selected node or its pods, plus bounded recent warning events for cluster resources.
- Volume attribution joins the core pod, PVC, and PV APIs. The scanner measures only PV paths beneath `NODE_STATS_K3S_VOLUME_ROOTS`.
- Volume lifecycle includes PVC/PV phase, deletion timestamp, finalizers, conditions, access modes, volume mode, reclaim policy, and PV status details.
- Namespace totals count each local volume once even when multiple pods mount the same claim.
- Unattributed results are immediate children of the configured storage roots that no current PV path owns. They surface released or abandoned local-path data without guessing ownership.
- Scheduled-work freshness comes from the batch/v1 Job and CronJob APIs.
- Custom-resource condition sources come from `NODE_STATS_K3S_CONDITION_RESOURCES`, a JSON list of objects with `name`, `group`, `version`, `resource`, and optional `namespace`.

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
