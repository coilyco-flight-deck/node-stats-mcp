"""Entrypoint so `python -m node_stats_mcp` and the console script both run the server."""

from node_stats_mcp.server import main

if __name__ == "__main__":
    main()
