# Host storage configuration

All paths and bounds are server-owned. Invalid names, paths, duplicate
profiles, root escapes, and exclusions outside a profile are rejected.

## Usage profiles

`NODE_STATS_HOST_USAGE_PROFILES` replaces the defaults with JSON objects. Each
object requires `name` and absolute `path`. Optional fields are
`exclude_paths`, `stale_after_seconds`, `max_entries`, `timeout_seconds`, and
`max_children`.

```json
[
  {
    "name": "root",
    "path": "/",
    "stale_after_seconds": 900,
    "max_entries": 5000000,
    "timeout_seconds": 900,
    "max_children": 10000
  },
  {
    "name": "k3s-storage",
    "path": "/var/lib/rancher/k3s/storage"
  }
]
```

The built-in profiles are `root` (`/`), `var` (`/var`), `var-lib`
(`/var/lib`), `k3s` (`/var/lib/rancher/k3s`), and `k3s-storage`
(`/var/lib/rancher/k3s/storage`).

Global defaults for omitted profile fields are:

* `NODE_STATS_HOST_USAGE_MAX_ENTRIES` - 5,000,000 entries.
* `NODE_STATS_HOST_USAGE_TIMEOUT_SECONDS` - 900 seconds.
* `NODE_STATS_HOST_USAGE_MAX_CHILDREN` - 10,000 immediate children.
* `NODE_STATS_HOST_USAGE_STALE_SECONDS` - 900 seconds.

Hitting a cap makes the snapshot incomplete and every total a lower bound.

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
