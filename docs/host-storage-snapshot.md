# Snapshot contract

What `get_host_usage_breakdown` promises, and when a total is a lower bound
rather than complete.

`get_host_usage_breakdown(profile, limit, refresh)` returns cache state
immediately. One daemon worker refreshes a missing, stale, or explicitly
refreshed snapshot.

Each snapshot reports:

* **Identity** - profile, fixed path, filesystem id, mountpoint, source, type,
  filesystem root, and mount id.
* **Usage** - allocated and apparent bytes, entries, immediate children,
  errors, permission failures, duration, timeout, and truncation.
* **Trust** - status, completeness, lower-bound state, capture time, age,
  stale threshold, and active refresh.
* **Deduplication** - hard-linked file inodes count once, reported as
  `deduplicated_entries` and `hardlinked_inodes_tracked`. Other filesystems are
  excluded. Same-filesystem bind or subtree mounts are reported and skipped.

Kubelet PVC bind mounts therefore do not count data already present beneath
the k3s local-path storage tree, and the `pod-ephemeral` profile measures only
`emptyDir` and pod scratch.

Only multiply-linked inodes enter the dedup set, so its size tracks hard links
rather than the tree. Tracking every inode cost roughly 160 MB per million
entries and killed the wide profiles at their configured budget
(`node-stats-mcp#26`).

`get_k3s_volume_usage` keeps its bounded Kubernetes join and fair per-volume
scan. It labels every volume, namespace rollup, and overall result as complete
or a lower bound. A matching profile schedules or returns the complete
background view under `host_usage_snapshot`.

Continued in [host log and deleted-file attribution](host-storage-logs.md).
