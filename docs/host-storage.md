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
   provide `root`, `var`, `var-lib`, `k3s`, `k3s-storage`, and `pod-ephemeral`.
   `pod-ephemeral` covers `emptyDir` and pod scratch under
   `/var/lib/kubelet/pods`, which no other profile attributes.
5. Trust a total only when `snapshot.complete` is true. Otherwise
   `totals_are_lower_bounds` is true and the response retains the cause.

A profile with `max_depth` above 1 nests `children` inside `children`, so one
call answers what used to take a `du` per level. `k3s-storage` ships at depth 3,
which reaches a claim's `data/attachments` without entering the pod.

The result limit clips returned child detail at every depth. It does not change
cached totals.

## Snapshot contract

What a snapshot promises and when it is a lower bound:
[snapshot contract](host-storage-snapshot.md).
