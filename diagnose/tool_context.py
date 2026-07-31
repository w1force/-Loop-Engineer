"""诊断控制工具使用的可信 AgentState 绑定检查。"""
from __future__ import annotations

from typing import Literal

from core.tools import ToolContext
from core.types import AgentState
from diagnose.session import DiagnosisSession


def require_diagnosis_state(
    tc: ToolContext,
    *,
    actor: Literal["diagnostician", "reviewer"],
) -> tuple[AgentState, DiagnosisSession]:
    state = tc.agent_state
    if state is None or state.diagnose_session is None:
        raise RuntimeError("diagnosis session is not bound to AgentState")
    if state.diagnose_actor != actor:
        raise RuntimeError(f"diagnosis tool requires actor={actor!r}")
    return state, state.diagnose_session
