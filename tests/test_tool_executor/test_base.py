"""ToolExecutor 基类: _execute_single 七路径 / register_tool / get_results 保序 / discard。

asyncio_mode=auto: 测试用 async def + 直接 await, 不用 run_until_complete。
"""
import asyncio

import pytest
from pydantic import BaseModel

from core.tools import CanUseDecision, Tool, ToolContext, default_can_use_tool
from core.tool_executor.base import ToolExecutor, _to_result
from core.types import AgentState, TextBlock, ToolResultBlock, ToolUseBlock
from telemetry.tracer import NoopTracer


class _In(BaseModel):
    city: str


async def _ok(inp: _In, ctx) -> str:
    return f"{inp.city}: 晴"


async def _boom(inp: _In, ctx) -> str:
    raise RuntimeError("炸了")


async def _deny(tc: ToolUseBlock):
    return CanUseDecision(allow=False, reason="禁止")


def _ctx() -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=AgentState())


def _new_executor(tools=None, can_use_tool=default_can_use_tool):
    """基类是 ABC, 用一个最小子类驱动 get_results(直接 _execute_single 全跑)。"""
    class _AllSerial(ToolExecutor):
        def _on_add(self, tracked): ...
        async def _run_all(self):
            for t in self._tracked:
                if t.status == "queued":
                    await self._execute_single(t)

    return _AllSerial(can_use_tool, NoopTracer(), _ctx(), tools)


def _block(name="ok", input_=None, id_="c1"):
    return ToolUseBlock(id=id_, name=name, input=input_ if input_ is not None else {"city": "巴黎"})


async def test_register_and_get_results_str_ok():
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_ok)])
    ex.add_tool(_block())
    results = await ex.get_results()
    assert len(results) == 1
    assert results[0] == ToolResultBlock(tool_use_id="c1", content="巴黎: 晴")


async def test_unknown_tool_produces_error_in_add_tool():
    ex = _new_executor()  # 没注册任何工具
    ex.add_tool(_block(name="nope"))
    results = await ex.get_results()
    assert results[0].is_error is True
    assert "未知工具" in results[0].content


async def test_func_exception_produces_error():
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_boom)])
    ex.add_tool(_block())
    results = await ex.get_results()
    assert results[0].is_error is True
    assert "工具执行错误" in results[0].content


async def test_permission_denied_produces_error():
    ex = _new_executor(
        [Tool(name="ok", description="d", input_model=_In, func=_ok)], can_use_tool=_deny
    )
    ex.add_tool(_block())
    results = await ex.get_results()
    assert results[0].is_error is True
    assert results[0].content == "禁止"


async def test_validation_error_produces_error():
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_ok)])
    ex.add_tool(_block(input_={"not_a_city_field": 1}))  # 缺 city
    results = await ex.get_results()
    assert results[0].is_error is True
    assert "参数校验失败" in results[0].content


async def test_get_results_preserves_order():
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_ok)])
    for i in range(3):
        ex.add_tool(_block(id_=f"c{i}"))
    results = await ex.get_results()
    assert [r.tool_use_id for r in results] == ["c0", "c1", "c2"]


async def test_pre_execute_hook_rejection():
    async def _guard(inp, ctx):
        raise PermissionError("危险命令")

    ex = _new_executor(
        [Tool(name="ok", description="d", input_model=_In, func=_ok, pre_execute=_guard)]
    )
    ex.add_tool(_block())
    results = await ex.get_results()
    assert results[0].is_error is True
    assert "危险命令" in results[0].content


async def test_str_return_wraps_as_content_str():
    async def _s(inp, ctx):
        return inp.city

    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_s)])
    ex.add_tool(_block())
    results = await ex.get_results()
    assert results[0].content == "巴黎"  # str → content=str


async def test_discard_cancels_task():
    """discard 标记后 add_tool 无效, 且取消未完成 task。"""
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def _hang(inp, ctx):
        started.set()
        await proceed.wait()  # 阻塞直到被取消
        return "done"

    class _BgExecutor(ToolExecutor):
        """把执行放到后台 task 里, 测试 discard 能取消它。"""
        def _on_add(self, tracked): ...
        async def _run_all(self):
            for t in self._tracked:
                if t.status == "queued" and t.task is None:
                    t.task = asyncio.create_task(self._execute_single(t))

    ex = _BgExecutor(default_can_use_tool, NoopTracer(), _ctx(),
                     [Tool(name="ok", description="d", input_model=_In, func=_hang)])
    ex.add_tool(_block())
    await ex._run_all()  # 启动后台 task
    await started.wait()
    ex.discard()
    # 让 event loop 处理取消: CancelledError 需要一次调度才能落地到 task 状态
    task = ex._tracked[0].task
    assert task is not None
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.cancelled()
    # discard 后再 add 应无效
    ex.add_tool(_block(id_="c2"))
    assert len(ex._tracked) == 1


async def test_add_tool_prefills_placeholder_result():
    """TrackedTool 创建即带 is_error 占位 result(执行前)。"""
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_ok)])
    ex.add_tool(_block())
    assert ex._tracked[0].result.is_error is True
    assert ex._tracked[0].result.tool_use_id == "c1"
    assert ex._tracked[0].result.content == "tool execution interrupted"


async def test_get_results_count_equals_tracked_no_filter():
    """get_results 不再过滤 None: 返回数 == tracked 数。"""
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_ok)])
    for i in range(3):
        ex.add_tool(_block(id_=f"c{i}"))
    results = await ex.get_results()
    assert len(results) == 3
    # 成功执行后占位被真实 result 覆盖(is_error=False)
    assert all(r.tool_use_id.startswith("c") for r in results)


async def test_cancelled_keeps_placeholder_result():
    """CancelledError 路径不覆盖 result → 占位 is_error 保留, get_results 仍返回它。"""
    started = asyncio.Event()

    async def _hang(inp, ctx):
        started.set()
        await asyncio.Event().wait()  # 阻塞直到被取消
        return ""  # 不可达; 仅满足 Tool.func 返回类型校验

    class _Bg(ToolExecutor):
        def _on_add(self, tracked): ...

        async def _run_all(self):
            for t in self._tracked:
                if t.status == "queued" and t.task is None:
                    t.task = asyncio.create_task(self._execute_single(t))

    ex = _Bg(
        default_can_use_tool, NoopTracer(), _ctx(),
        [Tool(name="ok", description="d", input_model=_In, func=_hang)],
    )
    ex.add_tool(_block())
    await ex._run_all()
    await started.wait()
    ex.discard()
    task = ex._tracked[0].task
    assert task is not None
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.cancelled()
    # 占位保留(is_error), get_results 不过滤
    assert ex._tracked[0].result.is_error is True
    results = await ex.get_results()
    assert len(results) == 1
    assert results[0].is_error is True


def test_to_result_str():
    r = _to_result("c1", "hello")
    assert r.tool_use_id == "c1"
    assert r.content == "hello"


def test_to_result_single_textblock():
    r = _to_result("c1", TextBlock(text="hi"))
    assert r.content == [TextBlock(text="hi")]


def test_to_result_list_textblock():
    r = _to_result("c1", [TextBlock(text="a"), TextBlock(text="b")])
    assert r.content == [TextBlock(text="a"), TextBlock(text="b")]


def test_tool_result_block_rejects_arbitrary_dict():
    """收窄后, 任意 dict 不是合法 content block → pydantic 校验拒绝。"""
    with pytest.raises(Exception):
        ToolResultBlock(tool_use_id="c1", content=[{"filenames": ["a", "b"]}])  # type: ignore[reportArgumentType]  # 故意传非法 dict 验证运行时拒绝
