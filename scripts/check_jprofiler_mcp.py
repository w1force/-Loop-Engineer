#!/usr/bin/env python
"""Check local JProfiler MCP setup.

Default mode is read-only and does not start `npx`. Pass `--start` when you
explicitly want to launch the configured MCP server and list real tools.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mcp import MCPManager, load_mcp_configs_from_file  # noqa: E402
from core.mcp.doctor import check_executable, check_jprofiler_config  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check JProfiler MCP setup")
    parser.add_argument(
        "--config",
        default=".mcp.json",
        help="Path to a CCB-style MCP config file. Default: .mcp.json",
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="Actually start the configured JProfiler MCP server and list tools.",
    )
    return parser.parse_args()


async def main_async() -> int:
    args = parse_args()
    try:
        configs = load_mcp_configs_from_file(args.config)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    checks = [check_executable("node"), check_executable("npm")]
    checks.extend(check_jprofiler_config(configs))
    output: dict = {
        "ok": all(item.ok for item in checks),
        "config": args.config,
        "checks": [asdict(item) for item in checks],
    }

    if args.start:
        manager = MCPManager(configs, tool_wait_timeout=0.0)
        try:
            try:
                await manager.start()
            except Exception as exc:
                output["start_error"] = str(exc)
            health = manager.health()
            tools = await manager.get_ready_tools()
            output["health"] = [asdict(item) for item in health]
            output["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "is_mcp": tool.is_mcp,
                    "mcp_info": tool.mcp_info,
                }
                for tool in tools
            ]
            output["ok"] = output["ok"] and bool(tools)
        finally:
            await manager.close()

    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if output["ok"] else 1


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
