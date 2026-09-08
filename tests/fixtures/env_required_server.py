"""A minimal MCP server that refuses to start without a specific environment
variable — mimics the real failure mode of `brave/brave-search-mcp-server`
(requires `BRAVE_API_KEY`) and `financial-datasets/mcp-server` (requires
`FINANCIAL_DATASETS_API_KEY`), used to verify `run_fuzz`'s `env` handling
without depending on a real external API key."""

from __future__ import annotations

import os
import sys

if os.environ.get("REQUIRED_TEST_KEY") != "expected-value":
    print("Error: REQUIRED_TEST_KEY is required", file=sys.stderr)
    sys.exit(1)

from mcp.server.mcpserver import MCPServer

server = MCPServer("env-required-fixture")


@server.tool()
def ping() -> str:
    """Returns pong."""
    return "pong"


if __name__ == "__main__":
    server.run(transport="stdio")
