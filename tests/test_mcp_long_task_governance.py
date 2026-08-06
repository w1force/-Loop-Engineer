"""MCP 长任务治理协议级测试。"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

from core.mcp import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolCallOptions,
    MCPToolExecutionPolicy,
)
from core.mcp.errors import MCPConnectionClosedError, MCPToolTimeoutError
from core.mcp.factory import create_mcp_client

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_lifecycle_server.py"


def _manager(
    tmp_path: Path,
    *,
    control_timeout: float = 0.2,
    tool_timeout: float = 0.3,
    heartbeat: float = 0.02,
) -> MCPManager:
    return MCPManager(
        [
            MCPServerConfig(
                name="life",
                command=sys.executable,
                args=[str(FIXTURE)],
                env={"MCP_LIFECYCLE_LOG": str(tmp_path / "mcp.jsonl")},
                timeout=control_timeout,
            )
        ],
        execution_policy=MCPToolExecutionPolicy(
            timeout_seconds=tool_timeout,
            heartbeat_seconds=heartbeat,
        ),
    )


def _read_log(tmp_path: Path) -> list[dict]:
    path = tmp_path / "mcp.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_tool_timeout_is_independent_from_control_request_timeout(tmp_path):
    """把工具调用超时误用成控制请求 timeout,会让 0.1s 工具被 0.05s 误杀。"""
    manager = _manager(tmp_path, control_timeout=0.1, tool_timeout=0.3)

    try:
        await manager.start()
        result = await manager.call_tool(
            "life",
            "sleep_echo",
            {"value": "slow-but-valid", "delay": 0.15},
        )
    finally:
        await manager.close()

    assert result.content == "slow-but-valid"


@pytest.mark.asyncio
async def test_timeout_sends_cancelled_notification_with_exact_request_id(tmp_path):
    """去掉请求级取消通知,server 侧就收不到当前 request id 的 cancelled。"""
    manager = _manager(tmp_path, tool_timeout=0.05)

    try:
        await manager.start()
        with pytest.raises(MCPToolTimeoutError):
            await manager.call_tool(
                "life",
                "sleep_echo",
                {"value": "too-slow", "delay": 0.2},
            )
        await asyncio.sleep(0.02)
    finally:
        await manager.close()

    records = _read_log(tmp_path)
    calls = [r for r in records if r["type"] == "call"]
    cancellations = [r for r in records if r["type"] == "cancelled"]
    assert calls
    assert cancellations
    assert cancellations[0]["params"]["requestId"] == calls[0]["id"]
    assert "timed out" in cancellations[0]["params"]["reason"]


@pytest.mark.asyncio
async def test_late_response_after_timeout_does_not_pollute_next_call(tmp_path):
    """取消后的旧响应迟到时,不能被下一次工具调用捡走。"""
    manager = _manager(tmp_path, tool_timeout=0.05)

    try:
        await manager.start()
        with pytest.raises(MCPToolTimeoutError):
            await manager.call_tool(
                "life",
                "sleep_echo",
                {"value": "late", "delay": 0.12},
            )

        result = await manager.call_tool(
            "life",
            "sleep_echo",
            {"value": "fresh", "delay": 0.0},
        )
        await asyncio.sleep(0.14)
    finally:
        await manager.close()

    assert result.content == "fresh"


@pytest.mark.asyncio
async def test_concurrent_out_of_order_responses_route_by_request_id(tmp_path):
    """如果按“当前等待者”猜响应,乱序返回会把两个结果串错。"""
    manager = _manager(tmp_path, tool_timeout=0.3)

    try:
        await manager.start()
        slow = asyncio.create_task(
            manager.call_tool("life", "sleep_echo", {"value": "slow", "delay": 0.08})
        )
        fast = asyncio.create_task(
            manager.call_tool("life", "sleep_echo", {"value": "fast", "delay": 0.0})
        )
        slow_result, fast_result = await asyncio.gather(slow, fast)
    finally:
        await manager.close()

    assert slow_result.content == "slow"
    assert fast_result.content == "fast"


@pytest.mark.asyncio
async def test_server_progress_and_heartbeat_are_distinct(tmp_path):
    """心跳不能伪装成 server progress 写进最终 MCPToolResult.progress。"""
    manager = _manager(tmp_path, tool_timeout=0.3, heartbeat=0.02)
    events = []

    try:
        await manager.start()
        result = await manager.call_tool(
            "life",
            "sleep_echo",
            {"value": "done", "delay": 0.06, "progress": True},
            progress_callback=events.append,
        )
    finally:
        await manager.close()

    assert result.content == "done"
    assert [event.source for event in result.progress] == ["server"]
    assert any(event.source == "server" and event.message == "working:done" for event in events)
    heartbeats = [event for event in events if event.source == "heartbeat"]
    assert heartbeats
    assert all(event.elapsed_seconds is not None for event in heartbeats)
    assert all(event.received_server_progress is True for event in heartbeats)


@pytest.mark.asyncio
async def test_abort_signal_cancels_current_request_but_keeps_connection(tmp_path):
    """abort 只取消当前 request,不应直接杀掉健康 MCP server。"""
    manager = _manager(tmp_path, tool_timeout=0.3)
    abort_signal = asyncio.Event()

    try:
        await manager.start()
        task = asyncio.create_task(
            manager.call_tool(
                "life",
                "sleep_echo",
                {"value": "abort-me", "delay": 0.2},
                abort_signal=abort_signal,
            )
        )
        await asyncio.sleep(0.02)
        abort_signal.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        result = await manager.call_tool(
            "life",
            "sleep_echo",
            {"value": "still-usable", "delay": 0.0},
        )
    finally:
        await manager.close()

    assert result.content == "still-usable"
    records = _read_log(tmp_path)
    aborted_calls = [
        record
        for record in records
        if record["type"] == "call" and record["value"] == "abort-me"
    ]
    cancellations = [record for record in records if record["type"] == "cancelled"]
    assert len(aborted_calls) == 1
    assert len(cancellations) == 1
    assert cancellations[0]["params"]["requestId"] == aborted_calls[0]["id"]


@pytest.mark.asyncio
async def test_connection_close_marks_server_failed_and_clears_tools(tmp_path):
    """transport 关闭是 server 生命周期问题,Manager 需要清 cache 并进入 FAILED。"""
    manager = _manager(tmp_path, tool_timeout=0.3)

    try:
        await manager.start()
        assert manager.health()[0].state == MCPServerState.READY
        with pytest.raises(MCPConnectionClosedError):
            await manager.call_tool(
                "life",
                "sleep_echo",
                {"value": "__exit__", "delay": 0.0},
            )
        health = manager.health()[0]
    finally:
        await manager.close()

    assert health.state == MCPServerState.FAILED
    assert health.tool_count == 0
    assert health.error


@pytest.mark.asyncio
async def test_connection_close_fails_every_pending_request(tmp_path):
    """transport EOF 必须唤醒全部等待者,不能留下永远悬挂的 Future。"""
    manager = _manager(tmp_path, tool_timeout=0.5)

    try:
        await manager.start()
        closing = asyncio.create_task(
            manager.call_tool(
                "life",
                "sleep_echo",
                {"value": "__exit_after__", "delay": 0.05},
            )
        )
        pending = asyncio.create_task(
            manager.call_tool(
                "life",
                "sleep_echo",
                {"value": "must-not-hang", "delay": 0.3},
            )
        )
        results = await asyncio.wait_for(
            asyncio.gather(closing, pending, return_exceptions=True),
            timeout=0.3,
        )
    finally:
        await manager.close()

    assert len(results) == 2
    assert all(isinstance(result, MCPConnectionClosedError) for result in results)


@pytest.mark.asyncio
async def test_progress_callback_failure_is_logged_without_losing_result(
    tmp_path, caplog
):
    """观测回调有 bug 时,工具结果仍返回,同时日志不能静默吞错。"""
    manager = _manager(tmp_path, tool_timeout=0.3)

    def broken_callback(_event) -> None:
        raise RuntimeError("observer failed")

    caplog.set_level(logging.DEBUG, logger="core.mcp.execution")
    try:
        await manager.start()
        result = await manager.call_tool(
            "life",
            "sleep_echo",
            {"value": "result-survives", "delay": 0.04, "progress": True},
            progress_callback=broken_callback,
        )
    finally:
        await manager.close()

    assert result.content == "result-survives"
    assert "MCP progress callback failed for life.sleep_echo" in caplog.text
    assert "observer failed" in caplog.text


@pytest.mark.asyncio
async def test_stdio_transport_can_start_and_close_from_different_tasks(tmp_path):
    """官方 SDK 的 AnyIO cancel scope 必须由 transport owner task 成对退出。"""
    config = MCPServerConfig(
        name="life",
        command=sys.executable,
        args=[str(FIXTURE)],
        env={"MCP_LIFECYCLE_LOG": str(tmp_path / "mcp.jsonl")},
        timeout=0.2,
    )
    client = create_mcp_client(config)

    await asyncio.create_task(client.start())
    result = await client.call_tool(
        "sleep_echo",
        {"value": "cross-task", "delay": 0.0},
        options=MCPToolCallOptions(timeout_seconds=0.2),
    )
    await asyncio.create_task(client.close())

    assert result.content == "cross-task"
    assert client._transport_owner_task is None
    assert client._reader_task is None


@pytest.mark.asyncio
async def test_protocol_timeout_uses_the_same_domain_error(tmp_path):
    """SDK/protocol 内层先超时时也不能泄漏裸 asyncio.TimeoutError。"""
    config = MCPServerConfig(
        name="life",
        command=sys.executable,
        args=[str(FIXTURE)],
        env={"MCP_LIFECYCLE_LOG": str(tmp_path / "mcp.jsonl")},
        timeout=0.2,
    )
    client = create_mcp_client(config)

    try:
        await client.start()
        with pytest.raises(MCPToolTimeoutError) as error:
            await client.call_tool(
                "sleep_echo",
                {"value": "protocol-timeout", "delay": 0.1},
                options=MCPToolCallOptions(timeout_seconds=0.02),
            )
    finally:
        await client.close()

    assert error.value.server_name == "life"
    assert error.value.tool_name == "sleep_echo"
    assert error.value.timeout_seconds == 0.02


@pytest.mark.asyncio
async def test_late_response_is_observable_and_connection_remains_usable(
    tmp_path, caplog
):
    """已取消请求的迟到响应要隔离并留下 debug 证据。"""
    manager = _manager(tmp_path, tool_timeout=0.03)
    caplog.set_level(logging.DEBUG, logger="core.mcp.client")

    try:
        await manager.start()
        with pytest.raises(MCPToolTimeoutError):
            await manager.call_tool(
                "life",
                "sleep_echo",
                {"value": "late-observable", "delay": 0.08},
            )
        await asyncio.sleep(0.1)
        result = await manager.call_tool(
            "life",
            "sleep_echo",
            {"value": "still-routed", "delay": 0.0},
        )
    finally:
        await manager.close()

    assert result.content == "still-routed"
    assert "Discarding late or unknown MCP response" in caplog.text
