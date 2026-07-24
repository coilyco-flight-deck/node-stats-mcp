# k3s inventory

`node-stats-mcp` exposes four read-only tools for host-to-pod attribution:

- `get_k3s_pods` - list pods with namespace, phase, node, restart count, pod IP, age, and container names/images.
- `get_k3s_container_memory` - report approximate container memory from metrics-server when available, else sum host process RSS by cgroup match.
- `get_k3s_process_attribution` - annotate the existing host process view with namespace/pod/container data when cgroup metadata resolves to a pod.
- `get_k3s_volume_usage` - join local persistent-volume disk usage to namespaces, PVCs, PVs, and every pod/container mount, while listing storage directories that no current PV owns.

## Data sources

- The preferred path is the host-mounted k3s admin kubeconfig at `/host/etc/rancher/k3s/k3s.yaml`.
- If that file is unavailable, the server falls back to the pod's service account token.
- Container memory prefers `metrics.k8s.io`, then falls back to cgroup-backed RSS from host PID data.
- Volume attribution joins the core pod, PVC, and PV APIs. The scanner measures only PV paths beneath `NODE_STATS_K3S_VOLUME_ROOTS`.
- Namespace totals count each local volume once even when multiple pods mount the same claim.
- Unattributed results are immediate children of the configured storage roots that no current PV path owns. They surface released or abandoned local-path data without guessing ownership.

## Safety

- Read-only only. No exec, logs, secret reads, deletes, patches, or rollout calls.
- Missing Kubernetes metadata is reported as an empty or partial result instead of becoming a write or shell escape.
- Kubernetes API paths outside the configured local-volume roots are reported but never scanned or echoed.
- Callers can tune result and entry limits, but callers cannot supply a raw filesystem path.

## Notes

- The deploy surface must still give the pod access to the host root at `/host` so the kubeconfig path can be read.
- Volume scans run in a worker thread and divide the request's entry and time budgets across discovered paths.
- Truncation, timeout, scan errors, permission errors, and cross-filesystem skips stay visible per volume.
- Existing node-local CPU, memory, disk, network, and file-read tools remain unchanged.
