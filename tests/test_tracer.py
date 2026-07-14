"""Tracer: NoopTracer/LoggingTracer 行为 + RemoteTracer 占位抛错。"""
import logging

import pytest

from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import LoggingTracer, NoopTracer, RemoteTracer


def test_noop_tracer_silent():
    t = NoopTracer()
    t.emit(TraceEvent(kind=TraceKind.TURN_START))  # 不抛
    child = t.child(depth=1)
    child.emit(TraceEvent(kind=TraceKind.STREAM_END))  # 不抛


def test_logging_tracer_logs_each_emit(caplog):
    with caplog.at_level(logging.INFO, logger="telemetry"):
        t = LoggingTracer({"chain_id": "abc"})
        t.emit(TraceEvent(kind=TraceKind.TURN_START, turn=1))
        t.emit(TraceEvent(kind=TraceKind.STREAM_END, payload={"stop_reason": "end_turn"}))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("turn_start" in m for m in msgs)
    assert any("stream_end" in m for m in msgs)


def test_logging_tracer_child_is_new_and_merges_ctx(caplog):
    parent = LoggingTracer({"chain_id": "abc"})
    child = parent.child(depth=2)
    assert child is not parent
    with caplog.at_level(logging.INFO, logger="telemetry"):
        child.emit(TraceEvent(kind=TraceKind.TRANSITION))
    last = caplog.records[-1].getMessage()
    assert "abc" in last and "depth" in last  # 继承 chain_id + 合并 depth=2


def test_remote_tracer_not_implemented():
    with pytest.raises(NotImplementedError):
        RemoteTracer(endpoint="http://localhost")
