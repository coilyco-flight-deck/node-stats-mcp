"""node-stats-mcp: a node-local MCP battery exposing host introspection over HTTP.

The generic node-introspection spine (instance #1 of the pattern discussed in
coilyco-gaming/eco-app#42): a small FastMCP server that reads the node it runs
on - CPU, memory, disk, load, network, processes, and bounded file metadata -
and serves it over streamable-HTTP so warded agents can inspect a node without a
host bind mount. Deployed as a hostPID+hostNetwork pod pinned to one node (the
node-exporter shape).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
