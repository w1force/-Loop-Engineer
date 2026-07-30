"""Optional real TDA MCP integration test.

This test intentionally uses the real TDA jar and a real Java thread dump. It
does not use a fake MCP server.

Run manually with:

LOOP_ENGINEER_TDA_JAR=/absolute/path/to/tda.jar \
LOOP_ENGINEER_TDA_EVIDENCE_ZIP="/absolute/path/to/runtime-evidence-demo.zip" \
.venv/bin/python -m pytest -q tests/test_mcp_tda_real_optional.py -s
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.agent_loop import AgentConfig
from core.mcp import (
    MCPManager,
    build_tda_mcp_config,
    extract_tda_thread_dump_from_zip,
)


def _env_file_or_skip(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set")
    path = Path(value)
    if not path.is_file():
        pytest.skip(f"{name} does not exist: {path}")
    return path


async def test_real_tda_parses_runtime_evidence_thread_dump(tmp_path):
    jar = _env_file_or_skip("LOOP_ENGINEER_TDA_JAR")
    evidence_zip = _env_file_or_skip("LOOP_ENGINEER_TDA_EVIDENCE_ZIP")
    thread_dump = extract_tda_thread_dump_from_zip(evidence_zip, tmp_path)

    manager = MCPManager([build_tda_mcp_config(jar, timeout=60.0)])
    try:
        await manager.start()
        specs = await manager.list_tools()
        names = {spec.name for spec in specs}
        assert {"parse_log", "get_summary", "check_deadlocks"} <= names
        agent_tools = await AgentConfig(
            provider=None,
            system="x",
            model="m",
            max_tokens=1,
            mcp_manager=manager,
        ).resolve_tools()
        assert "mcp__tda__parse_log" in {tool.name for tool in agent_tools}

        outputs = {
            "thread_dump": str(thread_dump),
            "tools": sorted(names),
            "parse_log": (
                await manager.call_tool(
                    "tda",
                    "parse_log",
                    {"path": str(thread_dump)},
                )
            ).content,
            "get_summary": (await manager.call_tool("tda", "get_summary", {})).content,
            "check_deadlocks": (
                await manager.call_tool("tda", "check_deadlocks", {})
            ).content,
            "find_long_running": (
                await manager.call_tool("tda", "find_long_running", {})
            ).content,
            "clear": (await manager.call_tool("tda", "clear", {})).content,
        }

        output_path = tmp_path / "real-tda-output.json"
        output_path.write_text(
            json.dumps(outputs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"REAL_TDA_OUTPUT={output_path}")
        print(json.dumps(outputs, ensure_ascii=False, indent=2))

        assert "Successfully parsed log file" in outputs["parse_log"]
        assert "threadCount" in outputs["get_summary"]
        assert "No deadlocks found" in outputs["check_deadlocks"]
    finally:
        await manager.close()
