# Usage profiles

The fixed profiles `get_host_usage_breakdown` serves, and how to replace them.

`NODE_STATS_HOST_USAGE_PROFILES` replaces the defaults with JSON objects, each
requiring `name` and absolute `path`, optionally `exclude_paths`,
`stale_after_seconds`, `max_entries`, `timeout_seconds`, and `max_children`.

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
(`/var/lib/rancher/k3s/storage`), and `pod-ephemeral`
(`/var/lib/kubelet/pods`).

Global defaults for omitted profile fields are:

* `NODE_STATS_HOST_USAGE_MAX_ENTRIES` - 5,000,000 entries.
* `NODE_STATS_HOST_USAGE_TIMEOUT_SECONDS` - 900 seconds.
* `NODE_STATS_HOST_USAGE_MAX_CHILDREN` - 10,000 immediate children.
* `NODE_STATS_HOST_USAGE_STALE_SECONDS` - 900 seconds.

Hitting a cap makes the snapshot incomplete and every total a lower bound.

Continued in [log and deleted-file configuration](host-storage-logs-configuration.md).
