"""node-stats-mcp: node-local host introspection over MCP and OTLP.

The generic node-introspection spine, first instance of the upstream pattern:
a small FastMCP server that reads the node it runs on - CPU, memory, disk,
disk pressure, load, network, processes, k3s pod and volume attribution, and
bounded file metadata - and serves it over streamable-HTTP. The same image can
run a separate bounded OTLP exporter process. Deployments pin both processes to
one node in the node-exporter shape.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
