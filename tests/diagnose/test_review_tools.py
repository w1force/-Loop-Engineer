"""Reviewer 工具只接受受信任 reviewer AgentState。"""
import asyncio
import json

import pytest

from core.tools import ToolContext
from core.types import AgentState
from diagnose.api import create_diagnosis_session
from diagnose.agent_tools import diagnosis_control_tools
from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
from diagnose.platform_impl.java_jvm.platform import JavaJvmDiagnosticPlatform
from diagnose.registry import PlatformRegistry
from diagnose.review_tools import diagnosis_review_tools
from telemetry.tracer import NoopTracer


def _session(tmp_path):
    (tmp_path / "log.txt").write_text("x")
    registry = PlatformRegistry()
    registry.register(JavaJvmDiagnosticPlatform())
    return create_diagnosis_session(DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path), artifacts=[ArtifactRef(id="log", kind=ArtifactKind.LOG, path="log.txt")]), registry)


@pytest.mark.asyncio
async def test_reviewer_context_rejects_diagnostician_actor(tmp_path):
    session = _session(tmp_path)
    tool = next(tool for tool in diagnosis_review_tools() if tool.name == "GetDiagnosisReviewContext")
    context = ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=AgentState(diagnose_session=session, diagnose_actor="diagnostician"))
    with pytest.raises(RuntimeError, match="reviewer"):
        await tool.func(tool.input_model(), context)


@pytest.mark.asyncio
async def test_diagnosis_control_rejects_reviewer_actor(tmp_path):
    session = _session(tmp_path)
    tool = next(tool for tool in diagnosis_control_tools() if tool.name == "GetDiagnosisContext")
    context = ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=AgentState(diagnose_session=session, diagnose_actor="reviewer"))
    with pytest.raises(RuntimeError, match="diagnostician"):
        await tool.func(tool.input_model(), context)
