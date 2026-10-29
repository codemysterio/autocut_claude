"""MCP entry point for Claude Desktop / Claude Code: python run_mcp.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "backend"))

from autocut.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
