# stream_turn 流式改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `stream_turn` 从累积模式(返回 `StreamOutcome` + `yielded` 透传)改造成 async generator(流式 yield StreamEvent + 末尾 yield StreamOutcome),删 `yielded` 冗余;query_loop 改 `async for` 消费 + 失败时 yield `Tombstone(turn_id)` 通知下游;submit 加 Tombstone/StreamEvent 分支。

**Architecture:** stream_turn 变 async gen(yield StreamEvent 实时 + 末尾 yield StreamOutcome 元数据,因 async gen 禁止 return value);query_loop `async for` 消费,显式 `yield outcome.assistant_msgs[0]` 给 submit(替代累积版 yielded 间接);失败(except/abort)yield `Tombstone(turn_id)` 让下游按 turn_id 丢弃本轮半截流。

**Tech Stack:** Python 3.10+、pydantic v2、pytest(asyncio_mode=auto)、asyncio。

## Global Constraints

(每个 task 隐式包含,值照 spec 抄)

- pytest `asyncio_mode = auto`:测试直接 `async def test_xxx()`,不加 `@pytest.mark.asyncio`。
- pyright `typeCheckingMode = "basic"` 必须通过。
- **不引入新依赖**。
- 留在 `feat/streaming-turn` 分支,不提交 main;每 task 末尾 commit。
- `Tombstone` 放 `core/types.py`(`@dataclass`,纯数据)。
- `turn_id`:query_loop 局部 int 递增,每次 stream_turn(含重试)+1。**不用 `state.turn_count`**(recovery 不递增会撞 id)。
- `stream_turn` 改 async generator:流式 `yield StreamEvent` + 末尾 `yield StreamOutcome`(替代 `return`)。
- **删 `StreamOutcome.yielded` 字段**(流式不累积)。
- query_loop 成功时显式 `yield outcome.assistant_msgs[0]`(整轮透传给 submit)。
- tombstone:except ProviderError → `yield Tombstone(turn_id)`;abort in `async for` → `yield Tombstone(turn_id)` + USER_INTERRUPT。
- submit 加 `Tombstone` 分支(不 append)+ `StreamEvent` 分支(本期 `continue` 留空,留扩展点)。
- 中文注释(对齐现有代码风格)。

## File Structure

| 文件 | 责任 | 动作 |
|------|------|------|
| `core/types.py` | 加 `Tombstone(turn_id)` | 改 |
| `core/loop/phases/stream_turn.py` | `stream_turn` 改 async gen + 删 `StreamOutcome.yielded` | 改 |
| `core/loop/orchestrator.py` | `query_loop` 改 `async for` + turn_id + tombstone + abort-in-loop + 显式整轮 yield | 改 |
| `core/agent_loop.py` | submit 加 `Tombstone` + `StreamEvent` 分支 | 改 |
| `tests/test_types.py` | Tombstone 构造测试 | 改 |
| `tests/test_stream_turn_executor.py` | stream_turn gen 消费测试(改) | 改 |
| `tests/test_orchestrator.py` | query_loop async for + tombstone 测试(改 + 新增) | 改 |
| `tests/test_agent_loop.py` | submit tombstone/streamevent 测试 | 改 |

---

### Task 1: Tombstone 类型

**Files:**
- Modify: `core/types.py`
- Test: `tests/test_types.py`

**Interfaces:**
- Produces: `Tombstone(turn_id: int)`(dataclass,query_loop yield 给下游,submit isinstance 判断)。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_types.py`)

```python
from core.types import Tombstone


def test_tombstone_holds_turn_id():
    t = Tombstone(turn_id=3)
    assert t.turn_id == 3
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_types.py::test_tombstone_holds_turn_id -v`
Expected: FAIL — `ImportError: cannot import name 'Tombstone'`。

- [ ] **Step 3: 实现**

`core/types.py` 顶部 import 加 `from dataclasses import dataclass`(若未有),文件末尾(Message 相关类之后)加:

```python
@dataclass
class Tombstone:
    """通知下游: turn_id 这一轮的流式 yield 作废(失败, 将重试或终止)。
    下游收到后丢弃该 turn_id 已收的 StreamEvent/AssistantMessage。
    重试/终止判断: 收到 tombstone 后有新轮(turn_id+1)=重试, loop 结束=终止。"""
    turn_id: int
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_types.py -v && pytest -q`
Expected: 新测通过 + 全量不破(Tombstone 是纯新增 dataclass)。

- [ ] **Step 5: commit**

```bash
git add core/types.py tests/test_types.py
git commit -m "feat: Tombstone(turn_id) 类型 (流式失败通知下游)"
```

---

### Task 2: stream_turn 改 async gen + 删 yielded + query_loop 改 async for(核心)

> stream_turn 改 gen 会立刻破 query_loop(`await stream_turn` 返回 generator 而非 StreamOutcome),两者必须同 task。本 task **不引入 tombstone**(Task 3 加),query_loop except 暂只 discard+handle_error。

**Files:**
- Modify: `core/loop/phases/stream_turn.py`、`core/loop/orchestrator.py`
- Test: `tests/test_stream_turn_executor.py`、`tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `Tombstone`(Task 1,本 task 暂不用)。
- Produces: `stream_turn` 为 async generator(yield `StreamEvent` + 末尾 `yield StreamOutcome`);`StreamOutcome` 无 `yielded` 字段;`query_loop` 用 `async for` 消费 stream_turn,显式 `yield outcome.assistant_msgs[0]` 透传整轮。

- [ ] **Step 1: 改 stream_turn 测试(从 await 返回 → async for 消费)**

`tests/test_stream_turn_executor.py` 现有 `test_stream_turn_feeds_executor_and_assembles_full_turn` 用 `outcome = await stream_turn(...)` + 断言 `outcome.yielded`。改成消费 async gen:

```python
async def test_stream_turn_gen_yields_stream_events_then_outcome():
    """stream_turn 是 async gen: 中途 yield StreamEvent, 末尾 yield StreamOutcome(含整轮)。"""
    class _FakeProvider:
        def stream(self, **kwargs):
            async def _g():
                for e in _seq_tool_use():   # 复用现有 _seq_tool_use(message_start/tool_use/content_block_stop/message_delta/stop)
                    yield e
            return _g()
        def count_tokens(self, messages): return 0

    state = State(messages=[UserMessage(content="hi")])
    params = QueryParams(
        messages=state.messages, system="", model="m", max_tokens=16,
        provider=_FakeProvider(), abort_signal=asyncio.Event(),
    )
    ctx = ToolContext(tracer=NoopTracer(), abort_signal=params.abort_signal)
    executor = StreamingToolExecutor(
        default_can_use_tool, NoopTracer(), ctx,
        [Tool(name="get", description="d", input_model=_In, func=_ok)],
    )

    events = []
    outcome = None
    async for m in stream_turn(state, params, NoopTracer(), executor):
        if isinstance(m, StreamOutcome):
            outcome = m
        else:
            events.append(m)   # StreamEvent

    # 中途 yield 了 StreamEvent
    assert any(e.type == "content_block_start" for e in events)
    # 末尾 yield StreamOutcome(整轮 + tool_calls)
    assert outcome is not None
    assert outcome.needs_follow_up is True
    assert [b.name for b in outcome.tool_calls] == ["get"]
    assert len(outcome.assistant_msgs) == 1
    assert outcome.assistant_msgs[0].stop_reason == "tool_use"
```

> 删原 `test_stream_turn_feeds_executor_and_assembles_full_turn`(基于 await + yielded)及任何断言 `outcome.yielded` 的代码。`StreamOutcome` 在测试文件顶部 import(`from core.loop.phases.stream_turn import StreamOutcome, stream_turn`)。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_stream_turn_executor.py -v`
Expected: FAIL — `stream_turn` 当前 `await` 返回 StreamOutcome(非 gen);`outcome.yielded` 已删/不存在。

- [ ] **Step 3: 改 stream_turn.py(stream_turn async gen + 删 yielded)**

`stream_turn` 函数体改成 async gen(删 `yielded` 局部变量 + 末尾 `return StreamOutcome(...)` 改 `yield StreamOutcome(...)`,中途 `yield item`):

```python
async def stream_turn(
    state: State,
    params: "QueryParams",
    tracer: Tracer,
    executor: "ToolExecutor | None",
):   # 不再 -> StreamOutcome(async generator)
    """调 provider.stream → aggregate_stream → 喂 executor + 组装整轮。

    流式版: 中途 yield StreamEvent(实时透传), 末尾 yield StreamOutcome(元数据, 替代 return)。
    async generator 不能 return value, 元数据用末尾 yield StreamOutcome 传出。
    """
    max_tokens = state.max_output_tokens_override or params.max_tokens
    events = params.provider.stream(
        messages=state.messages, system=params.system, tools=params.tools,
        model=params.model, max_tokens=max_tokens,
        abort_signal=params.abort_signal, tracer=tracer,
    )
    all_blocks: list[TextBlock | ToolUseBlock] = []
    tool_calls: list[ToolUseBlock] = []
    needs_follow_up = False
    stop_reason: str | None = None
    usage = Usage()
    async for item in aggregate_stream(events, tracer):
        if isinstance(item, StreamEvent):
            yield item                                    # ★ 流式透传(原累积到 yielded)
            if item.type == "message_delta":
                d = item.delta or {}
                if "stop_reason" in d:
                    stop_reason = d["stop_reason"]
                if item.message and "usage" in item.message:
                    usage = Usage(**item.message["usage"])
        else:  # block 级 AssistantMessage(内部累积, 不 yield)
            block = item.content[0]
            all_blocks.append(block)
            if isinstance(block, ToolUseBlock):
                if executor is not None:
                    executor.add_tool(block)
                tool_calls.append(block)
                needs_follow_up = True

    withheld = None
    if stop_reason == "max_tokens":
        withheld = "max_output_tokens"

    full = AssistantMessage(content=all_blocks, usage=usage, stop_reason=stop_reason)
    yield StreamOutcome(                                  # ★ 末尾 yield 元数据(替代 return)
        assistant_msgs=[full],
        tool_calls=tool_calls,
        needs_follow_up=needs_follow_up,
        stop_reason=stop_reason,
        withheld=withheld,
    )
```

`StreamOutcome` 删 `yielded` 字段:
```python
class StreamOutcome(BaseModel):
    """phase 之间传递的中间结果(流式版: 不再累积 yielded)。"""
    assistant_msgs: list[AssistantMessage]
    tool_calls: list[ToolUseBlock]
    needs_follow_up: bool
    stop_reason: str | None = None
    withheld: str | None = None
    # yielded 删除 —— 流式不累积, 整轮在 assistant_msgs + query_loop 显式 yield
```

> 删 `from pydantic import Field` 若仅 yielded 用到(grep 确认)。

- [ ] **Step 4: 改 orchestrator.py query_loop(async for 消费 + 显式整轮 yield,无 tombstone)**

query_loop 的 while 体改成 `async for` 消费 stream_turn。except 暂不加 tombstone(Task 3 加):

```python
async def query_loop(params: QueryParams, tracer: Tracer) -> AsyncIterator[Message | StreamEvent]:
    """内层 agentic loop。stream_turn 流式: async for 消费, 整轮显式 yield。"""
    state = State(messages=params.messages, turn_count=1)
    chain = build_recovery_chain()

    while True:
        tracer.emit(TraceEvent(kind=TraceKind.TURN_START, turn=state.turn_count))
        state = await maybe_compact(state, params, tracer)

        ctx = ToolContext(tracer=tracer, abort_signal=params.abort_signal, state=state)
        executor = make_executor(
            params.tool_execution_mode, params.tools, params.can_use_tool, tracer, ctx
        )
        try:
            outcome: StreamOutcome | None = None
            async for m in stream_turn(state, params, tracer, executor):
                if isinstance(m, StreamOutcome):
                    outcome = m                          # 元数据, 不向上 yield
                else:
                    yield m                              # ★ StreamEvent 实时透传下游
        except ProviderError as e:
            executor.discard()
            decision = await chain.handle_error(state, e, params, tracer)
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal):
                return
            if decision.next_state is None:
                return
            state = decision.next_state
            continue

        # stream_turn 成功 → 网络通, 清重试计数
        state.network_retry_count = 0
        yield outcome.assistant_msgs[0]                  # ★ 整轮透传(供 submit; 替代累积版 yielded 间接)

        if params.abort_signal.is_set():
            executor.discard()
            _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
            return

        # withheld 优先于 needs_follow_up
        if outcome.withheld:
            executor.discard()
            decision = await chain.handle(state, outcome, params, tracer)
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal):
                return
            if decision.next_state is None:
                return
            state = decision.next_state
            continue

        if outcome.needs_follow_up:
            tool_results = await executor.get_results()
            base = state.model_dump()
            base["messages"] = (
                state.messages + outcome.assistant_msgs
                + [UserMessage(content=cast(list[ContentBlock], tool_results))]
            )
            base["turn_count"] = state.turn_count + 1
            base["transition"] = Continue(reason=ContinueReason.NEXT_TURN)
            state = State(**base)
            if state.turn_count > params.max_turns:
                _emit_transition(tracer, Terminal(reason=TerminalReason.MAX_TURNS))
                return
            _emit_transition(tracer, state.transition)
            continue

        decision = await chain.handle(state, outcome, params, tracer)
        _emit_transition(tracer, decision.transition)
        if isinstance(decision.transition, Terminal):
            return
        if decision.next_state is None:
            return
        state = decision.next_state
```

> `StreamOutcome` import 进 orchestrator(`from .phases.stream_turn import stream_turn, StreamOutcome`)。`cast` 已 import。

- [ ] **Step 5: 改 test_orchestrator.py(query_loop 产出变 async for)**

现有 `test_query_loop_pure_text_completes` 等用 `[m async for m in query_loop(...)]` 收集——**仍可用**(query_loop 还是 async generator)。但产出从 `outcome.yielded`(StreamEvent + 整轮)变成"StreamEvent(中途)+ 整轮 AssistantMessage(显式 yield)"。

检查现有断言:`assts = [m for m in out if isinstance(m, AssistantMessage)]` 仍成立(整轮 yield)。StreamEvent 现在也在 out 里(原 yielded 也有 StreamEvent,所以一致)。**通常现有断言不需改**——但要跑确认。若某测试断言 `len(out)` 精确值,可能变(StreamEvent 数同,yielded 结构变),按实际调整。

Run: `pytest tests/test_orchestrator.py -v`。若有用例因 yield 结构变化失败,按失败信息调整断言(主要是"整轮从 yielded 来"→"显式 yield 来",断言 AssistantMessage 不变)。

- [ ] **Step 6: 运行确认通过**

Run: `pytest tests/test_stream_turn_executor.py tests/test_orchestrator.py -v && pytest -q`
Expected: stream_turn gen 测试通过 + orchestrator 现有用例适配通过 + 全量不破。

- [ ] **Step 7: commit**

```bash
git add core/loop/phases/stream_turn.py core/loop/orchestrator.py tests/test_stream_turn_executor.py tests/test_orchestrator.py
git commit -m "feat: stream_turn 改 async gen + 删 yielded + query_loop async for(核心流式)"
```

---

### Task 3: tombstone + abort-in-loop + turn_id(query_loop)

**Files:**
- Modify: `core/loop/orchestrator.py`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `Tombstone`(Task 1)、query_loop async for 骨架(Task 2)。
- Produces: query_loop 失败(except/abort)yield `Tombstone(turn_id)`;turn_id 递增。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_orchestrator.py`)

```python
from core.types import Tombstone
from core.loop.phases.stream_turn import StreamOutcome


class _ScriptedProvider:
    """按脚本依次返回事件 async-iterator 或抛 Exception。"""
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = 0
    def stream(self, **kwargs):
        i = self.calls; self.calls += 1
        item = self.scripts[i]
        if isinstance(item, Exception):
            raise item
        return item
    def count_tokens(self, messages): return 0


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


async def test_network_retry_yields_tombstone(monkeypatch):
    """失败重试: 第一轮抛异常 → yield Tombstone(turn_id=1) → 重试第二轮成功。"""
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", lambda s: None)
    provider = _ScriptedProvider([TransientProviderError("conn"), _text_events_async("ok")])
    spy = SpyTracer()
    out = [m async for m in query_loop(_params_with(provider), spy)]
    tombstones = [m for m in out if isinstance(m, Tombstone)]
    assert len(tombstones) == 1 and tombstones[0].turn_id == 1   # 失败轮 turn_id=1
    assts = [m for m in out if isinstance(m, AssistantMessage)]
    assert len(assts) == 1 and assts[0].content[0].text == "ok"  # 重试轮整轮


async def test_network_exhausted_yields_tombstone_then_return(monkeypatch):
    """重试耗尽 → yield Tombstone(最后一个失败轮) → return。"""
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", lambda s: None)
    provider = _ScriptedProvider([TransientProviderError("x")] * 4)
    spy = SpyTracer()
    out = [m async for m in query_loop(_params_with(provider), spy)]
    tombstones = [m for m in out if isinstance(m, Tombstone)]
    assert len(tombstones) >= 1
    # 无整轮(全失败)
    assert not any(isinstance(m, AssistantMessage) for m in out)


async def test_abort_yields_tombstone(monkeypatch):
    """abort_signal 在 async for 内 → yield Tombstone + USER_INTERRUPT。"""
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", lambda s: None)
    provider = _ScriptedProvider([_text_events_async("ok")])
    params = _params_with(provider)
    params.abort_signal.set()   # 预置 abort
    spy = SpyTracer()
    out = [m async for m in query_loop(params, spy)]
    # abort 在首个 StreamEvent 后触发 → tombstone
    assert any(isinstance(m, Tombstone) for m in out)
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions[-1].payload["reason"] == "user_interrupt"
```

> `_params_with` / `SpyTracer` / `TransientProviderError` import 同 test_orchestrator 现有(test_agent_loop 或 orchestrator 已有;若缺,从现有用例复制 helper)。`AssistantMessage` 已 import。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_orchestrator.py -k "tombstone or abort" -v`
Expected: FAIL — query_loop 当前 except 不 yield Tombstone;abort 检查在 async for 外(Task 2 版)。

- [ ] **Step 3: 改 query_loop(加 turn_id + tombstone + abort-in-loop)**

query_loop 完整体(在 Task 2 基础上加 turn_id + except yield Tombstone + async for 内 abort yield Tombstone):

```python
async def query_loop(params: QueryParams, tracer: Tracer) -> AsyncIterator[Message | StreamEvent | Tombstone]:
    """内层 agentic loop。stream_turn 流式 + tombstone 通知下游失败轮。"""
    state = State(messages=params.messages, turn_count=1)
    chain = build_recovery_chain()
    turn_id = 0

    while True:
        turn_id += 1                                      # ★ 每次 stream_turn(含重试)递增
        tracer.emit(TraceEvent(kind=TraceKind.TURN_START, turn=state.turn_count))
        state = await maybe_compact(state, params, tracer)

        ctx = ToolContext(tracer=tracer, abort_signal=params.abort_signal, state=state)
        executor = make_executor(
            params.tool_execution_mode, params.tools, params.can_use_tool, tracer, ctx
        )
        try:
            outcome: StreamOutcome | None = None
            async for m in stream_turn(state, params, tracer, executor):
                if isinstance(m, StreamOutcome):
                    outcome = m
                else:
                    yield m                               # StreamEvent 实时透传
                    if params.abort_signal.is_set():      # ★ abort in async for
                        executor.discard()
                        yield Tombstone(turn_id)
                        _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
                        return
        except ProviderError as e:
            executor.discard()
            decision = await chain.handle_error(state, e, params, tracer)
            yield Tombstone(turn_id)                      # ★ 通知下游本轮作废
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal):
                return
            if decision.next_state is None:
                return
            state = decision.next_state
            continue                                      # 重试 = turn_id+1 新轮

        state.network_retry_count = 0
        yield outcome.assistant_msgs[0]                   # 整轮透传(供 submit)

        if params.abort_signal.is_set():
            executor.discard()
            _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
            return

        if outcome.withheld:
            executor.discard()
            decision = await chain.handle(state, outcome, params, tracer)
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal):
                return
            if decision.next_state is None:
                return
            state = decision.next_state
            continue

        if outcome.needs_follow_up:
            tool_results = await executor.get_results()
            base = state.model_dump()
            base["messages"] = (
                state.messages + outcome.assistant_msgs
                + [UserMessage(content=cast(list[ContentBlock], tool_results))]
            )
            base["turn_count"] = state.turn_count + 1
            base["transition"] = Continue(reason=ContinueReason.NEXT_TURN)
            state = State(**base)
            if state.turn_count > params.max_turns:
                _emit_transition(tracer, Terminal(reason=TerminalReason.MAX_TURNS))
                return
            _emit_transition(tracer, state.transition)
            continue

        decision = await chain.handle(state, outcome, params, tracer)
        _emit_transition(tracer, decision.transition)
        if isinstance(decision.transition, Terminal):
            return
        if decision.next_state is None:
            return
        state = decision.next_state
```

> import `Tombstone`:`from ..types import ... Tombstone`。返回类型注解加 `Tombstone`。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_orchestrator.py -v && pytest -q`
Expected: tombstone 新测通过 + 现有用例不破(async for 结构不变,只加 tombstone/abort 路径)。

- [ ] **Step 5: commit**

```bash
git add core/loop/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: query_loop turn_id + tombstone(失败/abort 通知下游)"
```

---

### Task 4: submit 适配(Tombstone + StreamEvent 分支)

**Files:**
- Modify: `core/agent_loop.py`
- Test: `tests/test_agent_loop.py`

**Interfaces:**
- Consumes: query_loop 现在可能 yield `Tombstone` / `StreamEvent`(Task 2/3)。
- Produces: submit 对 `Tombstone`(不 append)、`StreamEvent`(本期 continue 留空)显式分支。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_agent_loop.py`)

```python
async def test_submit_handles_tombstone_and_stream_event(monkeypatch):
    """submit 对 Tombstone(不 append)和 StreamEvent(留空 continue)都有显式分支, 不崩。"""
    from core.agent_loop import AgentConfig, submit
    from core.types import Tombstone, StreamEvent

    # mock query_loop 产出: StreamEvent + Tombstone + AssistantMessage
    async def _fake_query_loop(params, tracer):
        yield StreamEvent(type="message_start")
        yield Tombstone(turn_id=1)              # 模拟第一轮失败
        yield _assistant_msg("ok")              # 模拟重试轮整轮
    monkeypatch.setattr("core.agent_loop.query_loop", _fake_query_loop)

    provider = _NoopProvider()
    config = AgentConfig(provider=provider, system="", model="m",
                         max_tokens=16, transcript_path=str(tmp_path / "t.jsonl"))
    results = [r async for r in submit("hi", config, _NoopTracer())]
    # submit 不因 Tombstone/StreamEvent 崩, 最终 success
    assert any(r.get("type") == "result" for r in results)
```

> `_assistant_msg(text)` / `_NoopProvider` / `_NoopTracer` / `tmp_path` 用 test_agent_loop 现有 helper(若缺,复制最小:AssistantMessage(content=[TextBlock(text=text)]);Provider 空 stub)。`tmp_path` 是 pytest fixture,测试函数签名加 `(monkeypatch, tmp_path)`。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_agent_loop.py::test_submit_handles_tombstone_and_stream_event -v`
Expected: FAIL — submit 当前对 Tombstone 无分支(isinstance 不匹配,落到末尾预算检查可能误处理);StreamEvent 是注释忽略(无显式 elif)。

- [ ] **Step 3: 改 submit(agent_loop.py 的 async for 体)**

`submit` 的 `async for msg in query_loop(...)` 体加 Tombstone + StreamEvent 分支:

```python
    async for msg in query_loop(params, tracer):
        if isinstance(msg, AssistantMessage):
            messages.append(msg)
            await record_transcript(messages, config.transcript_path)
            last_stop_reason = msg.stop_reason
            if msg.usage:
                total_in += msg.usage.input_tokens
                total_out += msg.usage.output_tokens
        elif isinstance(msg, UserMessage):
            messages.append(msg)
            await record_transcript(messages, config.transcript_path)
        elif isinstance(msg, Tombstone):
            # 本轮流式失败(没 yield 整轮), 不 append; 留位置供未来记日志/标记
            continue
        elif isinstance(msg, StreamEvent):
            # 流式 token 事件; 本期无 UI 暂不处理, 留位置供未来实时显示/hook
            continue

        if config.max_budget_usd is not None:
            if _rough_cost(total_in, total_out) >= config.max_budget_usd:
                yield {"type": "result", "subtype": "error_budget", "error": "budget exceeded"}
                return
```

> import:`from .types import (..., Tombstone)`(StreamEvent 已 import 或加)。注意把原 `# StreamEvent: 无 UI, 忽略` 注释删掉(改成显式 elif)。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_agent_loop.py -v && pytest -q`
Expected: 新测通过 + 全量不破。

- [ ] **Step 5: 全量回归 + pyright**

Run: `pytest -q && pyright`
Expected: 全部通过;pyright basic 无新错误。

- [ ] **Step 6: commit**

```bash
git add core/agent_loop.py tests/test_agent_loop.py
git commit -m "feat: submit 加 Tombstone(不 append) + StreamEvent(留空)分支"
```

---

## Self-Review 记录

**1. Spec 覆盖:**
- §3.1 stream_turn async gen → Task 2 Step 3。✓
- §3.2 删 StreamOutcome.yielded → Task 2 Step 3。✓
- §3.3 Tombstone(turn_id) → Task 1。✓
- §3.4 query_loop async for + turn_id + tombstone + abort-in-loop + 显式整轮 yield → Task 2(async for + 整轮 yield,无 tombstone)+ Task 3(tombstone + abort + turn_id)。✓
- §3.5 submit Tombstone + StreamEvent 分支 → Task 4。✓
- §4 数据流(六路径)→ Task 2/3 测试覆盖(正常/tool/withheld/重试/终止/abort)。✓
- §5 错误处理 → Task 3(except tombstone + abort tombstone)。✓
- §6 测试策略 → 各 task。✓
- §7 变更清单 5 文件 → Task 1-4。✓

**2. 类型一致性:**
- `Tombstone(turn_id: int)`:Task 1 定义,Task 3 query_loop yield,Task 4 submit isinstance。✓
- `stream_turn` async gen(yield StreamEvent + StreamOutcome):Task 2 定义,Task 3 query_loop async for 消费。✓
- `StreamOutcome` 无 yielded:Task 2 删,Task 2/3 query_loop 不引用。✓
- `outcome.assistant_msgs[0]` 整轮:Task 2 query_loop yield,Task 3 同。✓

**3. 关键风险点(implementer 注意):**
- **Task 2 是核心 + 跨文件**(stream_turn + query_loop 同改,否则中间破)。Step 3/4 必须**一起完成**再跑测试(Step 6),不能只改 stream_turn 就跑。
- **Task 2 删 yielded 后**,grep 确认无 `.yielded` 残留引用(test_stream_turn_executor / test_orchestrator 旧断言)。Step 1 已改 test_stream_turn_executor;test_orchestrator Step 5 按实际跑调整。
- **Task 3 query_loop 返回类型**加 `Tombstone`(`AsyncIterator[Message | StreamEvent | Tombstone]`),pyright 要过。
- **Task 4 submit** 把原"StreamEvent 注释忽略"改成显式 `elif`(语义同,但留扩展点);`Tombstone` 不 append(本轮失败没整轮)。
- **`_params_with` / `SpyTracer` / `TransientProviderError`** 在 test_orchestrator.py 若已存在(从 query_loop 健壮性 feature 留下),复用;若缺,从 spec §6 或现有用例复制 helper。
