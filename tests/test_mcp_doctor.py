"""MCP doctor checks for local onboarding."""
from __future__ import annotations

from core.mcp.doctor import check_executable, check_jprofiler_config
from core.mcp.types import MCPServerConfig


def test_check_executable_reports_missing_binary(monkeypatch):
    monkeypatch.setenv("PATH", "")

    result = check_executable("npx")

    assert result.ok is False
    assert result.name == "npx"
    assert "not found" in result.message.lower()
    assert result.remediation is not None


def test_check_jprofiler_config_reports_missing_server():
    results = check_jprofiler_config([])

    assert results[0].ok is False
    assert results[0].name == "JProfiler config"
    assert "not configured" in results[0].message


def test_check_jprofiler_config_accepts_official_npx_wrapper(monkeypatch):
    monkeypatch.setattr("core.mcp.doctor.shutil.which", lambda name: f"/bin/{name}")

    results = check_jprofiler_config(
        [
            MCPServerConfig(
                name="JProfiler",
                command="npx",
                args=["-y", "@ej-technologies/jprofiler-mcp@latest"],
                timeout=300,
            )
        ]
    )

    assert all(item.ok for item in results)
    assert [item.name for item in results] == [
        "JProfiler config",
        "npx",
        "JProfiler npm package",
        "JProfiler timeout",
    ]


def test_check_jprofiler_config_reports_placeholder_local_path():
    results = check_jprofiler_config(
        [
            MCPServerConfig(
                name="JProfiler",
                command="/path/to/JProfiler/bin/jpmcp",
                args=["--filter", "com.example.app"],
                timeout=300,
            )
        ]
    )

    assert any(item.name == "JProfiler jpmcp" and item.ok is False for item in results)
