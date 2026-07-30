"""MCP config file loader."""
from __future__ import annotations

import json

import pytest

from core.mcp import MCPTransport
from core.mcp.config_loader import load_mcp_configs, load_mcp_configs_from_file


def test_loads_stdio_servers_from_mcp_json(tmp_path):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "tda": {
                        "type": "stdio",
                        "command": "java",
                        "args": ["-jar", "/opt/tools/tda.jar", "--mcp"],
                        "env": {"JAVA_HOME": "/opt/java"},
                        "timeout": 60,
                    },
                    "disabled-log": {
                        "type": "stdio",
                        "command": "python",
                        "args": ["log_server.py"],
                        "disabled": True,
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    configs = load_mcp_configs_from_file(config_path)

    assert [cfg.name for cfg in configs] == ["disabled-log", "tda"]
    tda = configs[1]
    assert tda.transport == MCPTransport.STDIO
    assert tda.command == "java"
    assert tda.args == ["-jar", "/opt/tools/tda.jar", "--mcp"]
    assert tda.env == {"JAVA_HOME": "/opt/java"}
    assert tda.timeout == 60
    assert configs[0].disabled is True


def test_expands_environment_variables_in_mcp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TDA_JAR_PATH", "/real/tda.jar")
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "tda": {
                        "command": "java",
                        "args": ["-jar", "${TDA_JAR_PATH}", "${MODE:---mcp}"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    configs = load_mcp_configs_from_file(config_path)

    assert configs[0].args == ["-jar", "/real/tda.jar", "--mcp"]


def test_missing_environment_variable_reports_clear_error(tmp_path):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "tda": {
                        "command": "java",
                        "args": ["-jar", "${TDA_JAR_PATH}", "--mcp"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Missing environment variables"):
        load_mcp_configs_from_file(config_path)


def test_rejects_unsupported_transport_until_client_exists(tmp_path):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported MCP transport"):
        load_mcp_configs_from_file(config_path)


def test_loads_and_merges_multiple_config_items(tmp_path):
    file_config = tmp_path / "mcp.json"
    file_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "log": {"command": "python", "args": ["log_server.py"]},
                    "shared": {"command": "old", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    json_config = json.dumps(
        {
            "mcpServers": {
                "shared": {"command": "new", "args": ["server.py"]},
                "tda": {"command": "java", "args": ["-jar", "tda.jar", "--mcp"]},
            }
        }
    )

    configs = load_mcp_configs([str(file_config), json_config])

    by_name = {cfg.name: cfg for cfg in configs}
    assert sorted(by_name) == ["log", "shared", "tda"]
    assert by_name["shared"].command == "new"
    assert by_name["shared"].args == ["server.py"]
