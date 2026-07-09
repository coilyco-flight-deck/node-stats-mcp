# k3s inventory

`node-stats-mcp` exposes three read-only tools for host-to-pod attribution:

- `get_k3s_pods` - list pods with namespace, phase, node, restart count, pod IP, age, and container names/images.
- `get_k3s_container_memory` - report approximate container memory from metrics-server when available, else sum host process RSS by cgroup match.
- `get_k3s_process_attribution` - annotate the existing host process view with namespace/pod/container data when cgroup metadata resolves to a pod.

## Data sources

- The preferred path is the host-mounted k3s admin kubeconfig at `/host/etc/rancher/k3s/k3s.yaml`.
- If that file is unavailable, the server falls back to the pod's service account token.
- Container memory prefers `metrics.k8s.io`, then falls back to cgroup-backed RSS from host PID data.

## Safety

- Read-only only. No exec, logs, secret reads, deletes, patches, or rollout calls.
- Missing Kubernetes metadata is reported as an empty or partial result instead of becoming a write or shell escape.

## Notes

- The deploy surface must still give the pod access to the host root at `/host` so the kubeconfig path can be read.
- Existing node-local CPU, memory, disk, network, and file-read tools remain unchanged.
