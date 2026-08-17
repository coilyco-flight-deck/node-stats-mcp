# k3s tools

Read-only views of the local k3s cluster. Host and node tools are in
[host tools](tools-host.md).

- **get_k3s_pods** - read-only namespace/pod/container inventory from the k3s API.
- **get_k3s_container_memory** - approximate per-container memory from metrics-server or host cgroups.
- **get_k3s_process_attribution** - top host processes annotated with namespace/pod/container when cgroup metadata resolves.
- **get_k3s_resource_usage** - worker-thread kubelet Summary API view of node, runtime filesystem, system-container, pod, container, volume, network, and ephemeral-storage usage.
- **get_k3s_node_health** - worker-thread node conditions, taints, capacity, allocatable resources, and recent relevant or warning events.
- **get_k3s_volume_usage** - worker-thread local-volume scan joined to namespaces, PVCs, PVs, pod/container mount paths, and storage lifecycle state. Namespace totals count each volume once, server-owned roots constrain every scan, fair per-volume budgets prevent starvation, and unowned root children remain visible as unattributed storage. Volume, namespace, and response totals label complete usage versus lower bounds, while a matching host-usage profile schedules or exposes the complete background snapshot.
- **get_k3s_scheduled_work** - worker-thread Jobs and CronJobs with activity, failure, duration, and last-schedule or last-success timing.
- **get_k3s_configured_conditions** - normalized conditions from server-configured Kubernetes custom-resource types.
