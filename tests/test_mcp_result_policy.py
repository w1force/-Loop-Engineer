"""MCP 结果治理策略测试。"""
from __future__ import annotations

from pathlib import Path

from core.mcp import MCPResultPolicy as ExportedMCPResultPolicy
from core.mcp.result_policy import MCPResultPolicy
from core.mcp.types import MCPToolResult


def test_small_mcp_result_stays_inline(tmp_path):
    policy = MCPResultPolicy(max_inline_chars=100, artifact_dir=tmp_path)

    result = policy.apply("logs", "trace", "short output")

    assert result.content == "short output"
    assert result.truncated is False
    assert result.artifact_path is None
    assert result.original_chars == len("short output")


def test_mcp_result_policy_is_exported_from_package():
    assert ExportedMCPResultPolicy is MCPResultPolicy


def test_large_mcp_result_is_truncated_and_written_to_artifact(tmp_path):
    policy = MCPResultPolicy(max_inline_chars=20, artifact_dir=tmp_path)
    content = "x" * 80

    result = policy.apply("logs", "trace", content)

    assert result.truncated is True
    assert result.original_chars == 80
    assert result.artifact_path is not None
    assert "MCP output truncated" in result.content
    assert "logs.trace" in result.content
    assert result.content.startswith("x" * 20)
    artifact = Path(result.artifact_path)
    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8") == content


def test_policy_unwraps_single_field_text_payload(tmp_path):
    policy = MCPResultPolicy(max_inline_chars=1000, artifact_dir=tmp_path)
    result = MCPToolResult(
        content='{"text": "root cause is timeout"}',
        structured_content={"text": "root cause is timeout"},
    )

    governed = policy.apply_result("logs", "diagnose", result)

    assert governed.content == "root cause is timeout"
    assert governed.structured_content == {"text": "root cause is timeout"}
    assert governed.truncated is False


def test_policy_formats_structured_json_stably(tmp_path):
    policy = MCPResultPolicy(max_inline_chars=1000, artifact_dir=tmp_path)
    result = MCPToolResult(
        content='{"b": 2, "a": 1}',
        structured_content={"b": 2, "a": 1},
    )

    governed = policy.apply_result("logs", "diagnose", result)

    assert governed.content == '{\n  "a": 1,\n  "b": 2\n}'
    assert governed.truncated is False


def test_policy_keeps_plain_text_result_unchanged(tmp_path):
    policy = MCPResultPolicy(max_inline_chars=1000, artifact_dir=tmp_path)
    result = MCPToolResult(
        content="hello",
        raw_content=[{"type": "text", "text": "hello"}],
    )

    governed = policy.apply_result("demo", "echo_text", result)

    assert governed.content == "hello"
    assert governed.raw_content == [{"type": "text", "text": "hello"}]
