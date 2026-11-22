"""HTTP surfaces: web UI/API + git + token endpoint, the agents' MCP endpoint and their A2A cards."""
from .a2a import A2AHub, build_a2a_app
from .mcp import MCP_TOOLS, build_mcp
from .web import build_app, build_git_app, build_mcp_app, build_servers, build_token_app, serve

__all__ = ["MCP_TOOLS", "A2AHub", "build_a2a_app", "build_mcp", "build_app", "build_git_app",
           "build_mcp_app", "build_servers", "build_token_app", "serve"]
