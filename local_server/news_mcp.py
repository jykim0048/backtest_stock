#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import subprocess
import os

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    # Auto-install mcp package if missing in the environment
    import subprocess
    print("mcp library not found. Installing fastmcp...", file=sys.stderr)
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "mcp"])
        from mcp.server.fastmcp import FastMCP
    except Exception as e:
        print(f"Failed to install mcp package: {e}", file=sys.stderr)
        sys.exit(1)

# Initialize FastMCP Server
mcp = FastMCP("stock-news")

@mcp.tool()
def get_stock_news(query: str) -> str:
    """
    Retrieve the latest stock news and automated sentiment analysis for a given stock name, ticker symbol, or code.
    Args:
        query: Stock name, code, or ticker symbol (e.g., '삼성전자', 'AAPL', '005930').
    """
    # Locate the existing get_news.py script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    script_path = os.path.join(parent_dir, ".agents", "skills", "get-stock-news", "scripts", "get_news.py")
    
    if not os.path.exists(script_path):
        return f"Error: get_news.py script not found at {script_path}"
        
    try:
        # Run the script with the query
        result = subprocess.run(
            [sys.executable, script_path, query],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )
        return result.stdout or result.stderr
    except Exception as e:
        return f"Error running get_news.py: {str(e)}"

if __name__ == "__main__":
    mcp.run()
