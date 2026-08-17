# Host log and deleted-file attribution

The two attribution surfaces beside the usage snapshot.

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
