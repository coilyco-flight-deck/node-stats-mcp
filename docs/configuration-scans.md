# Scan configuration

Budgets and timeouts for the traversal tools. Each cap exists so one large
root or child cannot starve its siblings or outlive a request.

- `NODE_STATS_PRESSURE_PATHS` - colon-separated fixed paths for pressure scans, interpreted inside `ROOTFS`.
- `NODE_STATS_MAX_PRESSURE_CHILDREN_PER_ROOT` (default 1000) - one-level discovery cap for each configured pressure root.
- `NODE_STATS_MAX_DU_ENTRIES` (default 200000) - per-child traversal cap for pressure scans.
- `NODE_STATS_MAX_DU_TOTAL_ENTRIES` (default 200000) - shared traversal cap across all pressure children in one request.
- `NODE_STATS_DU_TIMEOUT_SECONDS` (default 10) - wall-clock cap for one pressure request; timeout is returned as root and child metadata.
- `NODE_STATS_HOST_USAGE_PROFILES` - JSON list of fixed usage profiles. Each object requires `name` and absolute `path`, with optional `exclude_paths`, `stale_after_seconds`, `max_entries`, `timeout_seconds`, `max_children`, and `max_depth`.
- `NODE_STATS_HOST_USAGE_MAX_DEPTH` (default 1) - reported nesting levels when a profile does not override it. Hard ceiling 5. Depth changes reporting granularity, not the walk.
- `NODE_STATS_HOST_USAGE_MAX_ENTRIES` (default 5000000) - background snapshot entry cap when a profile does not override it.
- `NODE_STATS_HOST_USAGE_TIMEOUT_SECONDS` (default 900) - background snapshot wall-clock cap when a profile does not override it.
- `NODE_STATS_HOST_USAGE_MAX_CHILDREN` (default 10000) - immediate-child discovery cap when a profile does not override it.
- `NODE_STATS_HOST_USAGE_STALE_SECONDS` (default 900) - cached snapshot freshness window when a profile does not override it.
- `NODE_STATS_HOST_LOG_PATHS` (default `/var/log`) - colon-separated fixed log roots.
- `NODE_STATS_JOURNAL_PATHS` (default `/var/log/journal:/run/log/journal`) - colon-separated fixed journald roots.
- `NODE_STATS_MAX_HOST_LOG_ENTRIES` (default 500000) - shared entry cap for one host-log request.
- `NODE_STATS_HOST_LOG_TIMEOUT_SECONDS` (default 30) - wall-clock cap for one host-log request.
- `NODE_STATS_MAX_HOST_LOG_CHILDREN` (default 1000) - immediate-child cap for each log or journald root.
- `NODE_STATS_MAX_DELETED_FILE_PIDS` (default 4096) - process cap for one deleted-file request.
- `NODE_STATS_MAX_DELETED_FILE_FDS_PER_PROCESS` (default 4096) - descriptor cap per process.
- `NODE_STATS_DELETED_FILE_TIMEOUT_SECONDS` (default 10) - wall-clock cap for one deleted-file request.
