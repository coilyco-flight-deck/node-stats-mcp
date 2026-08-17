# Log and deleted-file configuration

Settings for the two attribution surfaces beside usage profiles.

## Logs and journald

* `NODE_STATS_HOST_LOG_PATHS` - fixed roots, default `/var/log`.
* `NODE_STATS_JOURNAL_PATHS` - fixed roots, defaults
  `/var/log/journal:/run/log/journal`.
* `NODE_STATS_MAX_HOST_LOG_ENTRIES` - 500,000 entries across fair root slices.
* `NODE_STATS_HOST_LOG_TIMEOUT_SECONDS` - 30 seconds.
* `NODE_STATS_MAX_HOST_LOG_CHILDREN` - 1,000 immediate children per root.

Missing roots report zero complete usage. Invalid or escaped configured paths
make the overall response incomplete.

## Deleted open files

* `NODE_STATS_MAX_DELETED_FILE_PIDS` - 4,096 processes.
* `NODE_STATS_MAX_DELETED_FILE_FDS_PER_PROCESS` - 4,096 descriptors per
  process.
* `NODE_STATS_DELETED_FILE_TIMEOUT_SECONDS` - 10 seconds.

Permission failures, process churn, caps, and timeouts remain visible. Any
such condition makes the summary an explicit lower bound. Per-process
mountinfo distinguishes overlay and memory filesystems without guessing when
a filesystem is unknown.

See [host-storage.md](host-storage.md) for the operator workflow and trust
semantics.
