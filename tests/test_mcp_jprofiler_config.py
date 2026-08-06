"""JProfiler MCP config examples.

These tests only validate config shape. They do not claim that the real
JProfiler MCP server has started or analyzed runtime evidence.
"""
from __future__ import annotations

from pathlib import Path

from core.mcp.config_loader import load_mcp_configs_from_file
from core.mcp.types import MCPTransport


def test_jprofiler_example_config_loads_as_stdio_mcp_server():
    configs = load_mcp_configs_from_file(Path(".mcp.jprofiler.example.json"))

    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.name == "JProfiler"
    assert cfg.transport is MCPTransport.STDIO
    assert cfg.command == "npx"
    assert cfg.args == ["-y", "@ej-technologies/jprofiler-mcp@latest"]
    assert cfg.timeout >= 120
    assert cfg.disabled is False


def test_jprofiler_local_example_uses_placeholder_jpmcp_path():
    configs = load_mcp_configs_from_file(Path(".mcp.jprofiler.local.example.json"))

    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.name == "JProfiler"
    assert cfg.transport is MCPTransport.STDIO
    assert cfg.command == "/path/to/JProfiler/bin/jpmcp"
    assert cfg.args == ["--filter", "com.example.app"]
    assert cfg.timeout >= 120
