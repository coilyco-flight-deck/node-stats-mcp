# Networking tools

Read-only, like every tool here. Part of [host tools](tools-host.md).

- **get_conntrack** - netfilter connection tracking: `count` against `max`, and every per-CPU error column the running kernel publishes, summed.
- **get_socket_states** - TCP socket counts by state, plus ephemeral port usage against the configured range.
- **get_network_info** - aggregate and per-interface I/O counters, filtered.

## Counters and gauges are different readings

This is the distinction the whole surface turns on, and getting it wrong is how a clean reading becomes false evidence.

**Counters** accumulate and never reset while the node is up: `insert_failed`, `drop`, `early_drop`, and the per-interface `errin` / `dropout` figures. An absolute value is close to meaningless, because a node whose uptime is days cannot say whether `flannel.1 dropout=6321` happened this morning or last month. **Only movement between two readings is interpretable.**

Because a counter holds every increment that happened inside an interval, a sample taken once a minute still preserves a three-second burst. For counters, a coarse interval loses nothing.

**Gauges** are instantaneous: `nf_conntrack_count`, ephemeral port usage, TCP state counts. These describe the moment they were read and nothing else. A table that fills and drains between two samples is invisible, and a clean gauge reading is **worse than no reading**, because it looks like evidence of absence.

The observed case: on the egress incident, one CI job finished a full install fan-out and the next could not complete one starting five seconds later, on the same runner. A condition that turns over in that gap is invisible at 60s and at 15s alike. Shrinking the sample interval does not fix a gauge, it only costs more ingest.

## Interface filtering

A k3s node carries one veth per pod. On kai-server that is 140-plus interfaces and around 20 KB of response, none of which an incident asks about.

`get_network_info(interfaces=...)` takes:

- `"default"` - drops the virtual churn, keeping the interfaces that carry real traffic
- `"all"` - every interface
- a comma-separated list, such as `"enp1s0,cni0,flannel.1,tailscale0"`

The `filtering` block in the response always states what was left out and how many. A caller must never be unable to tell a filtered response from the whole picture. `NODE_STATS_VIRTUAL_INTERFACE_PREFIXES` overrides the dropped prefixes per deployment.

## Why procfs and not the conntrack binary

`get_conntrack` reads `/proc/net/stat/nf_conntrack`, `nf_conntrack_count` and `nf_conntrack_max` directly. `conntrack -S` returns the same numbers and would add a binary to the image for nothing.

**The per-CPU table is parsed by column name, from the file's own header line.** The column set differs between kernel versions, so indexing by position works on the kernel it was written against and returns confident nonsense on any other. Columns the parser does not recognise are summed and returned anyway, because a newer kernel's extra counter is still a counter.

## What these tools depend on

Every read resolves through `ROOTFS`, which the deployment mounts at `/host` from the node's `/`. Reading bare `/proc` would work today only because the pod runs `hostNetwork`, and would silently start describing the pod's own namespace if that ever changed. That is the same class of silent wrongness as the column-order trap above.

The node view needs `hostNetwork` for the network namespace and `hostPID` for process attribution. Without them these tools describe the pod, and say so in their notes rather than reporting a confident zero. `get_conntrack` reports a note rather than zeroes when the module is unloaded or the mount is absent, because an absent table and an idle one are not the same reading.

## See also

- [host tools](tools-host.md) - the rest of the node surface.
- [OTLP export](otlp-export.md) - the periodic snapshot that carries counters into SigNoz.
