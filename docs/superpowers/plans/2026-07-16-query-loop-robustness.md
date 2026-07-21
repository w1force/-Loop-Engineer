# query_loop 健壮性加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `query_loop` 对模型输出截断、工具匹配错配、网络波动等故障具备容错——业务异常在 while 内被 catch,经责任链翻译成 State 变换,确保下次循环正确重试或终止。

**Architecture:** provider adapter 把 HTTP/SSE/transport 错误分类为自定义异常 → `query_loop` while 内 `except ProviderError` 喂给责任链的 `handle_error` → 规则产出 `Decision(transition, next_state)` → continue/return。配对完整性由 executor 预占位设计保证(`TrackedTool` 创建即 `is_error` 占位)。max_output_tokens 走 withheld 路径(优先于 needs_follow_up)触发升档/续写。

**Tech Stack:** Python 3、pytest(asyncio_mode=auto)、pydantic v2、respx(httpx mock)、httpx、asyncio。

## Global Constraints

(每个 task 的需求隐式包含本节,值照 spec 抄)

- pytest `asyncio_mode = auto`:测试直接 `async def test_xxx()`,**不加** `@pytest.mark.asyncio`。
- pyright `typeCheckingMode = "basic"` 必须通过。
- HTTP mock 用 `respx`;SSE/事件流用手写 `StreamEvent` 序列或 SSE 字符串。
- **不引入新依赖**:jitter 用 `random.uniform`,退避用 `asyncio.sleep`。
- 精确常量(照抄):`ESCALATED_MAX_TOKENS = 64_000`、`NETWORK_RETRY_LIMIT = 3`、`NETWORK_BACKOFF_BASE = 1.0`、`MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3`(已存在,不改)。
- `TerminalReason.ABORTED` **重命名**为 `USER_INTERRUPT`。
- 中文注释(对齐现有代码风格)。
- 留在 `dev/lwt` 分支,**不提交 main**;每 task 末尾 commit。

## File Structure

| 文件 | 责任 | 动作 |
|------|------|------|
| `core/provider_errors.py` | provider 业务异常体系 | 新建 |
| `core/types.py` | 枚举 + State 字段 + 常量 | 改 |
| `core/tool_executor/base.py` | 占位设计(`_placeholder`/TrackedTool/get_results) | 改 |
| `core/loop/phases/stream_turn.py` | withheld 检测 + JSON 截断容错 | 改 |
| `core/providers/anthropic.py` | 抛分类异常 + TransportError 捕获 | 改 |
| `core/loop/recovery/base.py` | chain async + `handle_error` + `ErrorRule` | 改 |
| `core/loop/recovery/rules.py` | MaxOutputTokensRule 两档 + 错误规则 + 双链 | 改 |
| `core/loop/orchestrator.py` | query_loop try/except + withheld 优先 + 清零 | 改 |

测试对应:`tests/test_provider_errors.py`(新)、`tests/test_types.py`、`tests/test_tool_executor/test_base.py`、`tests/test_tool_executor/test_streaming.py`、`tests/test_aggregate.py`、`tests/test_stream_turn_executor.py`、`tests/test_anthropic.py`、`tests/test_recovery_chain.py`、`tests/test_orchestrator.py`。

---

### Task 1: provider 异常体系

**Files:**
- Create: `core/provider_errors.py`
- Test: `tests/test_provider_errors.py`

**Interfaces:**
- Produces: `ProviderError(message, *, status, body)`(基类);`TransientProviderError` / `PromptTooLongError` / `FatalProviderError`(子类,`isinstance` 分发);`UserInterruptError`(独立,非 ProviderError)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_provider_errors.py
from core.provider_errors import (
    FatalProviderError,
    PromptTooLongError,
    ProviderError,
    TransientProviderError,
    UserInterruptError,
)


def test_transient_is_provider_error_with_status_body():
    e = TransientProviderError("conn reset", status=429, body=b"busy")
    assert isinstance(e, ProviderError)
    assert e.status == 429
    assert e.body == b"busy"
    assert str(e) == "conn reset"


def test_prompt_too_long_and_fatal_are_provider_errors():
    assert isinstance(PromptTooLongError("x", status=400), ProviderError)
    assert isinstance(FatalProviderError("x", status=401), ProviderError)


def test_isinstance_dispatch_distinguishes_subclasses():
    assert isinstance(TransientProviderError("x"), TransientProviderError)
    assert not isinstance(FatalProviderError("x"), TransientProviderError)
    assert not isinstance(PromptTooLongError("x"), FatalProviderError)


def test_user_interrupt_is_not_provider_error():
    # UserInterruptError 走独立路径, 不被 except ProviderError 接住
    assert not isinstance(UserInterruptError(), ProviderError)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_provider_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.provider_errors'`

- [ ] **Step 3: 实现**

```python
# core/provider_errors.py
"""provider 层业务异常体系 (query_loop 健壮性 spec §3.1)。

adapter 把 HTTP / SSE / transport 错误分类为这三类, query_loop 的 except ProviderError
按 isinstance 分发到责任链错误规则。UserInterruptError 独立 (非 provider 错误, §7)。
"""
from __future__ import annotations


class ProviderError(Exception):
    """provider 业务异常基类。query_loop 的 except ProviderError 接它。"""

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        body: bytes | None = None,
    ):
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
    """用户主动中断。本次定义但不触发; 未来 stream 循环接入 abort_signal 时 raise (§7)。"""
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_provider_errors.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: commit**

```bash
git add core/provider_errors.py tests/test_provider_errors.py
git commit -m "feat: provider 异常体系 (Transient/PromptTooLong/Fatal/UserInterrupt)"
```

---

### Task 2: types 枚举 + State 字段 + 常量

**Files:**
- Modify: `core/types.py`(改 `ContinueReason` / `TerminalReason` / `State` / `ESCALATED_MAX_TOKENS`)
- Modify: `core/loop/orchestrator.py:83`(`ABORTED` → `USER_INTERRUPT`,避免删枚举后 import 报错)
- Test: `tests/test_types.py`

**Interfaces:**
- Produces: `ContinueReason.NETWORK_RETRY`;`TerminalReason.USER_INTERRUPT`(替 `ABORTED`);`State.network_retry_count: int = 0`;`ESCALATED_MAX_TOKENS = 64_000`。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_types.py`;若文件顶部缺以下 import 则补)

```python
from core.types import (
    ESCALATED_MAX_TOKENS,
    ContinueReason,
    State,
    TerminalReason,
    UserMessage,
)


def test_network_retry_continue_reason_exists():
    assert ContinueReason.NETWORK_RETRY.value == "network_retry"


def test_user_interrupt_replaces_aborted():
    assert TerminalReason.USER_INTERRUPT.value == "user_interrupt"
    assert not hasattr(TerminalReason, "ABORTED")


def test_state_network_retry_count_defaults_zero():
    s = State(messages=[UserMessage(content="hi")])
    assert s.network_retry_count == 0


def test_escalated_max_tokens_is_64000():
    assert ESCALATED_MAX_TOKENS == 64_000
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_types.py -v`
Expected: FAIL — `AttributeError`(NETWORK_RETRY / USER_INTERRUPT / network_retry_count 不存在),`assert ESCALATED_MAX_TOKENS == 64000` 失败(现为 32000)。

- [ ] **Step 3: 实现**

改 `core/types.py`:

`ContinueReason` 加一项(`NEXT_TURN` 之后):
```python
class ContinueReason(str, Enum):
    NEXT_TURN = "next_turn"
    NETWORK_RETRY = "network_retry"                       # 新增
    MAX_OUTPUT_TOKENS_ESCALATE = "max_output_tokens_escalate"
    MAX_OUTPUT_TOKENS_RECOVERY = "max_output_tokens_recovery"
    REACTIVE_COMPACT_RETRY = "reactive_compact_retry"
```

`TerminalReason` 把 `ABORTED` 改名为 `USER_INTERRUPT`:
```python
class TerminalReason(str, Enum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    USER_INTERRUPT = "user_interrupt"                     # 原 ABORTED
    MODEL_ERROR = "model_error"
    PROMPT_TOO_LONG = "prompt_too_long"
    BUDGET_EXCEEDED = "budget_exceeded"
```

`State` 加字段:
```python
class State(BaseModel):
    messages: list[Message]
    turn_count: int = 1
    max_output_tokens_recovery_count: int = 0
    max_output_tokens_override: int | None = None
    has_attempted_autocompact: bool = False
    network_retry_count: int = 0                          # 新增
    transition: Continue | Terminal | None = None
```

常量改值:
```python
ESCALATED_MAX_TOKENS = 64_000  # 占位 32000 → 64000 (Sonnet output 上限)
```

同步改 `core/loop/orchestrator.py:83`(仅这一行,避免引用已删的 `ABORTED`):
```python
            _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_types.py tests/test_orchestrator.py -v`
Expected: PASS。`test_orchestrator.py` 现有用例不破坏(仅枚举名改)。

- [ ] **Step 5: commit**

```bash
git add core/types.py core/loop/orchestrator.py tests/test_types.py
git commit -m "feat: types 加 NETWORK_RETRY/USER_INTERRUPT/network_retry_count, ESCALATED=64000"
```

---

### Task 3: 占位设计(executor 不变量)

**Files:**
- Modify: `core/tool_executor/base.py`
- Modify: `tests/test_tool_executor/test_streaming.py`(6 处 `TrackedTool(...)` 适配 result 必填)
- Test: `tests/test_tool_executor/test_base.py`

**Interfaces:**
- Consumes: `ToolResultBlock`(`core/types`)。
- Produces: `_placeholder(block: ToolUseBlock, reason: str = ...) -> ToolResultBlock`;`TrackedTool.result: ToolResultBlock`(必填,去 `| None`);`add_tool` 创建即赋占位;`get_results` 返回 `[t.result for t in self._tracked]`(不过滤 None)。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_tool_executor/test_base.py`;顶部 import 补 `_placeholder`)

```python
from core.tool_executor.base import _placeholder  # 追加到现有 import 行

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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_tool_executor/test_base.py -v`
Expected: FAIL — `ImportError: cannot import name '_placeholder'`。

- [ ] **Step 3: 实现**

改 `core/tool_executor/base.py`。

新增占位工厂(放在 `TrackedTool` 之前):
```python
_PLACEHOLDER_REASON = "tool execution interrupted"


def _placeholder(block: ToolUseBlock, reason: str = _PLACEHOLDER_REASON) -> ToolResultBlock:
    """造 is_error 占位 result (执行前预设 / cancel / 续写未执行 都用它)。"""
    return ToolResultBlock(tool_use_id=block.id, content=reason, is_error=True)
```

`TrackedTool.result` 去掉 `| None`:
```python
@dataclass
class TrackedTool:
    """一次 tool_use 的执行档案。"""

    block: ToolUseBlock
    status: Literal["queued", "executing", "completed", "cancelled"] = "queued"
    result: ToolResultBlock  # 创建即占位(去 | None)
    task: asyncio.Task | None = None
```

`add_tool` 创建 tracked 即赋占位:
```python
    def add_tool(self, block: ToolUseBlock) -> None:
        """收集 tool_use(block 级)。基类入队(保序) + 预占位; 未知工具直接造 error。"""
        if self._discarded:
            return
        tracked = TrackedTool(block=block, result=_placeholder(block))  # ★ 预占位
        self._tracked.append(tracked)
        _t = self._tools.get(block.name)
        _safe = "?" if _t is None else ("safe" if _t.is_concurrency_safe else "unsafe")
        logger.info("add_tool %s %s input=%s [%s]", block.id, block.name, block.input, _safe)
        if block.name not in self._tools:  # 未知工具: 覆盖占位为具体 error
            tracked.result = ToolResultBlock(
                tool_use_id=block.id, content=f"未知工具: {block.name}", is_error=True
            )
            tracked.status = "completed"
            return
        self._on_add(tracked)
```

`get_results` 去掉 `if t.result is not None` 过滤:
```python
    async def get_results(self) -> list[ToolResultBlock]:
        """收尾: 保证全部执行完, 按 _tracked 顺序返回(保序)。占位设计下 result 恒非 None。"""
        await self._run_all()
        return [t.result for t in self._tracked]
```

`_execute_single` 的 `finally` 去掉 `if result is not None` 条件(占位已在,result 变量在非 cancel 路径恒非 None,cancel 路径 result 为 None 时不应覆盖占位 → 保留条件)。**结论:`_execute_single` 不动**——它的 `finally: if result is not None: tracked.result = result` 正好实现"成功覆盖占位、cancel 不覆盖"。

适配 `tests/test_tool_executor/test_streaming.py` 的 6 处直接构造(`L111/112/114/117/118` 等),把 `TrackedTool(ToolUseBlock(id=..., name=..., input={}))` 改为带 result。在文件顶部加 import,并把构造改为通过 helper:

顶部 import 补:
```python
from core.tool_executor.base import TrackedTool
from core.types import ToolResultBlock
```
新增 helper:
```python
def _tracked(tid: str, name: str) -> TrackedTool:
    blk = ToolUseBlock(id=tid, name=name, input={})
    return TrackedTool(block=blk, result=ToolResultBlock(tool_use_id=tid, content="ph", is_error=True))
```
6 处替换示例(`L111`):`TrackedTool(ToolUseBlock(id="a", name="r", input={}))` → `_tracked("a", "r")`,其余同理。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_tool_executor/ -v`
Expected: PASS(test_base 新增 3 个 + 现有全过;test_streaming 6 处适配后过)。

- [ ] **Step 5: commit**

```bash
git add core/tool_executor/base.py tests/test_tool_executor/
git commit -m "feat: executor 占位设计 (TrackedTool 预占位 is_error, get_results 不过滤 None)"
```

---

### Task 4: stream_turn withheld 检测 + JSON 截断容错

**Files:**
- Modify: `core/loop/phases/stream_turn.py`(`aggregate_stream` 的 json.loads 容错 + `stream_turn` 末尾 withheld 检测)
- Test: `tests/test_aggregate.py`(JSON 容错)、`tests/test_stream_turn_executor.py`(withheld 检测)

**Interfaces:**
- Produces: `StreamOutcome.withheld` 取值 `"max_output_tokens" | None`(收窄,不再有 `"prompt_too_long"`)。`aggregate_stream` 对残缺 input 不抛、丢弃该 block。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_aggregate.py`:
```python
async def test_truncated_tool_input_dropped_not_raised():
    """max_tokens 截断 tool_use input 中途: input_buf 残缺 → 丢弃 block, 不抛。"""
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0,
                    block={"type": "tool_use", "id": "c1", "name": "f", "input": {}}),
        StreamEvent(type="content_block_delta", index=0,
                    delta={"tool_input": '{"city": "Par'}),  # 残缺 JSON
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="message_delta", delta={"stop_reason": "max_tokens"}),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]
    assert _assts(out) == []  # 残缺 tool_use 被丢弃, 不 yield
```

追加到 `tests/test_stream_turn_executor.py`:
```python
def _seq_max_tokens_text():
    """纯 text 输出撞 max_tokens: stop_reason=max_tokens。"""
    return [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0, block={"type": "text", "text": ""}),
        StreamEvent(type="content_block_delta", index=0, delta={"text": "半句话"}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="message_delta",
                    delta={"stop_reason": "max_tokens"},
                    message={"usage": {"input_tokens": 1, "output_tokens": 1}}),
        StreamEvent(type="message_stop"),
    ]


async def test_withheld_max_output_tokens():
    class _FakeProvider:
        def stream(self, **kwargs):
            async def _g():
                for e in _seq_max_tokens_text():
                    yield e
            return _g()
        def count_tokens(self, messages): return 0

    state = State(messages=[UserMessage(content="hi")])
    params = QueryParams(
        messages=state.messages, system="", model="m", max_tokens=16,
        provider=_FakeProvider(), abort_signal=asyncio.Event(),
    )
    outcome = await stream_turn(state, params, NoopTracer(), None)
    assert outcome.withheld == "max_output_tokens"
    assert outcome.stop_reason == "max_tokens"


async def test_withheld_none_when_end_turn():
    class _FakeProvider:
        def stream(self, **kwargs):
            async def _g():
                for e in [
                    StreamEvent(type="message_start"),
                    StreamEvent(type="content_block_start", index=0, block={"type": "text", "text": ""}),
                    StreamEvent(type="content_block_delta", index=0, delta={"text": "done"}),
                    StreamEvent(type="content_block_stop", index=0),
                    StreamEvent(type="message_delta", delta={"stop_reason": "end_turn"},
                                message={"usage": {"input_tokens": 1, "output_tokens": 1}}),
                    StreamEvent(type="message_stop"),
                ]:
                    yield e
            return _g()
        def count_tokens(self, messages): return 0

    state = State(messages=[UserMessage(content="hi")])
    params = QueryParams(
        messages=state.messages, system="", model="m", max_tokens=16,
        provider=_FakeProvider(), abort_signal=asyncio.Event(),
    )
    outcome = await stream_turn(state, params, NoopTracer(), None)
    assert outcome.withheld is None
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_aggregate.py tests/test_stream_turn_executor.py -v`
Expected: FAIL — truncated 测试 `json.JSONDecodeError` 抛出;withheld 测试 `outcome.withheld is None`(未实现检测)。

- [ ] **Step 3: 实现**

`aggregate_stream` 的 `content_block_stop` 分支(`stream_turn.py:84-92`)改为 try/except:
```python
        elif evt.type == "content_block_stop":
            idx = evt.index
            if idx is None:  # 守卫
                continue
            b = blocks[idx]
            if b.get("type") == "tool_use":
                try:
                    b["input"] = json.loads(b.pop("input_buf", "") or "{}")
                except json.JSONDecodeError:
                    # max_tokens 截断导致 input 残缺: 丢弃该 block, 由 stop_reason withhold 兜底
                    continue
            # block 级固化
            yield AssistantMessage(content=[_to_block(b)])
```

`stream_turn` 末尾(`stream_turn.py:170-180`)加 withheld 检测:
```python
    withheld = None
    if stop_reason == "max_tokens":
        withheld = "max_output_tokens"
    # 组装整轮追加到 yielded 末尾
    full = AssistantMessage(content=all_blocks, usage=usage, stop_reason=stop_reason)
    yielded.append(full)
    return StreamOutcome(
        assistant_msgs=[full],
        tool_calls=tool_calls,
        needs_follow_up=needs_follow_up,
        stop_reason=stop_reason,
        withheld=withheld,
        yielded=yielded,
    )
```

`StreamOutcome.withheld` 注释/类型收窄(`stream_turn.py:114`):
```python
    withheld: str | None = None  # None | "max_output_tokens" (prompt_too_long 走异常路径)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_aggregate.py tests/test_stream_turn_executor.py -v`
Expected: PASS。

- [ ] **Step 5: commit**

```bash
git add core/loop/phases/stream_turn.py tests/test_aggregate.py tests/test_stream_turn_executor.py
git commit -m "feat: stream_turn withheld(max_tokens)检测 + tool_use input 截断容错"
```

---

### Task 5: anthropic adapter 抛分类异常

**Files:**
- Modify: `core/providers/anthropic.py`(status/stream error/transport 三处分类抛出)
- Test: `tests/test_anthropic.py`

**Interfaces:**
- Consumes: `core.provider_errors` 的四类异常(Task 1)。
- Produces: `AnthropicAdapter._classify_status_error(status, body) -> ProviderError`;`_classify_stream_error(evt) -> ProviderError`;`stream` 抛这些异常而非 `httpx.HTTPStatusError`,并捕获 `httpx.TransportError` 转 `TransientProviderError`。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_anthropic.py`;顶部 import 补异常类)

```python
from core.provider_errors import (
    FatalProviderError,
    PromptTooLongError,
    TransientProviderError,
)
import pytest

PROMPT_TOO_LONG_BODY = (
    '{"type":"error","error":{"type":"invalid_request_error",'
    '"message":"prompt is too long: 200000 tokens > 195000 maximum"}}'
)


@respx.mock
async def test_classify_429_to_transient():
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(429, text="rate"))
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    with pytest.raises(TransientProviderError):
        async for _ in adapter.stream(
            messages=[UserMessage(content="hi")], system="", tools=[], model="m",
            max_tokens=16, abort_signal=asyncio.Event(), tracer=NoopTracer(),
        ):
            pass


@respx.mock
async def test_classify_500_to_transient():
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(503, text="down"))
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    with pytest.raises(TransientProviderError):
        async for _ in adapter.stream(
            messages=[UserMessage(content="hi")], system="", tools=[], model="m",
            max_tokens=16, abort_signal=asyncio.Event(), tracer=NoopTracer(),
        ):
            pass


@respx.mock
async def test_classify_prompt_too_long_400():
    respx.post(f"{BASE}/v1/messages").mock(
        return_value=httpx.Response(400, text=PROMPT_TOO_LONG_BODY))
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    with pytest.raises(PromptTooLongError):
        async for _ in adapter.stream(
            messages=[UserMessage(content="hi")], system="", tools=[], model="m",
            max_tokens=16, abort_signal=asyncio.Event(), tracer=NoopTracer(),
        ):
            pass


@respx.mock
async def test_classify_other_400_to_fatal():
    respx.post(f"{BASE}/v1/messages").mock(
        return_value=httpx.Response(400, text='{"error":{"message":"bad model"}}'))
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    with pytest.raises(FatalProviderError):
        async for _ in adapter.stream(
            messages=[UserMessage(content="hi")], system="", tools=[], model="m",
            max_tokens=16, abort_signal=asyncio.Event(), tracer=NoopTracer(),
        ):
            pass


@respx.mock
async def test_stream_overloaded_error_is_transient():
    sse = (
        'event: error\n'
        'data: {"type":"error","error":{"type":"overloaded_error","message":"overloaded"}}\n\n'
    )
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=sse))
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    with pytest.raises(TransientProviderError):
        async for _ in adapter.stream(
            messages=[UserMessage(content="hi")], system="", tools=[], model="m",
            max_tokens=16, abort_signal=asyncio.Event(), tracer=NoopTracer(),
        ):
            pass


@respx.mock
async def test_stream_generic_error_is_fatal():
    sse = (
        'event: error\n'
        'data: {"type":"error","error":{"type":"api_error","message":"boom"}}\n\n'
    )
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=sse))
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    with pytest.raises(FatalProviderError):
        async for _ in adapter.stream(
            messages=[UserMessage(content="hi")], system="", tools=[], model="m",
            max_tokens=16, abort_signal=asyncio.Event(), tracer=NoopTracer(),
        ):
            pass


@respx.mock
async def test_transport_error_becomes_transient():
    respx.post(f"{BASE}/v1/messages").mock(side_effect=httpx.ConnectError("conn refused"))
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    with pytest.raises(TransientProviderError):
        async for _ in adapter.stream(
            messages=[UserMessage(content="hi")], system="", tools=[], model="m",
            max_tokens=16, abort_signal=asyncio.Event(), tracer=NoopTracer(),
        ):
            pass
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_anthropic.py -v`
Expected: FAIL — 现实现抛 `httpx.HTTPStatusError`,断言 `TransientProviderError` 等不匹配。

- [ ] **Step 3: 实现**

改 `core/providers/anthropic.py`。顶部 import:
```python
import httpx

from ..provider_errors import (
    FatalProviderError,
    PromptTooLongError,
    ProviderError,
    TransientProviderError,
)
```

`stream` 方法体:把 `async with` 包进 `try/except httpx.TransportError`,status≠200 与流 error 改抛分类异常:
```python
    async def stream(self, *, messages, system, tools, model, max_tokens, abort_signal, tracer, **opts):
        tracer.emit(TraceEvent(kind=TraceKind.PROVIDER_REQUEST,
                               payload={"model": model, "msg_count": len(messages)}))
        req_body = {
            "model": model,
            "messages": to_anthropic(messages),
            "system": system,
            "tools": to_anthropic_tools(tools),
            "max_tokens": max_tokens,
            "stream": True,
        }
        print("request body: " + json.dumps(req_body, ensure_ascii=False))
        try:
            async with self.http.stream("POST", "/v1/messages", json=req_body) as r:
                _t0 = time.perf_counter()
                if r.status_code != 200:
                    body = await r.aread()
                    tracer.emit(TraceEvent(kind=TraceKind.PROVIDER_ERROR,
                                           payload={"status": r.status_code, "body": body[:200]}))
                    raise self._classify_status_error(r.status_code, body)
                async for data in parse_sse(r):
                    if self._debug_sse:
                        print(f"[sse +{time.perf_counter() - _t0:6.3f}s] {data}", file=sys.stderr, flush=True)
                    if data == "[DONE]":
                        break
                    evt = json.loads(data)
                    t = evt.get("type")
                    if t == "ping":
                        continue
                    if t == "error":
                        tracer.emit(TraceEvent(kind=TraceKind.PROVIDER_ERROR, payload=evt))
                        raise self._classify_stream_error(evt)
                    if t not in _CONTENT_EVENT_TYPES:
                        continue
                    yield self._to_stream_event(evt)
        except httpx.TransportError as e:
            tracer.emit(TraceEvent(kind=TraceKind.PROVIDER_ERROR,
                                   payload={"transport": type(e).__name__}))
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
        err = evt.get("error") or {}
        if err.get("type") == "overloaded_error":
            return TransientProviderError(f"stream overloaded: {err}")
        return FatalProviderError(f"stream error: {err}")
```

> `import httpx` 已存在(顶部),无需重复。`httpx.HTTPStatusError` 不再使用,删除其 import 用法(若有)。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_anthropic.py -v`
Expected: PASS(新增 7 个 + 现有 SSE 透传/ping 用例不破坏)。

- [ ] **Step 5: commit**

```bash
git add core/providers/anthropic.py tests/test_anthropic.py
git commit -m "feat: anthropic adapter 抛分类异常 (Transient/PromptTooLong/Fatal) + TransportError 捕获"
```

---

### Task 6: 责任链 async + handle_error + ErrorRule

**Files:**
- Modify: `core/loop/recovery/base.py`
- Modify: `tests/test_recovery_chain.py`(现有 `handle` 同步调用改 `await`)

**Interfaces:**
- Consumes: `ProviderError`(Task 1)、`Decision`(已有)。
- Produces: `TransitionRule.apply` 与新增 `ErrorRule.apply` 均 `async`;`RecoveryChain(rules, error_rules)`;`async handle(...)`;`async handle_error(state, err, params, tracer) -> Decision`(无匹配 → `Terminal(MODEL_ERROR)`)。

- [ ] **Step 1: 改现有测试为 async + 加 handle_error 兜底测试**

`tests/test_recovery_chain.py` 现有三个测试把 `chain.handle(...)` 改 `await chain.handle(...)`(函数已是 `async def`,加 await 即可):
```python
async def test_no_withheld_falls_through_to_completed():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    decision = await chain.handle(state, _outcome(None), params=None, tracer=NoopTracer())
    assert isinstance(decision.transition, Terminal)
    assert decision.transition.reason is TerminalReason.COMPLETED


async def test_completed_rule_emits_recovery_attempt():
    spy = SpyTracer()
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    await chain.handle(state, _outcome(None), params=None, tracer=spy)
    hits = [e for e in spy.events
            if e.kind is TraceKind.RECOVERY_ATTEMPT and e.payload.get("rule") == "completed"]
    assert len(hits) == 1


async def test_stub_rules_never_match_when_no_withheld():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    for _ in range(3):
        d = await chain.handle(state, _outcome(None), params=None, tracer=NoopTracer())
        assert d.transition.reason is TerminalReason.COMPLETED
```

新增 handle_error 兜底测试(用 `ProviderError` **基类**实例:Task 8 的 NetworkRetry/PromptTooLong/ModelError 规则都 `isinstance(子类)`,基类不 match 任何规则 → 稳定走兜底;这样 Task 8 填规则后该测试仍 pass):
```python
from core.provider_errors import ProviderError
from core.types import TerminalReason

async def test_handle_error_fallback_terminal_model_error():
    """无错误规则匹配时, handle_error 兜底返回 Terminal(MODEL_ERROR)。"""
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    d = await chain.handle_error(
        state, ProviderError("x"), params=None, tracer=NoopTracer())
    assert isinstance(d.transition, Terminal)
    assert d.transition.reason is TerminalReason.MODEL_ERROR
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_recovery_chain.py -v`
Expected: FAIL — `handle` 不是 async(或 `await` 报错);`handle_error` 不存在。

- [ ] **Step 3: 实现**

改 `core/loop/recovery/base.py`:
```python
"""恢复/退出判定责任链引擎 (P2 §5.1 + 健壮性 spec §3.4)。

正常链(rules)基于 outcome.withheld; 错误链(error_rules)基于 ProviderError。
两条链同一套 Decision 模型。handle / handle_error / rule.apply 均为 async。
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from ...types import Continue, State, Terminal, TerminalReason
from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

from ..phases.stream_turn import StreamOutcome
from ..provider_errors import ProviderError  # 注意: core/provider_errors.py


class Decision(BaseModel):
    transition: Continue | Terminal
    next_state: State | None = None


class TransitionRule(Protocol):
    name: str

    def match(self, state: State, outcome: StreamOutcome) -> bool: ...

    async def apply(
        self, state: State, outcome: StreamOutcome, params, tracer: Tracer
    ) -> Decision: ...


class ErrorRule(Protocol):
    name: str

    def match(self, state: State, err: ProviderError) -> bool: ...

    async def apply(
        self, state: State, err: ProviderError, params, tracer: Tracer
    ) -> Decision: ...


class RecoveryChain:
    def __init__(self, rules: list[TransitionRule], error_rules: list[ErrorRule]):
        self.rules = rules
        self.error_rules = error_rules

    async def handle(self, state, outcome, params, tracer: Tracer) -> Decision:
        for rule in self.rules:
            if rule.match(state, outcome):
                tracer.emit(TraceEvent(
                    kind=TraceKind.RECOVERY_ATTEMPT,
                    payload={"rule": rule.name, "withheld": outcome.withheld}))
                return await rule.apply(state, outcome, params, tracer)
        return Decision(transition=Terminal(reason=TerminalReason.COMPLETED))

    async def handle_error(self, state, err: ProviderError, params, tracer: Tracer) -> Decision:
        for rule in self.error_rules:
            if rule.match(state, err):
                tracer.emit(TraceEvent(
                    kind=TraceKind.RECOVERY_ATTEMPT,
                    payload={"rule": rule.name, "error": type(err).__name__}))
                return await rule.apply(state, err, params, tracer)
        return Decision(transition=Terminal(reason=TerminalReason.MODEL_ERROR, error=str(err)))
```

> 注意 import 路径:`base.py` 在 `core/loop/recovery/`,`provider_errors` 在 `core/`,所以 `from ...provider_errors import ProviderError`(三个点回 core)。实现时核对:`core/loop/recovery/base.py` → `..`=`core/loop/` → `...`=`core/`。✓

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_recovery_chain.py -v`
Expected: PASS(4 个)。注意:`build_recovery_chain()` 此时返回的 `RecoveryChain` 仍是旧签名(单参数)→ 本 task 需同步改 `rules.py` 的 `build_recovery_chain` 为双参数(暂传空 `error_rules=[]`),否则 `__init__` 报错。在 Step 3 同时改 `core/loop/recovery/rules.py` 的 `build_recovery_chain`:
```python
def build_recovery_chain() -> RecoveryChain:
    return RecoveryChain(
        rules=[MaxOutputTokensRule(), CompletedRule()],   # Task 7/8 会充实
        error_rules=[],                                    # Task 8 填错误规则
    )
```
并确保 `rules.py` 里现有规则 `apply` 改 `async def`(CompletedRule、以及 Task 7 处理的 MaxOutputTokensRule)。**本 task 最小改动**:把 `CompletedRule.apply` 改 `async def`,删旧 `PromptTooLongRule`(withheld 恒 None 已无意义)。运行 `pytest tests/test_recovery_chain.py tests/test_orchestrator.py -v` 全过。

- [ ] **Step 5: commit**

```bash
git add core/loop/recovery/base.py core/loop/recovery/rules.py tests/test_recovery_chain.py
git commit -m "feat: 责任链 async + handle_error + ErrorRule (error_rules 占位)"
```

---

### Task 7: MaxOutputTokensRule 两档(升档 / 续写 / 耗尽)

**Files:**
- Modify: `core/loop/recovery/rules.py`
- Test: `tests/test_recovery_chain.py`

**Interfaces:**
- Consumes: `StreamOutcome.withheld`(Task 4)、`_placeholder`(Task 3)、`ESCALATED_MAX_TOKENS` / `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT`(Task 2 / 已有)、`ContinueReason.MAX_OUTPUT_TOKENS_ESCALATE|RECOVERY`(已有)、`ToolUseBlock`/`UserMessage`。
- Produces: `MaxOutputTokensRule`(match withheld=="max_output_tokens";升档/续写/耗尽三档)。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_recovery_chain.py`;import 补齐)

```python
from core.loop.phases.stream_turn import StreamOutcome
from core.types import (
    AssistantMessage, ContinueReason, ESCALATED_MAX_TOKENS, TextBlock, ToolUseBlock,
)

def _outcome_max_tokens(tool_calls=None) -> StreamOutcome:
    return StreamOutcome(
        assistant_msgs=[AssistantMessage(content=[TextBlock(text="半句")])],
        tool_calls=tool_calls or [],
        needs_follow_up=False,
        withheld="max_output_tokens",
    )


async def test_max_tokens_escalate_first_time():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")])
    d = await chain.handle(state, _outcome_max_tokens(), params=None, tracer=NoopTracer())
    assert d.transition.reason is ContinueReason.MAX_OUTPUT_TOKENS_ESCALATE
    assert d.next_state.max_output_tokens_override == ESCALATED_MAX_TOKENS


async def test_max_tokens_recovery_injects_meta_and_placeholders():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")],
                  max_output_tokens_override=ESCALATED_MAX_TOKENS)  # 已升档 → 进续写
    tc = [ToolUseBlock(id="c1", name="get", input={"x": 1})]
    d = await chain.handle(state, _outcome_max_tokens(tool_calls=tc),
                           params=None, tracer=NoopTracer())
    assert d.transition.reason is ContinueReason.MAX_OUTPUT_TOKENS_RECOVERY
    # 本轮 assistant + 占位 result + meta 三条进历史
    added = d.next_state.messages[-3:]
    assert added[0] == AssistantMessage(content=[TextBlock(text="半句")])
    # 占位 user message: 1 个 is_error tool_result
    assert added[1].content[0].is_error is True
    assert added[1].content[0].tool_use_id == "c1"
    # meta 文本
    assert "Resume directly" in added[2].content
    assert d.next_state.max_output_tokens_recovery_count == 1


async def test_max_tokens_recovery_no_tool_calls_skips_placeholder():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")],
                  max_output_tokens_override=ESCALATED_MAX_TOKENS)
    d = await chain.handle(state, _outcome_max_tokens(tool_calls=[]),
                           params=None, tracer=NoopTracer())
    added = d.next_state.messages[-2:]  # 仅 assistant + meta, 无占位
    assert added[0] == AssistantMessage(content=[TextBlock(text="半句")])
    assert "Resume directly" in added[1].content


async def test_max_tokens_exhausted_after_three_recovery():
    chain = build_recovery_chain()
    state = State(messages=[UserMessage(content="hi")],
                  max_output_tokens_override=ESCALATED_MAX_TOKENS,
                  max_output_tokens_recovery_count=3)  # 已耗尽
    d = await chain.handle(state, _outcome_max_tokens(), params=None, tracer=NoopTracer())
    assert isinstance(d.transition, Terminal)
    assert d.transition.reason is TerminalReason.MODEL_ERROR
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_recovery_chain.py -v`
Expected: FAIL — 现有 MaxOutputTokensRule 是 `_not_impl` 桩或 withheld 不匹配,返回非预期 transition。

- [ ] **Step 3: 实现**

改 `core/loop/recovery/rules.py`。删除旧 `PromptTooLongRule`、旧 `MaxOutputTokensRule` 桩与 `_not_impl` 引用;新写 `MaxOutputTokensRule`:

顶部 import 调整:
```python
from ...provider_errors import ProviderError  # error 规则用 (Task 8), 此 task 可暂不导
from ...tool_executor.base import _placeholder
from ...types import (
    Continue, ContinueReason, Message, State, Terminal, TerminalReason,
    ToolUseBlock, UserMessage,
)
from telemetry.tracer import Tracer
from ..phases.stream_turn import StreamOutcome
from .base import Decision, ErrorRule, RecoveryChain, TransitionRule
```

```python
_META_RESUME = (
    "Output token limit hit. Resume directly — no apology, no recap. "
    "Pick up mid-thought. Break remaining work into smaller pieces."
)
ESCALATED_MAX_TOKENS_IMPORT = ESCALATED_MAX_TOKENS  # 直接用 types 的常量


class MaxOutputTokensRule:
    """withheld=='max_output_tokens': 升档(1 次)→ 续写(≤3)→ 耗尽 Terminal。"""

    name = "max_output_tokens"

    def match(self, state: State, outcome: StreamOutcome) -> bool:
        return outcome.withheld == "max_output_tokens"

    async def apply(self, state, outcome, params, tracer: Tracer) -> Decision:
        from ...types import ESCALATED_MAX_TOKENS, MAX_OUTPUT_TOKENS_RECOVERY_LIMIT
        # 第一档: 静默升档 (每会话一次) —— 本轮残缺 assistant 丢弃, 只改 max_tokens 重发
        if state.max_output_tokens_override is None:
            return Decision(
                transition=Continue(reason=ContinueReason.MAX_OUTPUT_TOKENS_ESCALATE),
                next_state=state.model_copy(
                    update={"max_output_tokens_override": ESCALATED_MAX_TOKENS}),
            )
        # 第二档: 注入续写消息 —— 本轮完成块 + 占位 result + meta
        if state.max_output_tokens_recovery_count < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
            turn_assistant = outcome.assistant_msgs[0]
            new_msgs: list[Message] = [turn_assistant]
            if outcome.tool_calls:
                placeholders = [_placeholder(tc) for tc in outcome.tool_calls]
                new_msgs.append(UserMessage(content=placeholders))
            new_msgs.append(UserMessage(content=_META_RESUME))
            return Decision(
                transition=Continue(reason=ContinueReason.MAX_OUTPUT_TOKENS_RECOVERY),
                next_state=state.model_copy(update={
                    "messages": state.messages + new_msgs,
                    "max_output_tokens_recovery_count":
                        state.max_output_tokens_recovery_count + 1,
                }),
            )
        # 耗尽
        return Decision(transition=Terminal(
            reason=TerminalReason.MODEL_ERROR, error="max_output_tokens recovery exhausted"))
```

`build_recovery_chain`(双链,Task 8 会填 error_rules):
```python
def build_recovery_chain() -> RecoveryChain:
    return RecoveryChain(
        rules=[MaxOutputTokensRule(), CompletedRule()],
        error_rules=[],
    )
```

`CompletedRule.apply` 保持 `async def`(Task 6 已改)。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_recovery_chain.py -v`
Expected: PASS(8 个:原 4 + 新 4)。

- [ ] **Step 5: commit**

```bash
git add core/loop/recovery/rules.py tests/test_recovery_chain.py
git commit -m "feat: MaxOutputTokensRule 升档/续写/耗尽三档 (withhold 不执行工具, 占位+meta)"
```

---

### Task 8: 错误规则 + 双链完整

**Files:**
- Modify: `core/loop/recovery/rules.py`(加 NetworkRetryRule/PromptTooLongErrorRule/ModelErrorRule,填 error_rules)
- Test: `tests/test_recovery_chain.py`

**Interfaces:**
- Consumes: `ProviderError` 子类(Task 1)、`ContinueReason.NETWORK_RETRY`(Task 2)、`State.network_retry_count`、`asyncio`、`random`。
- Produces: `NETWORK_RETRY_LIMIT=3`、`NETWORK_BACKOFF_BASE=1.0`、`NetworkRetryRule`(指数退避)、`PromptTooLongErrorRule`、`ModelErrorRule`;`build_recovery_chain()` 双链完整。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_recovery_chain.py`)

```python
from core.provider_errors import FatalProviderError, PromptTooLongError

def _state(retry=0):
    return State(messages=[UserMessage(content="hi")], network_retry_count=retry)


async def test_network_retry_under_limit(monkeypatch):
    sleeps = []

    async def _fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", _fake_sleep)
    chain = build_recovery_chain()
    d = await chain.handle_error(
        _state(retry=0), TransientProviderError("x"), params=None, tracer=NoopTracer())
    assert d.transition.reason is ContinueReason.NETWORK_RETRY
    assert d.next_state.network_retry_count == 1
    assert len(sleeps) == 1
    assert sleeps[0] == 1.0  # base * 2^0 = 1.0 (+jitter[0,0.5) → 断言下界)


async def test_network_retry_backoff_doubles(monkeypatch):
    sleeps = []
    async def _fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", _fake_sleep)
    chain = build_recovery_chain()
    # 第三次重试(count=2 → 2^2=4s 基底)
    d = await chain.handle_error(
        _state(retry=2), TransientProviderError("x"), params=None, tracer=NoopTracer())
    assert d.next_state.network_retry_count == 3
    assert sleeps[0] >= 4.0 and sleeps[0] < 4.5


async def test_network_retry_exhausted_terminal(monkeypatch):
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", lambda s: None)
    chain = build_recovery_chain()
    d = await chain.handle_error(
        _state(retry=3), TransientProviderError("x"), params=None, tracer=NoopTracer())
    assert isinstance(d.transition, Terminal)
    assert d.transition.reason is TerminalReason.MODEL_ERROR


async def test_prompt_too_long_terminal():
    chain = build_recovery_chain()
    d = await chain.handle_error(
        _state(), PromptTooLongError("too long", status=400), params=None, tracer=NoopTracer())
    assert isinstance(d.transition, Terminal)
    assert d.transition.reason is TerminalReason.PROMPT_TOO_LONG


async def test_model_error_fatal_terminal():
    chain = build_recovery_chain()
    d = await chain.handle_error(
        _state(), FatalProviderError("boom", status=401), params=None, tracer=NoopTracer())
    assert isinstance(d.transition, Terminal)
    assert d.transition.reason is TerminalReason.MODEL_ERROR
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_recovery_chain.py -v`
Expected: FAIL — error_rules 为空,全走兜底 `Terminal(MODEL_ERROR)`,网络重试/PROMPT_TOO_LONG 断言不匹配。

- [ ] **Step 3: 实现**

`core/loop/recovery/rules.py` 顶部加 import:
```python
import asyncio
import random

from ...provider_errors import (
    FatalProviderError,
    PromptTooLongError,
    ProviderError,
    TransientProviderError,
)
from ...types import Continue, ContinueReason, State, Terminal, TerminalReason
```

新增规则与常量:
```python
NETWORK_RETRY_LIMIT = 3
NETWORK_BACKOFF_BASE = 1.0


def _jitter() -> float:
    return random.uniform(0, 0.5)


class NetworkRetryRule:
    """transient → 指数退避重试(count<3); 超限 Terminal。"""

    name = "network_retry"

    def match(self, state: State, err: ProviderError) -> bool:
        return isinstance(err, TransientProviderError)

    async def apply(self, state, err, params, tracer) -> Decision:
        if state.network_retry_count < NETWORK_RETRY_LIMIT:
            backoff = NETWORK_BACKOFF_BASE * (2 ** state.network_retry_count) + _jitter()
            await asyncio.sleep(backoff)
            return Decision(
                transition=Continue(reason=ContinueReason.NETWORK_RETRY),
                next_state=state.model_copy(update={
                    "network_retry_count": state.network_retry_count + 1}),
            )
        return Decision(transition=Terminal(
            reason=TerminalReason.MODEL_ERROR, error=f"network retry exhausted: {err}"))


class PromptTooLongErrorRule:
    """prompt_too_long → Terminal (压缩恢复留 Phase5)。"""

    name = "prompt_too_long"

    def match(self, state: State, err: ProviderError) -> bool:
        return isinstance(err, PromptTooLongError)

    async def apply(self, state, err, params, tracer) -> Decision:
        return Decision(transition=Terminal(
            reason=TerminalReason.PROMPT_TOO_LONG, error=str(err)))


class ModelErrorRule:
    """fatal → Terminal(MODEL_ERROR)。"""

    name = "model_error"

    def match(self, state: State, err: ProviderError) -> bool:
        return isinstance(err, FatalProviderError)

    async def apply(self, state, err, params, tracer) -> Decision:
        return Decision(transition=Terminal(
            reason=TerminalReason.MODEL_ERROR, error=str(err)))
```

`build_recovery_chain` 填 error_rules:
```python
def build_recovery_chain() -> RecoveryChain:
    return RecoveryChain(
        rules=[MaxOutputTokensRule(), CompletedRule()],
        error_rules=[NetworkRetryRule(), PromptTooLongErrorRule(), ModelErrorRule()],
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_recovery_chain.py -v`
Expected: PASS(全部,含退避时序断言)。

- [ ] **Step 5: commit**

```bash
git add core/loop/recovery/rules.py tests/test_recovery_chain.py
git commit -m "feat: 错误规则 NetworkRetry(退避)/PromptTooLong/ModelError + 双链完整"
```

---

### Task 9: query_loop 主干集成(try/except + withheld 优先 + 清零)

**Files:**
- Modify: `core/loop/orchestrator.py`(query_loop 主干重写)
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `ProviderError`(Task 1)、`chain.handle_error`(Task 6)、各规则(Task 7/8)、`outcome.withheld`(Task 4)、`network_retry_count`(Task 2)。
- Produces: `query_loop` 业务异常在 while 内 catch → `chain.handle_error`;withheld 优先于 needs_follow_up;stream_turn 成功后 `network_retry_count=0`;Terminal 用 `USER_INTERRUPT`。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_orchestrator.py`;import 补 `_ScriptedProvider` 辅助与异常类)

```python
from core.loop.phases.stream_turn import StreamOutcome  # 如需可省
from core.provider_errors import FatalProviderError, TransientProviderError
from core.types import StreamEvent, TerminalReason


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


async def test_network_retry_then_success(monkeypatch):
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", lambda s: None)
    provider = _ScriptedProvider([TransientProviderError("conn"), _text_events_async("ok")])
    spy = SpyTracer()
    out = [m async for m in query_loop(_params_with(provider), spy)]
    assts = [m for m in out if isinstance(m, AssistantMessage)]
    assert len(assts) == 1 and assts[0].content[0].text == "ok"
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions[-1].payload["reason"] == "completed"
    assert provider.calls == 2  # 第一次抖动, 第二次成功


async def test_network_retry_exhausted_terminal(monkeypatch):
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", lambda s: None)
    provider = _ScriptedProvider([TransientProviderError("x")] * 4)
    spy = SpyTracer()
    out = [m async for m in query_loop(_params_with(provider), spy)]
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions[-1].payload["reason"] == "model_error"
    assert provider.calls == 4  # 初试 + 3 次重试


async def test_max_tokens_escalate_then_success(monkeypatch):
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", lambda s: None)
    provider = _ScriptedProvider([
        _text_events_async("半句", stop="max_tokens"),
        _text_events_async("完整", stop="end_turn"),
    ])
    spy = SpyTracer()
    out = [m async for m in query_loop(_params_with(provider), spy)]
    assts = [m for m in out if isinstance(m, AssistantMessage)]
    # 最终轮输出完整(升档重发后)
    assert assts[-1].content[0].text == "完整"
    assert provider.calls == 2


async def test_max_tokens_with_tool_use_does_not_execute(monkeypatch):
    """withheld 优先于 needs_follow_up: max_tokens + tool_use → 不回灌执行工具 → 升档。"""
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", lambda s: None)

    def _max_tokens_tooluse():
        async def _g():
            for e in [
                StreamEvent(type="message_start"),
                StreamEvent(type="content_block_start", index=0,
                            block={"type": "tool_use", "id": "c1", "name": "get", "input": {}}),
                StreamEvent(type="content_block_delta", index=0, delta={"tool_input": '{"city"'}),
                StreamEvent(type="content_block_stop", index=0),  # input 完整可解析
                StreamEvent(type="message_delta", delta={"stop_reason": "max_tokens"},
                            message={"usage": {"input_tokens": 1, "output_tokens": 1}}),
                StreamEvent(type="message_stop"),
            ]:
                yield e
        return _g()

    provider = _ScriptedProvider([_max_tokens_tooluse(), _text_events_async("done")])
    spy = SpyTracer()
    out = [m async for m in query_loop(_params_with(provider), spy)]
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    # 没有因 tool 回灌而 needs_follow_up; 升档后第二轮 completed
    assert transitions[-1].payload["reason"] == "completed"


async def test_prompt_too_long_terminal(monkeypatch):
    from core.provider_errors import PromptTooLongError
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", lambda s: None)
    provider = _ScriptedProvider([PromptTooLongError("too long", status=400)])
    spy = SpyTracer()
    out = [m async for m in query_loop(_params_with(provider), spy)]
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions[-1].payload["reason"] == "prompt_too_long"


async def test_programming_bug_not_swallowed(monkeypatch):
    """非 ProviderError(编程 bug)不被 except 吞, 照常冒泡。"""
    monkeypatch.setattr("core.loop.recovery.rules.asyncio.sleep", lambda s: None)

    class _BugProvider:
        def stream(self, **kwargs):
            raise KeyError("bug")  # 非 ProviderError
        def count_tokens(self, messages):
            return 0

    with pytest.raises(KeyError):
        async for _ in query_loop(_params_with(_BugProvider()), SpyTracer()):
            pass
```

顶部 import 补:
```python
import pytest
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_orchestrator.py -v`
Expected: FAIL — 现有 query_loop 无 try/except,`TransientProviderError` 直接冒泡;无 withheld 优先。

- [ ] **Step 3: 实现**

改 `core/loop/orchestrator.py` 的 `query_loop`(替换现有 while 循环体)。顶部 import 补:
```python
from ..provider_errors import ProviderError
```

```python
async def query_loop(params: QueryParams, tracer: Tracer) -> AsyncIterator[Message | StreamEvent]:
    """内层 agentic loop。业务异常在 while 内 catch → chain.handle_error → State 变换。"""
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
            outcome = await stream_turn(state, params, tracer, executor)
        except ProviderError as e:
            executor.discard()                                  # 清在途, 防泄漏
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

        for m in outcome.yielded:
            yield m
        if params.abort_signal.is_set():
            executor.discard()
            _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
            return

        # withheld 优先于 needs_follow_up (max_tokens 截断不执行残缺工具)
        if outcome.withheld:
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

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_orchestrator.py -v`
Expected: PASS(新增 6 + 原 3 个不破坏)。

- [ ] **Step 5: 全量回归 + pyright**

Run: `pytest -q && pyright`
Expected: 全部测试通过;pyright basic 无新错误。

- [ ] **Step 6: commit**

```bash
git add core/loop/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: query_loop try/except + withheld 优先 + network_retry 清零 + USER_INTERRUPT"
```

---

## Self-Review 记录(写完后自查)

**1. Spec 覆盖:**
- §3 异常体系 → Task 1;adapter 分类 → Task 5;query_loop try/except → Task 9;chain handle_error → Task 6;State 字段 → Task 2。✓
- §4 占位设计 → Task 3。✓
- §5 withheld 检测 → Task 4;JSON 容错 → Task 4;withheld 优先 → Task 9;MaxOutputTokensRule 两档 → Task 7。✓
- §6 NetworkRetry/PromptTooLong/ModelError → Task 8。✓
- §7 UserInterrupt → Task 2(枚举)+ Task 9(Terminal 引用);流式中断留扩展(spec §7/§12 明确不做)。✓
- §10 测试策略 → 各 task 测试覆盖分类器/规则/withheld/占位/集成。✓
- §11 变更清单 8 文件 → Task 1-9 全覆盖。✓

**2. 类型一致性:**
- `_placeholder(block, reason=...) -> ToolResultBlock`:Task 3 定义,Task 7 续写消费。✓
- `chain.handle`/`handle_error` 均 async:Task 6 定义,Task 7/8/9 消费用 `await`。✓
- `ErrorRule`/`TransitionRule` protocol:Task 6 定义,Task 8 规则实现。✓
- `RecoveryChain(rules, error_rules)`:Task 6 签名,Task 7/8 填充。✓
- `StreamOutcome.withheld`:`"max_output_tokens" | None`,Task 4 产出,Task 7 match。✓

**3. 已知边界(非占位,有意为之):**
- Task 6 与 Task 7 在 `rules.py` 上衔接:`build_recovery_chain` 先双参数空 error_rules(Task 6),Task 7 加 MaxOutputTokensRule,Task 8 填 error_rules。每个 task 末尾全量测试过,无中间破损。
- Task 9 的 `test_max_tokens_with_tool_use_does_not_execute` 依赖"input 完整可解析的 tool_use + max_tokens"组合验证 withheld 优先——若该用例因 aggregate 行为微调失败,核查 Task 4 的 json.loads 正常路径未被容错误伤。
