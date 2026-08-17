# Security envelope

- **Read-only** - no tool mutates the host.
- **Prefix-allowlisted file access** - `NODE_STATS_READABLE_ROOTS` (colon-separated, empty by default) gates `stat_path` / `read_text_head`. Paths resolve real (symlinks collapsed) and must sit under an allowed root. `NODE_STATS_MAX_READ_BYTES` caps read size.
- **Fixed pressure scan paths** - `get_pressure_path_usage` only discovers immediate children beneath `NODE_STATS_PRESSURE_PATHS`, never a caller-supplied raw path. Nested configured paths are skipped when an ancestor already covers them. Root discovery, per-child traversal, total entries, and wall-clock time are capped.
- **Fixed host usage profiles** - `get_host_usage_breakdown` accepts only a validated profile name from `NODE_STATS_HOST_USAGE_PROFILES`. Recursive scans run on daemon workers, snapshots identify complete totals versus lower bounds, and stale cache state triggers refresh without blocking the request.
- **Mount-aware physical attribution** - host usage and log scans use Linux mountinfo to report filesystem source/type, exclude other filesystems, deduplicate bind or subtree mounts, and count hard-linked non-directory inodes once.
- **Fixed log and proc scans** - log and journald roots come only from server configuration. Deleted-file collection walks bounded `/proc/<pid>/fd` metadata, returns no filename, and never opens target contents.
- **Fixed Kubernetes volume roots** - `get_k3s_volume_usage` accepts no path argument. The server resolves PV paths beneath `NODE_STATS_K3S_VOLUME_ROOTS`, rejects paths outside those roots, and bounds both one-level orphan discovery and recursive usage scans.
- **Server-selected Kubernetes targets** - kubelet usage selects the configured node or the API's only node. Custom-resource condition reads derive API paths from validated server configuration. Callers supply neither node names nor API targets.
- **Server-selected freshness markers** - `get_configured_freshness` accepts no path argument, resolves configured absolute paths beneath `ROOTFS`, and returns metadata without reading marker content.
- **Network-gated reach** - the endpoint is meant to sit behind the tailnet / node boundary, not public.
