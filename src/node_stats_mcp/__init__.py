"""node-stats-mcp: a node-local MCP battery exposing host introspection over HTTP.

The generic node-introspection spine, first instance of the upstream pattern:
a small FastMCP server that reads the node it runs on - CPU, memory, disk,
disk pressure, load, network, processes, k3s pod and volume attribution, and
bounded file metadata - and serves it over streamable-HTTP. Deployed as a
hostPID+hostNetwork pod pinned to one node (the node-exporter shape).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
