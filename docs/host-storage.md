# Host storage attribution

The host storage tools turn root filesystem pressure into a read-only path to
the physical owner. They use fixed profiles, Linux mount metadata, allocated
blocks, and explicit completeness state. Callers never supply a raw path.

## Walk from pressure to an owner

1. Call `get_filesystem_pressure` to confirm pressure and runway.
2. Call `get_host_usage_breakdown` with `profile: root`. The first call
   normally returns `snapshot_status: pending` and starts a background scan.
3. Poll the same profile until `refresh.running` is false.
4. Follow the largest child through another configured profile. The defaults
   provide `root`, `var`, `var-lib`, `k3s`, and `k3s-storage`.
5. Trust a total only when `snapshot.complete` is true. Otherwise
   `totals_are_lower_bounds` is true and the response retains the cause.

The result limit clips returned child detail only. It does not change cached
totals.

## Snapshot contract

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
* **Deduplication** - hard-linked file inodes count once. Other filesystems are
  excluded. Same-filesystem bind or subtree mounts are reported and skipped.

Kubelet PVC bind mounts therefore do not count data already present beneath
the k3s local-path storage tree.

`get_k3s_volume_usage` keeps its bounded Kubernetes join and fair per-volume
scan. It labels every volume, namespace rollup, and overall result as complete
or a lower bound. A matching profile schedules or returns the complete
background view under `host_usage_snapshot`.

## Logs and journald

`get_host_log_usage` scans fixed log and journald roots on a worker thread.
Nested journald roots are excluded from a parent log root and counted
separately. Allocated blocks keep sparse apparent size from masquerading as
physical usage. Every result carries completeness and lower-bound state.

## Deleted open files

`get_deleted_open_files` walks fixed `/proc/<pid>/fd` metadata on a worker
thread. It returns no filename and reads no target content. Repeated
descriptors for one device and inode count once.

The summary separates disk-backed reclaimable and still-linked files from
memfd, tmpfs, device, container-overlay, unknown-filesystem, and other
non-disk entries. Only `disk_backed_reclaimable_bytes` answers how much
ordinary disk allocation can return when owning processes close descriptors.

## Safety boundary

The tools do not accept raw paths, read scanned contents, run shell
passthroughs, delete files, prune storage, or restart workloads. They read only
fixed filesystem and proc metadata. Recursive work never runs on the MCP event
loop.

See [host-storage-configuration.md](host-storage-configuration.md) for profile
JSON, bounds, and every environment setting.
