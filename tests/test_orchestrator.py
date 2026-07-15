"""orchestrator: respx mock Anthropic 纯文本 SSE → query_loop 走 completed。

断言埋点序列 [TURN_START, PROVIDER_REQUEST, STREAM_END, TRANSITION(completed)]。
"""
import asyncio

import httpx
import pytest
import respx
from pydantic import BaseModel

from core.loop.orchestrator import QueryParams, query_loop
from core.provider_errors import TransientProviderError
from core.providers.anthropic import AnthropicAdapter
from core.tools import Tool
from core.types import (
    AssistantMessage,
    StreamEvent,
    TerminalReason,
    ToolResultBlock,
    UserMessage,
)
from telemetry.events import TraceKind
from telemetry.tracer import NoopTracer

BASE = "https://api.anthropic.com"

ANTHROPIC_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}\n'
    "\n"
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
    "\n"
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}\n'
    "\n"
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"世界"}}\n'
    "\n"
    'event: content_block_stop\n'
    'data: {"type":"content_block_stop","index":0}\n'
    "\n"
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n'
    "\n"
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n'
    "\n"
)


class SpyTracer(NoopTracer):
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _params(spy_tracer=None) -> QueryParams:
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    return QueryParams(
        messages=[UserMessage(content="你好")],
        system="be brief",
        model="claude-sonnet-4-6",
        max_tokens=128,
        provider=adapter,
        abort_signal=asyncio.Event(),
    )


@respx.mock
async def test_query_loop_pure_text_completes():
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=ANTHROPIC_SSE))
    spy = SpyTracer()
    out = [m async for m in query_loop(_params(), spy)]

    assts = [m for m in out if isinstance(m, AssistantMessage)]
    assert len(assts) == 1
    assert assts[0].content[0].text == "你好世界"
    assert assts[0].stop_reason == "end_turn"


@respx.mock
async def test_query_loop_trace_sequence_completes():
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=ANTHROPIC_SSE))
    spy = SpyTracer()
    async for _ in query_loop(_params(), spy):
        pass

    kinds = [e.kind for e in spy.events]
    assert TraceKind.TURN_START in kinds
    assert TraceKind.PROVIDER_REQUEST in kinds
    assert TraceKind.STREAM_END in kinds
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions, "should emit at least one TRANSITION"
    assert transitions[-1].payload["reason"] == "completed"


@respx.mock
async def test_query_loop_pure_text_no_tool_execution():
    """无 tool_use → 不进入 executor.get_results 分支,直接 completed。

    验证 executor 接线不破坏纯文本路径:即使每轮都构造 executor,
    只要 provider 不产 tool_use,就不会调用 get_results / 不会 needs_follow_up。
    """
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=ANTHROPIC_SSE))
    spy = SpyTracer()
    out = [m async for m in query_loop(_params(), spy)]
    # 无 tool_use → 不进入 get_results 分支,直接 completed
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions[-1].payload["reason"] == "completed"


# --- Task 9: query_loop 主干集成(try/except + withheld 优先 + 清零) ---


class _ScriptedProvider:
    """按脚本依次返回事件 async-iterator 或抛 Exception。"""
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = 0

    def stream(self, **kwargs):
        i = self.calls
        self.calls += 1
        item = self.scripts[i]
        if isinstance(item, Exception):
            raise item
        return item  # async iterator

    def count_tokens(self, messages):
        return 0


def _text_events_async(text="ok", stop="end_turn"):
    async def _g():
        for e in [
            StreamEvent(type="message_start"),
            StreamEvent(type="content_block_start", index=0, block={"type": "text", "text": ""}),
            StreamEvent(type="content_block_delta", index=0, delta={"text": text}),
            StreamEvent(type="content_block_stop", index=0),
            StreamEvent(type="message_delta", delta={"stop_reason": stop},
                        message={"usage": {"input_tokens": 1, "output_tokens": 1}}),
            StreamEvent(type="message_stop"),
        ]:
            yield e
    return _g()


def _params_with(provider, spy_tracer=None) -> QueryParams:
    return QueryParams(
        messages=[UserMessage(content="hi")],
        system="", model="m", max_tokens=16,
        provider=provider, abort_signal=asyncio.Event(),
    )


async def _no_sleep(_s):
    """跳过真实退避(asyncio.sleep 的测试替身, 返回 awaitable)。"""
    return None


async def test_network_retry_then_success(monkeypatch):
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", _no_sleep)
    provider = _ScriptedProvider([TransientProviderError("conn"), _text_events_async("ok")])
    spy = SpyTracer()
    out = [m async for m in query_loop(_params_with(provider), spy)]
    assts = [m for m in out if isinstance(m, AssistantMessage)]
    assert len(assts) == 1 and assts[0].content[0].text == "ok"  # pyright: ignore[reportAttributeAccessIssue]
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions[-1].payload["reason"] == "completed"
    assert provider.calls == 2  # 第一次抖动, 第二次成功


async def test_network_retry_exhausted_terminal(monkeypatch):
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", _no_sleep)
    provider = _ScriptedProvider([TransientProviderError("x")] * 4)
    spy = SpyTracer()
    out = [m async for m in query_loop(_params_with(provider), spy)]
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions[-1].payload["reason"] == "model_error"
    assert provider.calls == 4  # 初试 + 3 次重试


async def test_max_tokens_escalate_then_success(monkeypatch):
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", _no_sleep)
    provider = _ScriptedProvider([
        _text_events_async("半句", stop="max_tokens"),
        _text_events_async("完整", stop="end_turn"),
    ])
    spy = SpyTracer()
    out = [m async for m in query_loop(_params_with(provider), spy)]
    assts = [m for m in out if isinstance(m, AssistantMessage)]
    # 最终轮输出完整(升档重发后)
    assert assts[-1].content[0].text == "完整"  # pyright: ignore[reportAttributeAccessIssue]
    assert provider.calls == 2


async def test_max_tokens_with_tool_use_does_not_execute(monkeypatch):
    """withheld 优先于 needs_follow_up: max_tokens + 有效 tool_use → 不走回灌(NEXT_TURN),
    直接升档(escalate); tool_result 不被收集进 messages。"""
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", _no_sleep)

    def _max_tokens_tooluse():
        async def _g():
            for e in [
                StreamEvent(type="message_start"),
                StreamEvent(type="content_block_start", index=0,
                            block={"type": "tool_use", "id": "c1", "name": "get", "input": {}}),
                # 完整可解析 JSON: 让 aggregate 真正产出 tool_use, 触发 needs_follow_up 候选
                StreamEvent(type="content_block_delta", index=0,
                            delta={"tool_input": '{"city": "x"}'}),
                StreamEvent(type="content_block_stop", index=0),
                StreamEvent(type="message_delta", delta={"stop_reason": "max_tokens"},
                            message={"usage": {"input_tokens": 1, "output_tokens": 1}}),
                StreamEvent(type="message_stop"),
            ]:
                yield e
        return _g()

    provider = _ScriptedProvider([_max_tokens_tooluse(), _text_events_async("done")])

    # 注册工具: 否则 needs_follow_up 分支与 withheld 分支终点相同, 测试退化为 smoke
    class _In(BaseModel):
        city: str

    async def _get(inp: _In, ctx) -> str:
        return f"weather in {inp.city}"

    tool = Tool(name="get", description="d", input_model=_In, func=_get,
                is_concurrency_safe=True)

    params = _params_with(provider)
    params.tools = [tool]
    spy = SpyTracer()
    out = [m async for m in query_loop(params, spy)]
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    # 升档(非 next_turn): 证明 withheld 优先于 needs_follow_up
    assert transitions[0].payload["reason"] == "max_output_tokens_escalate"
    assert transitions[-1].payload["reason"] == "completed"
    # tool_result 未被收集进 messages(不回灌执行)
    tool_results = [
        b for m in out if isinstance(m, UserMessage)
        for b in (m.content if isinstance(m.content, list) else [])
        if isinstance(b, ToolResultBlock)
    ]
    assert tool_results == []


async def test_prompt_too_long_terminal(monkeypatch):
    from core.provider_errors import PromptTooLongError
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", _no_sleep)
    provider = _ScriptedProvider([PromptTooLongError("too long", status=400)])
    spy = SpyTracer()
    out = [m async for m in query_loop(_params_with(provider), spy)]
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions[-1].payload["reason"] == "prompt_too_long"


async def test_programming_bug_not_swallowed(monkeypatch):
    """非 ProviderError(编程 bug)不被 except 吞, 照常冒泡。"""
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", _no_sleep)

    class _BugProvider:
        def stream(self, **kwargs):
            raise KeyError("bug")  # 非 ProviderError
        def count_tokens(self, messages):
            return 0

    with pytest.raises(KeyError):
        async for _ in query_loop(_params_with(_BugProvider()), SpyTracer()):
            pass


# --- I-1: withheld 路径必须 discard executor, 防 tool task 泄漏 ---

async def test_withheld_path_discards_executor_tool_task(monkeypatch):
    """withheld(max_tokens + tool_use) 是 Continue 路径, 必须 discard executor,
    否则 streaming 模式下 _on_add→create_task 启动的在途 tool task 成孤儿运行。

    用挂起工具(永不自行完成)+ max_tokens withhold → 升档 → 第二轮纯文本 completed。
    断言: (1) withheld 轮 executor 被 discard; (2) 其在途 task 被取消(done 且
    cancelled), 不是 orphan(未 done)。反向验证: 移除 orchestrator withheld 分支的
    executor.discard() → (1) executor._discarded=False; (2) task 悬挂不 done → 失败。
    """
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", _no_sleep)

    # 挂起工具: 卡住直到被 discard 取消(永不自行完成)
    async def _hang(inp, ctx) -> str:
        await asyncio.Event().wait()  # 永久阻塞, 仅 CancelledError 能打断
        return ""  # 不可达; 仅满足 Tool.func 返回类型校验

    class _In(BaseModel):
        city: str

    tool = Tool(name="hang", description="d", input_model=_In, func=_hang,
                is_concurrency_safe=True)

    def _max_tokens_tooluse():
        async def _g():
            for e in [
                StreamEvent(type="message_start"),
                StreamEvent(type="content_block_start", index=0,
                            block={"type": "tool_use", "id": "c1", "name": "hang",
                                   "input": {"city": "x"}}),
                StreamEvent(type="content_block_delta", index=0,
                            delta={"tool_input": '{"city": "x"}'}),
                StreamEvent(type="content_block_stop", index=0),
                StreamEvent(type="message_delta", delta={"stop_reason": "max_tokens"},
                            message={"usage": {"input_tokens": 1, "output_tokens": 1}}),
                StreamEvent(type="message_stop"),
            ]:
                yield e
        return _g()

    provider = _ScriptedProvider([_max_tokens_tooluse(), _text_events_async("done")])

    # 捕获 orchestrator 每轮创建的 executor, 以便观察 withheld 轮 task 状态
    import core.loop.orchestrator as orch_mod
    captured_executors = []
    real_make_executor = orch_mod.make_executor

    def _capturing_make_executor(mode, tools, can_use_tool, tracer, ctx):
        ex = real_make_executor(mode, tools, can_use_tool, tracer, ctx)
        captured_executors.append(ex)
        return ex

    monkeypatch.setattr(orch_mod, "make_executor", _capturing_make_executor)

    params = _params_with(provider)
    params.tools = [tool]
    params.tool_execution_mode = "streaming"  # 关键: add_tool 即 create_task(fire-and-forget)
    spy = SpyTracer()

    out = [m async for m in query_loop(params, spy)]

    # 升档 + 最终 completed: 走了 withheld 路径
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions[0].payload["reason"] == "max_output_tokens_escalate"
    assert transitions[-1].payload["reason"] == "completed"

    # query_loop 每轮重建 executor: [0]=withheld 轮, [1]=completed 轮
    assert len(captured_executors) == 2
    withheld_executor = captured_executors[0]

    # 核心断言 1: withheld 轮 executor 已被 discard(与 except 路径对称)
    assert withheld_executor._discarded is True, \
        "withheld 路径必须 discard executor, 否则 tool task 泄漏"

    # 核心断言 2: streaming 模式下 add_tool 已 create_task, 该 task 必须被 cancel
    tracked = withheld_executor._tracked
    assert len(tracked) == 1 and tracked[0].task is not None, \
        "streaming executor 应在 add_tool 时 fire-and-forget 启动 task"
    hang_task = tracked[0].task
    # 让事件循环推进: 若被 discard 取消, task 落地为 cancelled; 若 orphan 则悬挂不 done
    try:
        await hang_task
    except asyncio.CancelledError:
        pass
    assert hang_task.done(), "task 必须 done(被 cancel), 不得 orphan 悬挂"
    assert hang_task.cancelled(), "withheld 轮 tool task 必须被 cancel"

