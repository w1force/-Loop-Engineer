"""Bash 工具测试:执行/输出/退出码/stderr/超时/截断,并经统一入口 executor 跑通。

需要环境有 bash 和 python3。
"""
import asyncio
from pathlib import Path

import pytest

from core.builtin_tools import BASH_TOOL
from core.builtin_tools.bash import MAX_OUTPUT_CHARS, BashInput, _bash_func
from core.registry import get_all_base_tools
from core.tool_executor import make_executor
from core.tools import ToolContext, default_can_use_tool
from core.types import AgentState, ToolResultBlock, ToolUseBlock
from telemetry.tracer import NoopTracer


def _ctx(cwd: str = "") -> ToolContext:
    return ToolContext(
        tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=AgentState(cwd=cwd)
    )


# ── 注册 & 属性 ─────────────────────────────────────────
def test_registry_includes_bash():
    assert "Bash" in {t.name for t in get_all_base_tools()}


def test_bash_is_write_tool_exclusive():
    # 有副作用 → 非并发安全(写工具,executor 串行独占)
    assert BASH_TOOL.is_concurrency_safe is False


# ── 执行 & 输出 ─────────────────────────────────────────
async def test_bash_echo():
    out = await _bash_func(BashInput(command="echo hello"), _ctx())
    assert out.strip() == "hello"


async def test_bash_python_output():
    out = await _bash_func(BashInput(command="python3 -c 'print(2+2)'"), _ctx())
    assert "4" in out


async def test_bash_captures_stderr():
    out = await _bash_func(
        BashInput(command="python3 -c 'import sys; sys.stderr.write(\"errmsg\")'"), _ctx()
    )
    assert "errmsg" in out


async def test_bash_nonzero_exit_code_reported():
    out = await _bash_func(BashInput(command="exit 3"), _ctx())
    assert "[退出码: 3]" in out


async def test_bash_pipe_works():
    # 保留 shell 能力(管道)
    out = await _bash_func(BashInput(command="printf 'a\\nb\\nc\\n' | wc -l"), _ctx())
    assert "3" in out


async def test_bash_starts_in_agent_state_cwd(tmp_path):
    out = await _bash_func(BashInput(command="pwd -P"), _ctx(str(tmp_path)))
    assert Path(out.strip()).resolve() == tmp_path.resolve()


# ── 执行边界:超时 & 截断 ────────────────────────────────
async def test_bash_timeout():
    with pytest.raises(ValueError, match="超时"):
        await _bash_func(BashInput(command="sleep 2", timeout=100), _ctx())


async def test_bash_output_truncated():
    n = MAX_OUTPUT_CHARS + 5000
    out = await _bash_func(BashInput(command=f"python3 -c \"print('x'*{n})\""), _ctx())
    assert "已截断" in out
    # 截断后主体不超过上限(容错截断提示语)
    assert len(out) < MAX_OUTPUT_CHARS + 200


# ── 端到端:经统一入口 executor ─────────────────────────
async def test_bash_via_executor():
    ctx = _ctx()
    ex = make_executor("batch", [BASH_TOOL], default_can_use_tool, NoopTracer(), ctx)
    ex.add_tool(ToolUseBlock(id="b1", name="Bash", input={"command": "echo via-executor"}))
    results = await ex.get_results()
    assert len(results) == 1 and not results[0].is_error
    content = results[0].content
    content = content if isinstance(content, str) else str(content)
    assert "via-executor" in content
