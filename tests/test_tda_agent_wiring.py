"""TDA MCP wiring for the agent entrypoint."""
from __future__ import annotations

import json

import pytest

from config import get_settings
from core.mcp import MCPServerState
from main import build_mcp_manager_from_settings


def test_tda_settings_are_read_from_environment(monkeypatch):
    monkeypatch.setenv("LOOP_ENGINEER_MCP_CONFIG_PATH", "/opt/project/.mcp.json")
    monkeypatch.setenv("LOOP_ENGINEER_MCP_TOOL_WAIT_TIMEOUT", "4")
    monkeypatch.setenv("LOOP_ENGINEER_TDA_ENABLED", "true")
    monkeypatch.setenv("LOOP_ENGINEER_TDA_JAR_PATH", "/opt/tools/tda-3.2.jar")
    monkeypatch.setenv("LOOP_ENGINEER_TDA_TIMEOUT", "60")
    monkeypatch.setenv("LOOP_ENGINEER_TDA_TOOL_WAIT_TIMEOUT", "3")

    settings = get_settings()

    assert settings.tda_enabled is True
    assert settings.mcp_config_path == "/opt/project/.mcp.json"
    assert settings.mcp_tool_wait_timeout == 4
    assert settings.tda_jar_path == "/opt/tools/tda-3.2.jar"
    assert settings.tda_timeout == 60
    assert settings.tda_tool_wait_timeout == 3


def test_main_builds_tda_mcp_manager_only_when_enabled(monkeypatch):
    monkeypatch.setenv("LOOP_ENGINEER_TDA_ENABLED", "true")
    monkeypatch.setenv("LOOP_ENGINEER_TDA_JAR_PATH", "/opt/tools/tda-3.2.jar")

    manager = build_mcp_manager_from_settings(get_settings())

    assert manager is not None
    health = manager.health()
    assert len(health) == 1
    assert health[0].name == "tda"
    assert health[0].state == MCPServerState.DISCONNECTED
    assert manager._tool_wait_timeout == 5.0


def test_main_builds_mcp_manager_from_config_file(tmp_path, monkeypatch):
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "tda": {
                        "command": "java",
                        "args": ["-jar", "/opt/tools/tda.jar", "--mcp"],
                        "timeout": 60,
                    },
                    "log": {
                        "command": "python",
                        "args": ["log_server.py"],
                        "disabled": True,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOOP_ENGINEER_MCP_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("LOOP_ENGINEER_MCP_TOOL_WAIT_TIMEOUT", "2")

    manager = build_mcp_manager_from_settings(get_settings())

    assert manager is not None
    assert manager._tool_wait_timeout == 2
    health = manager.health()
    assert [item.name for item in health] == ["log", "tda"]
    assert health[0].state == MCPServerState.DISABLED


def test_main_builds_mcp_manager_from_multiple_config_items(tmp_path, monkeypatch):
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared": {"command": "old", "args": []},
                    "tda": {"command": "java", "args": ["-jar", "tda.jar", "--mcp"]},
                }
            }
        ),
        encoding="utf-8",
    )
    override = json.dumps(
        {
            "mcpServers": {
                "shared": {"command": "new", "args": ["server.py"]},
            }
        }
    )
    monkeypatch.setenv("LOOP_ENGINEER_MCP_CONFIG", json.dumps([str(config_path), override]))

    manager = build_mcp_manager_from_settings(get_settings())

    assert manager is not None
    assert sorted(manager._configs) == ["shared", "tda"]
    assert manager._configs["shared"].command == "new"
    assert manager._configs["shared"].args == ["server.py"]


def test_main_leaves_mcp_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LOOP_ENGINEER_TDA_ENABLED", raising=False)
    monkeypatch.delenv("LOOP_ENGINEER_TDA_JAR_PATH", raising=False)

    assert build_mcp_manager_from_settings(get_settings()) is None


def test_main_requires_tda_jar_path_when_enabled(monkeypatch):
    monkeypatch.setenv("LOOP_ENGINEER_TDA_ENABLED", "true")
    monkeypatch.delenv("LOOP_ENGINEER_TDA_JAR_PATH", raising=False)

    with pytest.raises(ValueError, match="LOOP_ENGINEER_TDA_JAR_PATH"):
        build_mcp_manager_from_settings(get_settings())
