# Usage profiles

The fixed profiles `get_host_usage_breakdown` serves, and how to replace them.

`NODE_STATS_HOST_USAGE_PROFILES` replaces the defaults with JSON objects, each
requiring `name` and absolute `path`, optionally `exclude_paths`,
`stale_after_seconds`, `max_entries`, `timeout_seconds`, `max_children`, and
`max_depth`. Depth changes reporting granularity, never the walk: totals and
entry counts are identical at every depth.

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
(`/var/lib`), `k3s` (`/var/lib/rancher/k3s`), `k3s-storage`
(`/var/lib/rancher/k3s/storage`, depth 3), and `pod-ephemeral`
(`/var/lib/kubelet/pods`, depth 3).

Depth 3 on `k3s-storage` reaches claim -> `data` -> `attachments`, so Forgejo's
managed-asset split is readable without an attended `kubectl exec`. The same
depth on `pod-ephemeral` reaches pod -> `volumes` -> volume type.

Global defaults for omitted profile fields are:

* `NODE_STATS_HOST_USAGE_MAX_ENTRIES` - 5,000,000 entries.
* `NODE_STATS_HOST_USAGE_TIMEOUT_SECONDS` - 900 seconds.
* `NODE_STATS_HOST_USAGE_MAX_CHILDREN` - 10,000 children per level.
* `NODE_STATS_HOST_USAGE_MAX_DEPTH` - 1 reported level, ceiling 5.
* `NODE_STATS_HOST_USAGE_STALE_SECONDS` - 900 seconds.

Hitting a cap makes the snapshot incomplete and every total a lower bound.

Continued in [log and deleted-file configuration](host-storage-logs-configuration.md).
