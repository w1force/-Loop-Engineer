# stream_turn 流式改造设计 (async generator + tombstone)

> 日期: 2026-07-17 · 分支: feat/streaming-turn(从 feat/four-tool-lwt 拉)
> 动机: 累积模式 → 流式(实时 token + 删 yielded 冗余 + 整轮显式 yield),失败用 tombstone 通知下游。

## 1. 背景与动机

当前 `stream_turn` 是**累积模式**:普通 `async def`,跑完整轮,把 StreamEvent + 整轮 AssistantMessage 累积进 `StreamOutcome.yielded`(`stream_turn.py:182 yielded.append(full)`),一次性 `return`。`query_loop` 用 `await stream_turn(...)` + `for m in outcome.yielded: yield m` 透传给 submit。

三个问题:
1. **非流式**:整轮完成才 yield,外层(submit)无实时 token(低延迟显示场景受限)。
2. **整轮 yield 隐藏**:`query_loop` 没有显式 `yield assistant_message`,整轮藏在 `yielded` 透传里(`stream_turn.py:182`),不直观——曾导致"query_loop 和 submit 错配"的误判。
3. **冗余**:整轮同时存在 `StreamOutcome.assistant_msgs`(query_loop 内部分叉用)和 `yielded`(外层透传)两处。

## 2. 设计决策(brainstorming 已定)

- **`stream_turn` 改 async generator**:流式 `yield StreamEvent`(实时 token)+ 末尾 `yield StreamOutcome`(元数据,替代被禁的 `return value`)。
- **删 `StreamOutcome.yielded`**(流式不累积;整轮只在 `assistant_msgs`)。
- **state 轮结束才进**:stream_turn 成功(到 StreamOutcome)才把整轮加进 state.messages;失败本轮没进 state,**无需显式回退**,只需 `executor.discard`。state 语义同累积版。
- **`Tombstone(turn_id)`**:query_loop 失败时 yield,下游按 turn_id 作废本轮已收的 StreamEvent/AssistantMessage。下游判断重试/终止:收到 tombstone 后有新轮(turn_id+1)=重试,loop 结束=终止(tombstone 本身不带 will_retry)。
- **turn_id**:query_loop 局部 int 递增,每次 stream_turn(含重试)+1。**不用 `state.turn_count`**(recovery 不递增,失败轮与重试轮会撞 id)。
- **整轮显式 yield**:query_loop 成功时 `yield outcome.assistant_msgs[0]`(供 submit,替代累积版 yielded 间接透传)。
- **submit 加 `Tombstone` 分支**(本轮失败没整轮,不 append)。
- **abort 在 `async for` 内检查**(每条 StreamEvent 后)。
- **新分支 `feat/streaming-turn`**(从 feat/four-tool-lwt)。

## 3. 架构

### 3.1 `stream_turn` async generator(`core/loop/phases/stream_turn.py`)

```python
async def stream_turn(state, params, tracer, executor):   # 不再 -> StreamOutcome
    events = params.provider.stream(
        messages=state.messages, system=params.system, tools=params.tools,
        model=params.model,
        max_tokens=state.max_output_tokens_override or params.max_tokens,
        abort_signal=params.abort_signal, tracer=tracer,
    )
    all_blocks = []; tool_calls = []; needs_follow_up = False
    stop_reason = None; usage = Usage()
    async for item in aggregate_stream(events, tracer):
        if isinstance(item, StreamEvent):
            yield item                                    # ★ 流式透传(原累积到 yielded)
            if item.type == "message_delta":
                d = item.delta or {}
                if "stop_reason" in d: stop_reason = d["stop_reason"]
                if item.message and "usage" in item.message:
                    usage = Usage(**item.message["usage"])
        else:  # block 级 AssistantMessage(内部累积, 不 yield —— 同原逻辑)
            block = item.content[0]; all_blocks.append(block)
            if isinstance(block, ToolUseBlock):
                if executor is not None: executor.add_tool(block)
                tool_calls.append(block); needs_follow_up = True
    withheld = "max_output_tokens" if stop_reason == "max_tokens" else None
    full = AssistantMessage(content=all_blocks, usage=usage, stop_reason=stop_reason)
    yield StreamOutcome(                                  # ★ 末尾 yield 元数据(替代 return)
        assistant_msgs=[full], tool_calls=tool_calls,
        needs_follow_up=needs_follow_up, stop_reason=stop_reason, withheld=withheld,
    )
```

### 3.2 `StreamOutcome` 删 `yielded`

```python
class StreamOutcome(BaseModel):
    assistant_msgs: list[AssistantMessage]
    tool_calls: list[ToolUseBlock]
    needs_follow_up: bool
    stop_reason: str | None = None
    withheld: str | None = None
    # yielded 删除 —— 流式不累积; 整轮在 assistant_msgs, query_loop 显式 yield
```

### 3.3 `Tombstone`(`core/types.py`)

```python
from dataclasses import dataclass

@dataclass
class Tombstone:
    """通知下游: turn_id 这一轮的流式 yield 作废(失败, 将重试或终止)。
    下游收到后丢弃该 turn_id 已收的 StreamEvent/AssistantMessage。
    重试/终止判断: 收到 tombstone 后有新轮(turn_id+1)=重试, loop 结束=终止。"""
    turn_id: int
```

> 放 `core/types.py`(与 StreamEvent/Message 同层,下游 submit 要 isinstance 判断)。

### 3.4 `query_loop` 改 `async for` + turn_id + tombstone + abort(`core/loop/orchestrator.py`)

```python
async def query_loop(params, tracer):
    state = State(messages=params.messages, turn_count=1)
    chain = build_recovery_chain()
    turn_id = 0
    while True:
        turn_id += 1
        tracer.emit(TraceEvent(kind=TraceKind.TURN_START, turn=state.turn_count))
        state = await maybe_compact(state, params, tracer)
        ctx = ToolContext(tracer=tracer, abort_signal=params.abort_signal, state=state)
        executor = make_executor(params.tool_execution_mode, params.tools,
                                  params.can_use_tool, tracer, ctx)
        try:
            outcome: StreamOutcome | None = None
            async for m in stream_turn(state, params, tracer, executor):
                if isinstance(m, StreamOutcome):
                    outcome = m                          # 元数据, 不向上 yield
                else:
                    yield m                              # ★ StreamEvent 实时透传
                    if params.abort_signal.is_set():     # ★ abort in async for
                        executor.discard()
                        yield Tombstone(turn_id)
                        _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
                        return
        except ProviderError as e:
            executor.discard()
            decision = await chain.handle_error(state, e, params, tracer)
            yield Tombstone(turn_id)                     # ★ 通知下游本轮作废
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal): return
            if decision.next_state is None: return
            state = decision.next_state
            continue                                     # 重试 = turn_id+1 新轮

        # stream_turn 成功 → 网络通, 清重试计数
        state.network_retry_count = 0
        yield outcome.assistant_msgs[0]                  # ★ 整轮透传(供 submit; 替代累积版 yielded 间接)

        if params.abort_signal.is_set():
            executor.discard()
            _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT)); return

        # withheld 优先于 needs_follow_up
        if outcome.withheld:
            executor.discard()
            decision = await chain.handle(state, outcome, params, tracer)
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal): return
            if decision.next_state is None: return
            state = decision.next_state; continue

        if outcome.needs_follow_up:
            tool_results = await executor.get_results()
            ...回灌 + NEXT_TURN + max_turns 检查...(同累积版逻辑)
            continue

        decision = await chain.handle(state, outcome, params, tracer)
        ...(同累积版完成路径)
```

### 3.5 submit 适配(`core/agent_loop.py`)

```python
async for msg in query_loop(params, tracer):
    if isinstance(msg, AssistantMessage): ...            # 不变(append + transcript + usage)
    elif isinstance(msg, UserMessage): ...               # 不变
    elif isinstance(msg, Tombstone):                     # ★ 新增
        # 本轮失败(没 yield 整轮), 不 append; 可记日志/标记
        continue
    elif isinstance(msg, StreamEvent):                   # ★ 新增(本期留空, 留扩展点)
        # 流式 token 事件; 本期无 UI 暂不处理, 留位置供未来实时显示/hook
        continue
```

## 4. 数据流

| 路径 | 流程 |
|------|------|
| 正常完成(无 tool) | stream_turn yield StreamEvent→query_loop 透传(submit 忽略)→末尾 StreamOutcome→query_loop `yield outcome.assistant_msgs[0]`(整轮)→submit append+transcript→chain.handle→COMPLETED |
| 有 tool(needs_follow_up) | 同上到整轮 yield→query_loop 回灌 tool_results+continue(下一轮) |
| withheld(max_tokens) | stream_turn yield StreamEvent+StreamOutcome→query_loop `executor.discard`+chain.handle(MaxOutputTokensRule 升档/续写)→continue |
| 网络失败重试 | stream_turn 中途抛 ProviderError→except:`discard`+`handle_error`+`yield Tombstone(turn_id)`→重试(turn_id+1)新轮 |
| 失败终止 | handle_error 返 Terminal→`yield Tombstone`→return |
| abort | `async for` 内 abort_signal→`discard`+`yield Tombstone`+USER_INTERRUPT+return |

## 5. 错误处理

- **ProviderError**(网络/截断/模型):except → `executor.discard` → `chain.handle_error` → `yield Tombstone(turn_id)` → 据 decision 重试(`continue`)/终止(`return`)。
- **abort**(`async for` 内每条后检查):`discard` + `yield Tombstone` + `USER_INTERRUPT` + `return`。
- **半截流外泄**:失败时已 yield 的 StreamEvent 已在下游,`Tombstone` 通知下游按 turn_id 丢弃。state 不进本轮(无需回退)。

## 6. 测试策略

- **stream_turn gen**(`test_stream_turn_executor.py`):stream_turn 从"返回 StreamOutcome"改成"async gen";断言"中途 yield StreamEvent"+"末尾 yield StreamOutcome"。删 `yielded` 相关断言。
- **query_loop async for**(`test_orchestrator.py`):query_loop 消费 async gen;现有用例(纯文本完成/tool_use)适配。
- **tombstone 新测**:网络重试 → yield tombstone + 重试新轮(turn_id 递增);终止 → yield tombstone + return;abort → tombstone + USER_INTERRUPT。
- **整轮 yield**:query_loop 成功 yield `outcome.assistant_msgs[0]`(submit 收整轮)。
- **submit tombstone**:submit 收 tombstone 不 append(本轮没整轮);收整轮正常 append。

## 7. 变更清单

| 文件 | 改动 |
|------|------|
| `core/types.py` | 加 `Tombstone(turn_id)` dataclass |
| `core/loop/phases/stream_turn.py` | `stream_turn` 改 async gen(末尾 yield StreamOutcome)+ 删 `StreamOutcome.yielded` |
| `core/loop/orchestrator.py` | `query_loop` 改 `async for` + turn_id + tombstone + abort-in-loop + 显式 `yield outcome.assistant_msgs[0]` |
| `core/agent_loop.py` | submit 加 `Tombstone` 分支 + 显式 `StreamEvent` 分支(本期留空, 留扩展点) |
| 测试 | `test_stream_turn_executor.py` / `test_orchestrator.py` 适配 + tombstone 新测 |

## 8. 权衡

**收益**:
- 流式:外层实时收 token(低延迟显示场景)。
- 删 `yielded` 冗余(整轮只在 `assistant_msgs`)。
- 整轮显式 `yield`(不再藏 `yielded`,消除"错配"误判)。
- tombstone 让下游精确处置失败轮(按 turn_id)。

**代价**:
- **原子性丢失**:失败时半截 StreamEvent 已外泄(累积版失败不 yield 任何)。tombstone 通知下游丢弃,但下游要实现处置(submit 当前简单忽略/记录;未来 UI 要按 turn_id 清屏)。
- **abort 非即时**:`async for` 内每条后检查,provider 阻塞等 token 时 abort 要等下个 m。彻底即时取消要 provider 层接 abort_signal(未来,本次不做)。
- **async gen 不能 return value**:stream_turn 用 yield StreamOutcome 传元数据(已在设计内,非新增问题)。

**适用判断**:消费者能容忍半截流 + 按 tombstone 处置(实时显示场景)。本项目 submit 已适配 tombstone + 整轮仍到(成功),所以可行。若未来有"对中间态严格敏感"的消费者,需在它那一层缓冲+只在整轮/tombstone 后提交。
