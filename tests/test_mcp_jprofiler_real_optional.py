"""Optional real JProfiler MCP integration test.

This test starts the official JProfiler MCP server from a real MCP config and
prints the full health/tool discovery output. It does not use a fake JProfiler
server.

Run manually with:

LOOP_ENGINEER_JPROFILER_REAL=1 \
LOOP_ENGINEER_JPROFILER_CONFIG=.mcp.jprofiler.example.json \
.venv/bin/python -m pytest -q tests/test_mcp_jprofiler_real_optional.py -s

Optionally add:

LOOP_ENGINEER_RUNTIME_EVIDENCE_ZIP=/absolute/path/to/runtime-evidence-demo.zip
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
import zipfile

import pytest

from core.agent_loop import AgentConfig
from core.mcp import MCPManager, load_mcp_configs_from_file


def _real_enabled_or_skip() -> None:
    if os.environ.get("LOOP_ENGINEER_JPROFILER_REAL") != "1":
        pytest.skip("set LOOP_ENGINEER_JPROFILER_REAL=1 to run real JProfiler MCP")


def _evidence_inventory() -> dict | None:
    raw = os.environ.get("LOOP_ENGINEER_RUNTIME_EVIDENCE_ZIP")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        raise AssertionError(f"LOOP_ENGINEER_RUNTIME_EVIDENCE_ZIP does not exist: {path}")
    with zipfile.ZipFile(path) as zf:
        names = sorted(zf.namelist())
    return {
        "path": str(path),
        "entry_count": len(names),
        "entries": names,
    }


def _evidence_zip_or_skip() -> Path:
    raw = os.environ.get("LOOP_ENGINEER_RUNTIME_EVIDENCE_ZIP")
    if not raw:
        pytest.skip("set LOOP_ENGINEER_RUNTIME_EVIDENCE_ZIP to run real analysis")
    path = Path(raw)
    if not path.is_file():
        raise AssertionError(f"LOOP_ENGINEER_RUNTIME_EVIDENCE_ZIP does not exist: {path}")
    return path


def _extract_heap_dump(zip_path: Path, output_dir: Path) -> Path:
    with zipfile.ZipFile(zip_path) as zf:
        candidates = [
            name
            for name in zf.namelist()
            if name.lower().endswith(".hprof") and not name.startswith("__MACOSX/")
        ]
        if len(candidates) != 1:
            raise AssertionError(
                f"expected exactly one real .hprof entry, found: {candidates}"
            )
        member = candidates[0]
        target = output_dir / Path(member).name
        with zf.open(member) as source, target.open("wb") as destination:
            while chunk := source.read(1024 * 1024):
                destination.write(chunk)
    return target


def _result_output(result) -> dict:
    return {
        "content": result.content,
        "is_error": result.is_error,
        "raw_content": result.raw_content,
        "structured_content": result.structured_content,
        "progress": [asdict(event) for event in result.progress],
    }


def _write_real_output(tmp_path: Path, output: dict) -> Path:
    configured = os.environ.get("LOOP_ENGINEER_JPROFILER_OUTPUT")
    output_path = Path(configured) if configured else tmp_path / "real-jprofiler-analysis.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"REAL_JPROFILER_ANALYSIS_OUTPUT={output_path}")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return output_path


async def test_real_jprofiler_mcp_starts_and_lists_tools(tmp_path):
    _real_enabled_or_skip()
    config_path = Path(
        os.environ.get("LOOP_ENGINEER_JPROFILER_CONFIG", ".mcp.jprofiler.example.json")
    )
    configs = load_mcp_configs_from_file(config_path)
    manager = MCPManager(configs, tool_wait_timeout=0.0)

    try:
        await manager.start()
        specs = await manager.list_tools()
        agent_tools = await AgentConfig(
            provider=None,
            system="x",
            model="m",
            max_tokens=1,
            mcp_manager=manager,
        ).resolve_tools()

        output = {
            "config": str(config_path),
            "health": [asdict(item) for item in manager.health()],
            "mcp_specs": [asdict(item) for item in specs],
            "agent_tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "mcp_info": tool.mcp_info,
                }
                for tool in agent_tools
                if tool.is_mcp
            ],
            "runtime_evidence_zip": _evidence_inventory(),
            "note": (
                "This proves the official JProfiler MCP server started and "
                "exposed tools. Evidence-specific tool calls must be added "
                "after reviewing the real tool names and schemas printed here."
            ),
        }

        output_path = tmp_path / "real-jprofiler-mcp-output.json"
        output_path.write_text(
            json.dumps(output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"REAL_JPROFILER_MCP_OUTPUT={output_path}")
        print(json.dumps(output, ensure_ascii=False, indent=2))

        assert specs, "JProfiler MCP started but returned no tools"
        assert any(tool.name.startswith("mcp__JProfiler__") for tool in agent_tools)
    finally:
        await manager.close()


async def test_real_jprofiler_analyzes_runtime_evidence_heap_dump(tmp_path):
    """真实 JProfiler:load -> status -> heap data,不使用 fake server。"""
    _real_enabled_or_skip()
    evidence_zip = _evidence_zip_or_skip()
    heap_dump = _extract_heap_dump(evidence_zip, tmp_path)
    config_path = Path(
        os.environ.get("LOOP_ENGINEER_JPROFILER_CONFIG", ".mcp.jprofiler.example.json")
    )
    observation_timeout = float(
        os.environ.get("LOOP_ENGINEER_JPROFILER_OBSERVATION_TIMEOUT", "900")
    )
    configs = load_mcp_configs_from_file(config_path)
    manager = MCPManager(configs, tool_wait_timeout=0.0)
    progress = []
    output = {
        "config": str(config_path),
        "evidence_zip": str(evidence_zip),
        "heap_dump": str(heap_dump),
        "heap_dump_bytes": heap_dump.stat().st_size,
        "observation_timeout_seconds": observation_timeout,
        "calls": [],
        "progress": progress,
    }

    def observe(event) -> None:
        record = asdict(event)
        progress.append(record)
        print("JPROFILER_PROGRESS=" + json.dumps(record, ensure_ascii=False))

    started_at = time.monotonic()
    try:
        await manager.start()
        async with asyncio.timeout(observation_timeout):
            load_result = await manager.call_tool(
                "JProfiler",
                "load_snapshot",
                {"filePath": str(heap_dump)},
                progress_callback=observe,
            )
            output["calls"].append(
                {"tool": "load_snapshot", "result": _result_output(load_result)}
            )

            status_result = None
            while time.monotonic() - started_at < observation_timeout:
                status_result = await manager.call_tool(
                    "JProfiler",
                    "check_status",
                    {},
                    progress_callback=observe,
                )
                output["calls"].append(
                    {"tool": "check_status", "result": _result_output(status_result)}
                )
                if "data_ready" in status_result.content:
                    break
                await asyncio.sleep(2)
            assert status_result is not None
            assert "data_ready" in status_result.content

            for arguments in (
                {"view": "biggest_objects"},
                {"view": "classes"},
            ):
                heap_result = await manager.call_tool(
                    "JProfiler",
                    "get_heap_data",
                    arguments,
                    progress_callback=observe,
                )
                output["calls"].append(
                    {
                        "tool": "get_heap_data",
                        "arguments": arguments,
                        "result": _result_output(heap_result),
                    }
                )
    except BaseException as exc:
        output["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        output["elapsed_seconds"] = time.monotonic() - started_at
        output["health"] = [asdict(item) for item in manager.health()]
        _write_real_output(tmp_path, output)
        await manager.close()

    assert len(output["calls"]) >= 4
    assert all(not call["result"]["is_error"] for call in output["calls"])
