# query_loop 健壮性加固 设计

> 日期: 2026-07-16 · 分支: dev/lwt
> 配套: [`notes/python-recovery-patterns.md`](../../../ts/Claude-Code/notes/python-recovery-patterns.md)(静默升档 + 孤儿 tool_use 补全)

## 1. 背景与问题

当前 `query_loop` 对故障几乎没有容错。诊断出 7 个缺陷:

| # | 缺陷 | 后果 |
|---|------|------|
| 1 | 无顶层异常捕获 | 任何 provider 异常直接冒泡崩溃整个 loop |
| 2 | `withheld` 未实现(stream_turn 恒 None) | max_output_tokens 截断被当作 COMPLETED 静默返回;prompt_too_long 崩溃 |
| 3 | 无重试/退避 | 一次网络抖动 = 一次崩溃 |
| 4 | 异常路径泄漏工具 task | `discard()` 只在 abort 分支,异常出口不清理,孤儿 task 后台继续跑 |
| 5 | abort 流式中断无效 | `anthropic.py:75` 收了 abort_signal 却没用,流式中无法中断 |
| 6 | tool_result 匹配靠"过滤 None" | `base.py:93` 静默丢 result,隐患 |
| 7 | tool_use input JSON 截断 | `stream_turn.py:90` `json.loads` 未保护,max_tokens 切中途会崩 |

## 2. 设计目标与核心原则

**原则:状态由 State 统一管理,异常经责任链对 State 做变换,确保 while 循环下次能正确重试/终止。**

推论:
- **异常不抛出 loop**——业务异常在 `query_loop` 的 while 内被 catch,翻译成 `Decision(transition, next_state)`,与正常流转走同一套 State 重建模型。
- **恢复逻辑收口到责任链**——不在各退出路径散落补丁。正常完成 + 异常恢复都由 chain 产出 `Decision`。
- **配对完整性是 executor 的内在不变量**(预占位设计),而非事后补全。
- **分类知识封在 adapter**——"什么是可重试"的 HTTP 语义由 provider adapter 判断,query_loop 只分发。

**编程 bug(`KeyError` 等)不被吞**,照常冒泡——只接业务异常。

## 3. 核心架构:异常体系 + 分类器 + 责任链扩展

### 3.1 provider 自定义异常体系(新文件 `core/provider_errors.py`)

把"transient / prompt_too_long / fatal"的 HTTP 语义判断**就近放 adapter**,query_loop 只按异常类型分发。

```python
# core/provider_errors.py
class ProviderError(Exception):
    """provider 层业务异常基类。query_loop 的 except ProviderError 接它。"""
    def __init__(self, message: str = "", *, status: int | None = None, body: bytes | None = None):
        super().__init__(message)
        self.status = status
        self.body = body

class TransientProviderError(ProviderError):
    """网络瞬时错误 / 5xx / 429 / overload —— 可重试。"""

class PromptTooLongError(ProviderError):
    """HTTP 400 且 body 指示 prompt 过长。"""

class FatalProviderError(ProviderError):
    """其余 4xx / 流 error 事件 / 未知 —— 不可重试。"""

class UserInterruptError(Exception):
    """用户主动中断。本次定义但不触发;未来 stream 循环接入 abort_signal 时 raise(§7 扩展点)。"""
```

规则链用 `isinstance` 分发,不需要 `kind` 字段。

### 3.2 anthropic adapter 改造(`core/providers/anthropic.py`)

各 raise 点改为抛自定义异常;新增对 `httpx.TransportError` 的捕获(连接失败/超时/流中途断):

```python
from ..provider_errors import TransientProviderError, PromptTooLongError, FatalProviderError
import httpx

async def stream(self, *, messages, system, tools, model, max_tokens, abort_signal, tracer, **opts):
    tracer.emit(TraceEvent(kind=TraceKind.PROVIDER_REQUEST, payload={...}))
    req_body = {...}
    try:
        async with self.http.stream("POST", "/v1/messages", json=req_body) as r:
            if r.status_code != 200:
                body = await r.aread()
                tracer.emit(TraceEvent(kind=TraceKind.PROVIDER_ERROR,
                                       payload={"status": r.status_code, "body": body[:200]}))
                raise self._classify_status_error(r.status_code, body)
            async for data in parse_sse(r):
                ...
                evt = json.loads(data); t = evt.get("type")
                if t == "ping": continue
                if t == "error":
                    tracer.emit(TraceEvent(kind=TraceKind.PROVIDER_ERROR, payload=evt))
                    raise self._classify_stream_error(evt)
                if t not in _CONTENT_EVENT_TYPES: continue
                yield self._to_stream_event(evt)
    except httpx.TransportError as e:  # ConnectError/ReadTimeout/NetworkError/RemoteProtocolError...
        tracer.emit(TraceEvent(kind=TraceKind.PROVIDER_ERROR, payload={"transport": type(e).__name__}))
        raise TransientProviderError(f"transport error: {e}") from e

@staticmethod
def _classify_status_error(status: int, body: bytes) -> ProviderError:
    text = body.decode("utf-8", errors="replace").lower()
    if status == 429 or status >= 500:
        return TransientProviderError(f"HTTP {status}", status=status, body=body)
    if status == 400 and "prompt is too long" in text:
        return PromptTooLongError("prompt is too long", status=status, body=body)
    return FatalProviderError(f"HTTP {status}", status=status, body=body)

@staticmethod
def _classify_stream_error(evt: dict) -> ProviderError:
    # overloaded_error 视为瞬时(服务端过载,可重试);其余 fatal
    err = evt.get("error") or {}
    if err.get("type") == "overloaded_error":
        return TransientProviderError(f"stream overloaded: {err}")
    return FatalProviderError(f"stream error: {err}")
```

`httpx.HTTPStatusError` 不再使用(adapter 内部已分类抛出)。`abort_signal` 参数保留(§7 扩展点),本次函数体仍不检查它。

### 3.3 query_loop 主干(`core/loop/orchestrator.py`)

while 内 try/except,except 把异常喂给**同一个**责任链的错误入口。executor 由 query_loop 持有,异常不丢,补孤儿/占位直接基于它。

```python
async def query_loop(params, tracer):
    state = State(messages=params.messages, turn_count=1)
    chain = build_recovery_chain()
    while True:
        tracer.emit(TraceEvent(kind=TraceKind.TURN_START, turn=state.turn_count))
        state = await maybe_compact(state, params, tracer)
        ctx = ToolContext(tracer=tracer, abort_signal=params.abort_signal, state=state)
        executor = make_executor(params.tool_execution_mode, params.tools,
                                  params.can_use_tool, tracer, ctx)
        try:
            outcome = await stream_turn(state, params, tracer, executor)
        except ProviderError as e:
            executor.discard()                                  # 先清在途,防泄漏(缺陷 4)
            decision = await chain.handle_error(state, e, params, tracer)
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal):
                return
            state = decision.next_state
            continue
        # —— stream_turn 成功:网络通,清重试计数 ——
        state.network_retry_count = 0
        for m in outcome.yielded:
            yield m
        if params.abort_signal.is_set():
            executor.discard()
            _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
            return
        # —— withheld 优先于 needs_follow_up(§5.3)——
        if outcome.withheld:
            decision = await chain.handle(state, outcome, params, tracer)
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal):
                return
            if decision.next_state is None:
                return
            state = decision.next_state
            continue
        # —— 正常工具回灌 ——
        if outcome.needs_follow_up:
            tool_results = await executor.get_results()
            base = state.model_dump()
            base["messages"] = (state.messages + outcome.assistant_msgs
                                + [UserMessage(content=cast(list[ContentBlock], tool_results))])
            base["turn_count"] = state.turn_count + 1
            base["transition"] = Continue(reason=ContinueReason.NEXT_TURN)
            state = State(**base)
            if state.turn_count > params.max_turns:
                _emit_transition(tracer, Terminal(reason=TerminalReason.MAX_TURNS))
                return
            _emit_transition(tracer, state.transition)
            continue
        # —— 正常完成 ——
        decision = await chain.handle(state, outcome, params, tracer)
        _emit_transition(tracer, decision.transition)
        if isinstance(decision.transition, Terminal):
            return
        if decision.next_state is None:
            return
        state = decision.next_state
```

### 3.4 责任链扩展(`core/loop/recovery/base.py`)

`RecoveryChain` 加 `error_rules` + `handle_error`。`handle` / `handle_error` / 所有 `rule.apply` 改 **async**(网络重试要 `await asyncio.sleep`,压缩等恢复也可能 async)。

```python
class Decision(BaseModel):
    transition: Continue | Terminal
    next_state: State | None = None

class TransitionRule(Protocol):
    name: str
    def match(self, state, outcome) -> bool: ...
    async def apply(self, state, outcome, params, tracer) -> Decision: ...

class ErrorRule(Protocol):
    name: str
    def match(self, state, err: ProviderError) -> bool: ...
    async def apply(self, state, err, params, tracer) -> Decision: ...

class RecoveryChain:
    def __init__(self, rules: list, error_rules: list):
        self.rules = rules            # 正常链(基于 outcome.withheld)
        self.error_rules = error_rules  # 错误链(基于 ProviderError)

    async def handle(self, state, outcome, params, tracer) -> Decision:
        for rule in self.rules:
            if rule.match(state, outcome):
                tracer.emit(TraceEvent(kind=TraceKind.RECOVERY_ATTEMPT,
                                       payload={"rule": rule.name, "withheld": outcome.withheld}))
                return await rule.apply(state, outcome, params, tracer)
        return Decision(transition=Terminal(reason=TerminalReason.COMPLETED))

    async def handle_error(self, state, err, params, tracer) -> Decision:
        for rule in self.error_rules:
            if rule.match(state, err):
                tracer.emit(TraceEvent(kind=TraceKind.RECOVERY_ATTEMPT,
                                       payload={"rule": rule.name, "error": type(err).__name__}))
                return await rule.apply(state, err, params, tracer)
        return Decision(transition=Terminal(reason=TerminalReason.MODEL_ERROR, error=str(err)))
```

### 3.5 State / 枚举字段(`core/types.py`)

```python
class ContinueReason(str, Enum):
    NEXT_TURN = "next_turn"
    NETWORK_RETRY = "network_retry"                       # 新增
    MAX_OUTPUT_TOKENS_ESCALATE = "max_output_tokens_escalate"
    MAX_OUTPUT_TOKENS_RECOVERY = "max_output_tokens_recovery"
    REACTIVE_COMPACT_RETRY = "reactive_compact_retry"

class TerminalReason(str, Enum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    USER_INTERRUPT = "user_interrupt"                     # 重命名自 ABORTED(§7)
    MODEL_ERROR = "model_error"
    PROMPT_TOO_LONG = "prompt_too_long"
    BUDGET_EXCEEDED = "budget_exceeded"                   # 原有,保留不动

class State(BaseModel):
    messages: list[Message]
    turn_count: int = 1
    max_output_tokens_recovery_count: int = 0
    max_output_tokens_override: int | None = None
    has_attempted_autocompact: bool = False
    network_retry_count: int = 0                          # 新增
    transition: Continue | Terminal | None = None

ESCALATED_MAX_TOKENS = 64_000                             # 改:占位 32000 → 64000
MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3                      # 不变
```

> `TerminalReason.ABORTED` 重命名为 `USER_INTERRUPT`。所有引用(orchestrator.py)同步改。

## 4. 占位设计:配对完整性作为 executor 不变量

### 4.1 核心思想

`TrackedTool` 创建即带一个 `is_error=True` 占位 result;执行成功才回填真实值;cancel/未执行保持占位。配对完整性从"事后补全"变成 executor 的内在不变量——**消除通用的扫历史补孤儿函数**(缺陷 6)。

### 4.2 TrackedTool + 占位工厂(`core/tool_executor/base.py`)

```python
_PLACEHOLDER_REASON = "tool execution interrupted"

def _placeholder(block: ToolUseBlock, reason: str = _PLACEHOLDER_REASON) -> ToolResultBlock:
    """造 is_error 占位 result(执行前预设 / cancel / 续写未执行 都用它)。"""
    return ToolResultBlock(tool_use_id=block.id, content=reason, is_error=True)

@dataclass
class TrackedTool:
    block: ToolUseBlock
    status: Literal["queued", "executing", "completed", "cancelled"] = "queued"
    result: ToolResultBlock       # 不再 | None: 创建即占位
    task: asyncio.Task | None = None
```

`add_tool` 创建 tracked 时即赋占位;`_execute_single` 的 finally 成功/失败覆盖占位,CancelledError 分支不覆盖(保持占位):

```python
def add_tool(self, block: ToolUseBlock) -> None:
    if self._discarded:
        return
    tracked = TrackedTool(block=block, result=_placeholder(block))   # ★ 预占位
    self._tracked.append(tracked)
    if block.name not in self._tools:
        tracked.result = ToolResultBlock(
            tool_use_id=block.id, content=f"未知工具: {block.name}", is_error=True)
        tracked.status = "completed"
        return
    self._on_add(tracked)

async def get_results(self) -> list[ToolResultBlock]:
    await self._run_all()
    return [t.result for t in self._tracked]   # ★ 不再过滤 None(全是占位或真实)
```

`_execute_single` 内部逻辑不变(成功/失败赋 result,CancelledError 路径 result 变量保持 None → finally 的 `if result is not None` 跳过 → 占位保留)。占位 result 在三种情况出现,只是 `content` 文本不同:

| 情况 | content | is_error |
|------|---------|----------|
| 占位(未执行/续写) | "tool execution interrupted" | true |
| 执行异常 | "工具执行错误: ..." | true |
| 执行成功 | 工具返回值 | false |

### 4.3 is_error tool_result 在请求 body 中的形态

`ToolResultBlock.model_dump()` 后,在 user message 的 content 数组里:

```json
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "toolu_01abc",
      "content": "tool execution interrupted",
      "is_error": true
    }
  ]
}
```

`to_anthropic` 原样透传。`is_error: true` 时模型把 content 当工具失败信息,下一轮重新决策。

## 5. withheld 路径:max_output_tokens 恢复

### 5.1 stream_turn withheld 检测(`core/loop/phases/stream_turn.py`)

循环结束后(stop_reason 此时已知——它来自 `message_delta`,在 `content_block_stop` 之后到达,时序正确):

```python
withheld = None
if stop_reason == "max_tokens":
    withheld = "max_output_tokens"
return StreamOutcome(
    assistant_msgs=[full], tool_calls=tool_calls,
    needs_follow_up=needs_follow_up, stop_reason=stop_reason,
    withheld=withheld, yielded=yielded,
)
```

`StreamOutcome.withheld` 类型从 `None | "prompt_too_long" | "max_output_tokens"` 收窄为 `None | "max_output_tokens"`(prompt_too_long 走异常路径,不产生 withheld)。

### 5.2 JSON 截断容错(`aggregate_stream`)

`stream_turn.py:90` 的 `json.loads(input_buf)` 在 max_tokens 切断 tool_use input 中途时会崩(缺陷 7)。改为 try/except——截断时**丢弃该残缺 block**(不 yield,不喂 executor),由 stop_reason withhold 统一兜底:

```python
elif evt.type == "content_block_stop":
    idx = evt.index
    if idx is None: continue
    b = blocks[idx]
    if b.get("type") == "tool_use":
        try:
            b["input"] = json.loads(b.pop("input_buf", "") or "{}")
        except json.JSONDecodeError:
            continue   # 残缺(max_tokens 截断):丢弃,由 withhold 兜底
    yield AssistantMessage(content=[_to_block(b)])
```

权威信号是 `stop_reason == "max_tokens"`(message_delta 必到,不依赖 content_block_stop 是否优雅结束);json.loads 容错只是附加防御层。两条路都收敛到 `withheld="max_output_tokens"`。

### 5.3 流程调整:withheld 优先于 needs_follow_up

当前 `if outcome.needs_follow_up` 在前,max_tokens + 有 tool_use 时会走回灌执行(可能残缺的)工具,withheld 被忽略。**必须**调整为先判 withheld(见 §3.3 主干):`if outcome.withheld` → chain 恢复 → `elif needs_follow_up` → 回灌 → `else` → 正常完成。

### 5.4 MaxOutputTokensRule 两档(`core/loop/recovery/rules.py`,激活现有空转规则)

State 字段 `max_output_tokens_override` / `max_output_tokens_recovery_count` 已存在,`stream_turn.py:136` 已 `max_tokens = override or params.max_tokens`——升档重试天然接通。

```python
_META_RESUME = ("Output token limit hit. Resume directly — no apology, no recap. "
                "Pick up mid-thought. Break remaining work into smaller pieces.")

class MaxOutputTokensRule:
    name = "max_output_tokens"
    def match(self, state, outcome) -> bool:
        return outcome.withheld == "max_output_tokens"

    async def apply(self, state, outcome, params, tracer) -> Decision:
        # 第一档:静默升档(每会话一次)—— 本轮残缺 assistant 丢弃,只改 max_tokens 重发
        if state.max_output_tokens_override is None:
            return Decision(
                transition=Continue(reason=ContinueReason.MAX_OUTPUT_TOKENS_ESCALATE),
                next_state=state.model_copy(update={"max_output_tokens_override": ESCALATED_MAX_TOKENS}),
            )
        # 第二档:注入续写消息(≤3)—— 本轮完成块 + 占位 result + meta 进历史, withhold 不执行工具
        if state.max_output_tokens_recovery_count < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
            turn_assistant = outcome.assistant_msgs[0]
            new_msgs: list[Message] = [turn_assistant]
            if outcome.tool_calls:   # 本轮完成的 tool_use 一律占位 is_error(不执行)
                placeholders = [_placeholder(tc) for tc in outcome.tool_calls]
                new_msgs.append(UserMessage(content=placeholders))
            new_msgs.append(UserMessage(content=_META_RESUME))
            return Decision(
                transition=Continue(reason=ContinueReason.MAX_OUTPUT_TOKENS_RECOVERY),
                next_state=state.model_copy(update={
                    "messages": state.messages + new_msgs,
                    "max_output_tokens_recovery_count": state.max_output_tokens_recovery_count + 1,
                }),
            )
        # 耗尽
        return Decision(transition=Terminal(reason=TerminalReason.MODEL_ERROR,
                                            error="max_output_tokens recovery exhausted"))
```

关键点:
- `max_output_tokens_override is None` 用"是否已升档"做幂等(每会话升档一次,notes 原则)。
- 升档 = 本轮丢弃(state.messages 不变,只设 override);续写 = 本轮完成块 + 占位 + meta 进历史。
- 续写时 `outcome.tool_calls` 的 tool_use 一律占位 is_error(基于 §4 占位工厂),不调 `get_results`——max_tokens 是异常态,不执行可能不完整的工具,让模型看到"工具没成功、重新决定"。

## 6. 错误恢复规则(`core/loop/recovery/rules.py`)

错误链 `[NetworkRetryRule, PromptTooLongErrorRule, ModelErrorRule]`。

### 6.1 NetworkRetryRule(transient → 指数退避重试)

```python
NETWORK_RETRY_LIMIT = 3
NETWORK_BACKOFF_BASE = 1.0   # 秒

def _jitter() -> float:
    return random.uniform(0, 0.5)

class NetworkRetryRule:
    name = "network_retry"
    def match(self, state, err) -> bool:
        return isinstance(err, TransientProviderError)

    async def apply(self, state, err, params, tracer) -> Decision:
        if state.network_retry_count < NETWORK_RETRY_LIMIT:
            backoff = NETWORK_BACKOFF_BASE * (2 ** state.network_retry_count) + _jitter()
            await asyncio.sleep(backoff)    # ~1s/2s/4s
            return Decision(
                transition=Continue(reason=ContinueReason.NETWORK_RETRY),
                next_state=state.model_copy(update={
                    "network_retry_count": state.network_retry_count + 1}),
            )
        # 超限:Terminal(本轮没进历史,executor 已 discard,无需配对)
        return Decision(transition=Terminal(reason=TerminalReason.MODEL_ERROR,
                                            error=f"network retry exhausted: {err}"))
```

state 不变(本轮 stream_turn 抛异常,assistant 没进 state.messages),只 +1 计数 + 退避,continue → 下轮用原 state 重新 stream_turn。stream_turn 成功后 §3.3 清零计数。

### 6.2 PromptTooLongErrorRule(本次 Terminal,压缩留 Phase5)

```python
class PromptTooLongErrorRule:
    name = "prompt_too_long"
    def match(self, state, err) -> bool:
        return isinstance(err, PromptTooLongError)
    async def apply(self, state, err, params, tracer) -> Decision:
        # 本次无压缩能力(maybe_compact 是桩):诚实终止。Phase5 实现压缩后升级为
        # Continue(REACTIVE_COMPACT_RETRY) + 压缩一轮。
        return Decision(transition=Terminal(reason=TerminalReason.PROMPT_TOO_LONG, error=str(err)))
```

### 6.3 ModelErrorRule(fatal → Terminal)

```python
class ModelErrorRule:
    name = "model_error"
    def match(self, state, err) -> bool:
        return isinstance(err, FatalProviderError)
    async def apply(self, state, err, params, tracer) -> Decision:
        return Decision(transition=Terminal(reason=TerminalReason.MODEL_ERROR, error=str(err)))
```

`handle_error` 兜底也返回 `Terminal(MODEL_ERROR)`。

### 6.4 build_recovery_chain

```python
def build_recovery_chain() -> RecoveryChain:
    return RecoveryChain(
        rules=[MaxOutputTokensRule(), CompletedRule()],                    # 正常链
        error_rules=[NetworkRetryRule(), PromptTooLongErrorRule(), ModelErrorRule()],  # 错误链
    )
```

> 原正常链里的 `PromptTooLongRule` / `MaxOutputTokensRule`(基于 withheld 恒 False 的空转版)删除/替换:max_output_tokens 走激活版(§5.4),prompt_too_long 移到错误链。

## 7. UserInterrupt(本次留扩展,不实现具体处理)

- `TerminalReason.ABORTED` → `USER_INTERRUPT`(语义"用户主动中断",比 abort 清晰)。
- 现有 stream_turn 后的 `abort_signal` 基础检查保留(orchestrator.py,Terminal 用 USER_INTERRUPT)。
- **本次不实现**流式中断(`anthropic.py` stream 循环接入 abort_signal)。
- **留扩展空间**:`UserInterruptError` 已定义(§3.1,本次不触发)。未来在 stream 循环检查 `abort_signal.is_set()` → `raise UserInterruptError()`,query_loop 加 `except UserInterruptError: discard + Terminal(USER_INTERRUPT)` 即可,接口已就位。

## 8. 数据流(各路径 State 变换)

| 路径 | 触发 | State 变换 | 终止? |
|------|------|-----------|-------|
| 正常工具回灌 | needs_follow_up, 无 withhold | messages += [assistant, user(tool_results)], turn+1 | 否(continue) |
| 正常完成 | 无 tool_use, 无 withhold | CompletedRule → Terminal(COMPLETED) | 是 |
| 网络重试 | TransientProviderError, count<3 | network_retry_count+1, 退避, state 其余不变 | 否(continue) |
| 网络耗尽 | Transient, count≥3 | Terminal(MODEL_ERROR) | 是 |
| max_tokens 升档 | withheld, override is None | override=64000, 其余不变 | 否(continue) |
| max_tokens 续写 | withheld, count<3 | messages += [assistant, user(占位), meta], count+1 | 否(continue) |
| max_tokens 耗尽 | withheld, count≥3 | Terminal(MODEL_ERROR) | 是 |
| prompt_too_long | PromptTooLongError | Terminal(PROMPT_TOO_LONG) | 是 |
| model_error | FatalProviderError | Terminal(MODEL_ERROR) | 是 |
| 用户中断 | abort_signal(stream_turn 后) | Terminal(USER_INTERRUPT) | 是 |

> 所有 Terminal 路径本轮 assistant 未进 state.messages(stream_turn 抛异常或在回灌前),无需配对补全——占位设计的 executor 已 discard,无悬空 tool_use 入历史。

## 9. 错误处理矩阵(异常 → 规则 → 动作)

```
stream_turn 抛 ProviderError
  ├─ TransientProviderError → NetworkRetryRule → 退避重试(count<3) / Terminal MODEL_ERROR
  ├─ PromptTooLongError     → PromptTooLongErrorRule → Terminal PROMPT_TOO_LONG
  └─ FatalProviderError     → ModelErrorRule → Terminal MODEL_ERROR
stream_turn 成功
  ├─ withheld=max_output_tokens → MaxOutputTokensRule → 升档 / 续写 / Terminal MODEL_ERROR
  ├─ needs_follow_up            → 回灌 tool_results → continue
  └─ (无)                       → CompletedRule → Terminal COMPLETED
```

## 10. 测试策略(TDD)

每条规则/分类器/占位/withheld 检测独立可测。mock provider 抛各异常,mock SSE 驱动 max_tokens 截断。

- **分类器**(`anthropic._classify_status_error` / `_classify_stream_error`):429→Transient、500→Transient、400+prompt long→PromptTooLong、400 其他→Fatal、overloaded_error→Transient、其余 stream error→Fatal。
- **NetworkRetryRule**:count<3 返回 Continue+计数+1、退避时序(用 fake sleep 断言 sleep 时长 = base·2^n + jitter 范围)、count≥3 返回 Terminal。stream_turn 成功后计数清零。
- **MaxOutputTokensRule**:override is None → 升档(幂等:第二次进续写)、续写注入 meta + 占位(基于 tool_calls)、count≥3 耗尽 Terminal。withhold 不执行工具(tool_calls 全占位)。
- **PromptTooLongErrorRule / ModelErrorRule**:返回对应 Terminal。
- **stream_turn withheld**:mock SSE stop_reason=max_tokens → withheld="max_output_tokens";正常 end_turn → None。
- **JSON 截断容错**:mock SSE 发残缺 input_json_delta + content_block_stop → aggregate_stream 不崩、丢弃残缺 block、stop_reason=max_tokens → withheld。
- **占位**:TrackedTool 创建即 is_error 占位;执行成功回填;cancel 保持占位;get_results 不过滤(返回数 == tracked 数)。
- **query_loop 主干**:网络抖动→重试→成功(最终 COMPLETED,用户无感);max_tokens→升档→成功;Transient 超限→Terminal MODEL_ERROR;编程 bug(KeyError)不被 except ProviderError 吞(冒泡)。
- **withheld 优先**:max_tokens + 有 tool_use → 走 MaxOutputTokensRule 不走回灌(执行 0 个工具)。

## 11. 变更清单(文件级)

| 文件 | 动作 | 内容 |
|------|------|------|
| `core/provider_errors.py` | 新建 | ProviderError 体系 + UserInterruptError(预留) |
| `core/providers/anthropic.py` | 改 | 抛自定义异常;try TransportError;`_classify_status_error` / `_classify_stream_error` |
| `core/types.py` | 改 | `ContinueReason.NETWORK_RETRY`;`TerminalReason.USER_INTERRUPT`(替 ABORTED);`State.network_retry_count`;`ESCALATED_MAX_TOKENS=64000` |
| `core/tool_executor/base.py` | 改 | `_placeholder` 工厂;`TrackedTool.result` 占位;`add_tool` 预占位;`get_results` 不过滤 None |
| `core/loop/phases/stream_turn.py` | 改 | withheld 检测(stop_reason=max_tokens);JSON 截断容错;`StreamOutcome.withheld` 收窄 |
| `core/loop/recovery/base.py` | 改 | `handle`/`handle_error` async;`error_rules`;`ErrorRule` protocol |
| `core/loop/recovery/rules.py` | 改 | `apply` async;激活 MaxOutputTokensRule;新增 NetworkRetryRule/PromptTooLongErrorRule/ModelErrorRule;`build_recovery_chain` 双链 |
| `core/loop/orchestrator.py` | 改 | while 内 try/except ProviderError;withheld 优先;`network_retry_count` 清零;USER_INTERRUPT |

## 12. 权衡与未做(YAGNI)

- **prompt_too_long 压缩恢复留 Phase5**:`maybe_compact` 是桩,本次只 Terminal 终止,不实现压缩。Phase5 实现后 PromptTooLongErrorRule 升级为压缩重试。
- **UserInterrupt 流式中断留扩展**:本次不接入 stream 循环,只留 `UserInterruptError` + 命名。缺陷 5 本次未修(标注为已知,留扩展点)。
- **续写 meta 文案**:照 notes,英文,不改。
- **重试参数硬编码**:`NETWORK_RETRY_LIMIT=3` / `NETWORK_BACKOFF_BASE=1.0` 常量,不进 QueryParams(YAGNI,生产需要时再提配置)。
- **不引入新依赖**:jitter 用 `random.uniform`,退避用 `asyncio.sleep`,无新包。
