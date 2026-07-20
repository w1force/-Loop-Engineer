# agent_state 架构重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入 `AgentState`(跨 submit 会话状态),收编散落数据(messages/skills/file_read_state/cwd/预算),`ToolContext` 持双 state,工具闭包全退场,为多输入铺路。

**Architecture:** 三层状态分离——`AgentState`(跨 submit,caller 持有,dataclass)/ `QueryState`(单 query_loop 内,原 `State` 改名,pydantic,messages 字段引用 `agent_state.messages`)/ `QueryParams`(单次配置)。messages 单一来源 `agent_state.messages`。工具从 `ctx.agent_state` 取运行时数据(闭包退场)。recovery/needs_follow_up 改原地 extend messages(不 model_copy 重建)。

**Tech Stack:** Python 3.10+、pydantic v2、pytest(asyncio_mode=auto)、dataclasses 标准库。

## Global Constraints

- pytest `asyncio_mode = auto`:测试直接 `async def test_xxx()`,不加 `@pytest.mark.mark_asyncio`;`tmp_path`/`monkeypatch` 造数据。
- pyright `typeCheckingMode = "basic"` 必须通过(`uv run pyright <changed files>`)。
- 留在 `feat/skill` 分支,每 task 末尾 commit;不提交 main。
- `AgentState` 用 `@dataclass`(非 pydantic);`QueryState`(原 `State`)仍是 pydantic `BaseModel`,**保留 `messages` 字段**(引用 `agent_state.messages`)。
- **messages 单一来源** `agent_state.messages`;`query_state.messages` 是引用别名。
- **recovery / needs_follow_up 改原地 extend** messages(`state.messages.extend(...)`),`model_copy` **不 update messages**(引用保持)。
- 工具从 `ctx.agent_state` 取(file_read_state/skills/cwd),工厂无参,**闭包退场**。
- 中文注释(对齐现有代码风格)。
- 测试模式参照 `tests/test_builtin_tools/test_read.py`:`_ctx() = ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=...)`。

## File Structure

| 文件 | 责任 | 动作 |
|------|------|------|
| `core/types.py` | `AgentState`(dataclass)+ `QueryState`(原 State 改名)+ `SkillMeta` 移入 | 改 |
| `core/tools.py` | `ToolContext(agent_state, query_state)` 双 state | 改 |
| `core/builtin_tools/read.py`/`write.py`/`glob.py`/`grep.py` | func 从 `ctx.agent_state` 取,工厂无参 | 改 |
| `core/builtin_tools/load_skill.py` | 移入(从 `core/skills/`),func 从 `ctx.agent_state.skills` 取 | 新建 |
| `core/builtin_tools/__init__.py` | `builtin_tools()` 无参,返回 5 个(含 load_skill) | 改 |
| `core/skills/loader.py` | `SkillMeta` 移走;删 `render_catalog`/`append_catalog`;留 `SkillLoader.scan` | 改 |
| `core/loop/orchestrator.py` | `query_loop(agent_state, params, tracer)`;`QueryState(messages=agent_state.messages)`;needs_follow_up 原地 extend | 改 |
| `core/loop/phases/stream_turn.py` | 签名加 `agent_state` | 改 |
| `core/loop/phases/compact.py` | 签名加 `agent_state`(桩) | 改 |
| `core/loop/recovery/rules.py` | `MaxOutputTokensRule` 原地 extend messages,model_copy 不 update messages | 改 |
| `core/agent_loop.py` | `build_agent_state` + `build_system_prompt` + `submit` 改签名;`AgentConfig` 加 `cwd` | 改 |
| `tests/*` | 全面适配(agent_state 构造、多 submit 累积) | 改 |

---

### Task 1: 数据模型基础(AgentState + State→QueryState + SkillMeta 移入 + 删 render/append)

**Files:**
- Modify: `core/types.py`
- Modify: `core/skills/loader.py`
- Modify: 全局 `import State` → `QueryState`(orchestrator/stream_turn/compact/rules/tests)
- Test: `tests/test_types.py`

**Interfaces:**
- Consumes: 无(首个 task)
- Produces:
  - `AgentState`(dataclass):`messages: list[Message]` / `skills: list[SkillMeta]` / `file_read_state: FileReadState` / `cwd: str` / `total_input_tokens: int` / `total_output_tokens: int`
  - `QueryState`(原 `State` 改名,pydantic):字段不变(`messages`/`turn_count`/...)
  - `SkillMeta`(dataclass,从 `core/skills/loader.py` 移到 `core/types.py`)

- [ ] **Step 1: types.py — State→QueryState 改名 + SkillMeta 移入 + AgentState 新增**

修改 `core/types.py`:
1. `SkillMeta` 从 `core/skills/loader.py` 移入(放 `State` 之前)。需 `from dataclasses import dataclass`(已 import)+ `from pathlib import Path`:
```python
from pathlib import Path

@dataclass(frozen=True)
class SkillMeta:
    """一个 skill 的元数据(从 core/skills/loader.py 移入,避免 types→skills 循环依赖)。"""
    name: str
    description: str
    skill_dir: Path
    skill_md: Path
```
2. `class State` → `class QueryState`(改名,字段不变):
```python
class QueryState(BaseModel):
    messages: list[Message]
    turn_count: int = 1
    max_output_tokens_recovery_count: int = 0
    max_output_tokens_override: int | None = None
    has_attempted_autocompact: bool = False
    network_retry_count: int = 0
    transition: Continue | Terminal | None = None
```
3. `AgentState` 新增(放 `QueryState` 之后):
```python
from .builtin_tools.readstate import FileReadState  # 顶部 import 段(单向,readstate 不依赖 types)

@dataclass
class AgentState:
    """跨 submit 的 agent 会话状态(caller 持有)。

    收编原本散落/闭包的数据:messages(跨 submit 累积)、skills、file_read_state、cwd、预算计数。
    tools 不存(走 QueryParams;executor 注册 + stream_turn 发 API)。
    """
    messages: list[Message] = field(default_factory=list)
    skills: list[SkillMeta] = field(default_factory=list)
    file_read_state: FileReadState = field(default_factory=FileReadState)
    cwd: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
```
(顶部 `from dataclasses import dataclass, field` 已有 `dataclass`,补 `field`。)

- [ ] **Step 2: skills/loader.py — SkillMeta 移走 + 删 render_catalog/append_catalog**

修改 `core/skills/loader.py`:
1. 删 `SkillMeta` 定义(已移到 types),改 import:`from ..types import SkillMeta`(顶部)。
2. 删 `render_catalog` 和 `append_catalog` 两个函数(逻辑移到 Task 4 的 `build_system_prompt`)。
3. 保留 `SkillLoader.scan` + `_parse_frontmatter`(`scan` 返回 `list[SkillMeta]`,从 types 取)。

结果文件只剩:`_parse_frontmatter` + `SkillLoader`(scan)。

- [ ] **Step 3: 全局 State→QueryState rename**

```bash
# 找所有引用 State(从 types import)的地方
grep -rn "from.*types import.*State\|: State\|State(" core/ tests/ | grep -v QueryState
```
逐文件改 `State` → `QueryState`(import + 类型注解 + 构造)。关键文件:
- `core/loop/orchestrator.py`:`from ..types import ... State ...` → `QueryState`;`state = State(...)` → `QueryState(...)`;类型注解
- `core/loop/phases/stream_turn.py`:`from ...types import ... State` → `QueryState`;`async def stream_turn(state: State, ...)` → `state: QueryState`
- `core/loop/phases/compact.py`:`from ...types import State` → `QueryState`;`async def maybe_compact(state: State, ...)` → `QueryState`
- `core/loop/recovery/rules.py`:`from ...types import ... State` → `QueryState`;各 `state: State` 注解
- `core/tools.py`:`ToolContext.state` 注释/类型(下个 task 改字段名,这里先 rename)
- `tests/test_types.py`/`test_orchestrator.py`/`test_stream_turn_executor.py`/`test_recovery_chain.py`:State → QueryState

- [ ] **Step 4: 写测试** `tests/test_types.py`(追加)

```python
from core.types import AgentState, QueryState, SkillMeta
from pathlib import Path


def test_agent_state_defaults():
    a = AgentState()
    assert a.messages == []
    assert a.skills == []
    assert a.total_input_tokens == 0
    assert a.total_output_tokens == 0
    assert a.cwd == ""


def test_query_state_keeps_messages():
    from core.types import UserMessage
    q = QueryState(messages=[UserMessage(content="hi")])
    assert q.turn_count == 1
    assert len(q.messages) == 1


def test_skill_meta_in_types():
    m = SkillMeta(name="x", description="d", skill_dir=Path("/x"), skill_md=Path("/x/SKILL.md"))
    assert m.name == "x"
```

- [ ] **Step 5: 跑测试 + pyright**

```bash
uv run pytest -q
uv run pyright core/types.py core/skills/loader.py
```
Expected: 全量 passed(rename 一致);pyright 0 errors。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: AgentState 引入 + State→QueryState 改名 + SkillMeta 移 types + 删 render/append"
```

---

### Task 2: ToolContext 双 state + agent_state 流转链 + messages 引用

**Files:**
- Modify: `core/tools.py`(`ToolContext`)
- Modify: `core/loop/orchestrator.py`(`query_loop` 签名 + `QueryState(messages=agent_state.messages)` + needs_follow_up 原地 extend)
- Modify: `core/loop/phases/stream_turn.py`(签名加 `agent_state`)
- Modify: `core/loop/phases/compact.py`(签名加 `agent_state`)
- Modify: `core/loop/recovery/rules.py`(`MaxOutputTokensRule` 原地 extend)
- Modify: `core/agent_loop.py`(`submit` 签名 + messages append + QueryParams 去 messages)
- Modify: `core/loop/orchestrator.py`(`QueryParams` 去 `messages`)
- Test: `tests/test_orchestrator.py`, `tests/test_agent_loop.py`

**Interfaces:**
- Consumes: `AgentState`/`QueryState`/`SkillMeta`(Task 1)
- Produces:
  - `ToolContext(tracer, abort_signal, agent_state, query_state=None)`
  - `query_loop(agent_state, params, tracer) -> AsyncIterator[...]`
  - `stream_turn(agent_state, query_state, params, tracer, executor)`
  - `maybe_compact(agent_state, query_state, params, tracer) -> QueryState`
  - `submit(prompt, agent_state, config, tracer)`
  - `QueryParams` 去掉 `messages` 字段

- [ ] **Step 1: ToolContext 双 state**(`core/tools.py`)

```python
@dataclass
class ToolContext:
    """工具执行时注入的运行时上下文。"""
    tracer: Tracer
    abort_signal: asyncio.Event
    agent_state: "AgentState"               # 跨 submit(工具取 file_read_state/skills/cwd)
    query_state: "QueryState | None" = None  # 单轮(原 state 改名)

    # 兼容:旧的 state 属性(过渡期,Task 3 工具改造后可删)
    @property
    def state(self):
        return self.query_state
```
顶部 `if TYPE_CHECKING: from .types import AgentState, QueryState`(避免循环 import)。`state` property 过渡(老代码 `ctx.state` 还能用),Task 3 后删。

- [ ] **Step 2: QueryParams 去 messages**(`core/loop/orchestrator.py`)

```python
@dataclass
class QueryParams:
    system: str | list[dict]
    model: str
    max_tokens: int
    provider: Provider
    abort_signal: asyncio.Event
    tools: list[Tool] = field(default_factory=list)
    max_turns: int = 20
    can_use_tool: Callable = default_can_use_tool
    tool_execution_mode: Literal["streaming", "batch"] = "streaming"
    # messages 字段删除 —— 归 agent_state
```

- [ ] **Step 3: query_loop 签名 + QueryState 引用 messages + ToolContext 注入**(`core/loop/orchestrator.py`)

```python
async def query_loop(
    agent_state: AgentState, params: QueryParams, tracer: Tracer
) -> AsyncIterator[Message | StreamEvent | Tombstone]:
    state = QueryState(messages=agent_state.messages, turn_count=1)  # ★ messages 引用同一 list
    chain = build_recovery_chain()
    turn_id = 0
    while True:
        turn_id += 1
        tracer.emit(TraceEvent(kind=TraceKind.TURN_START, turn=state.turn_count))
        state = await maybe_compact(agent_state, state, params, tracer)
        ctx = ToolContext(tracer=tracer, abort_signal=params.abort_signal,
                          agent_state=agent_state, query_state=state)
        executor = make_executor(params.tool_execution_mode, params.tools,
                                 params.can_use_tool, tracer, ctx)
        try:
            outcome: StreamOutcome | None = None
            async for m in stream_turn(agent_state, state, params, tracer, executor):
                if isinstance(m, StreamOutcome):
                    outcome = m
                else:
                    yield m
                    if params.abort_signal.is_set():
                        executor.discard()
                        yield Tombstone(turn_id)
                        _emit_transition(tracer, Terminal(reason=TerminalReason.USER_INTERRUPT))
                        return
            assert outcome is not None
        except ProviderError as e:
            executor.discard()
            decision = await chain.handle_error(state, e, params, tracer)
            yield Tombstone(turn_id)
            _emit_transition(tracer, decision.transition)
            if isinstance(decision.transition, Terminal):
                return
            if decision.next_state is None:
                return
            state = decision.next_state
            continue

        state.network_retry_count = 0
        state.messages.extend(outcome.assistant_msgs)   # ★ 原地 extend(= agent_state.messages)
        yield outcome.assistant_msgs[0]

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
            state.messages.extend(outcome.assistant_msgs)            # ★ 原地 extend
            state.messages.append(UserMessage(content=cast(list[ContentBlock], tool_results)))  # ★ 原地 append
            state = state.model_copy(update={                        # ★ model_copy 不 update messages
                "turn_count": state.turn_count + 1,
                "transition": Continue(reason=ContinueReason.NEXT_TURN),
            })
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
顶部 `from ..types import ... AgentState, QueryState ...`。

- [ ] **Step 4: stream_turn 签名加 agent_state**(`core/loop/phases/stream_turn.py`)

只改签名(内部继续用 `state.messages`,引用 agent_state.messages):
```python
async def stream_turn(
    agent_state,          # ★ 新增(本期内部不用,预留;messages 仍走 state.messages)
    state: QueryState,
    params: "QueryParams",
    tracer: Tracer,
    executor: "ToolExecutor | None",
):
    ...
```

- [ ] **Step 5: maybe_compact 签名加 agent_state**(`core/loop/phases/compact.py`)

```python
from ...types import QueryState

async def maybe_compact(agent_state, state: QueryState, params, tracer) -> QueryState:
    """Phase 1: 直通不压缩。Phase 5: 触发式压缩(用 agent_state.messages)。"""
    return state
```

- [ ] **Step 6: MaxOutputTokensRule 原地 extend**(`core/loop/recovery/rules.py`)

`MaxOutputTokensRule.apply` 的第二档(续写)改原地 extend + model_copy 不 update messages:
```python
        # 第二档: 注入续写消息 —— 原地 append 到 state.messages(= agent_state.messages)
        if state.max_output_tokens_recovery_count < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
            turn_assistant = outcome.assistant_msgs[0]
            state.messages.append(turn_assistant)           # ★ 原地(原 model_copy update messages)
            if outcome.tool_calls:
                placeholders = [_placeholder(tc) for tc in outcome.tool_calls]
                state.messages.append(UserMessage(content=placeholders))
            state.messages.append(UserMessage(content=_META_RESUME))
            return Decision(
                transition=Continue(reason=ContinueReason.MAX_OUTPUT_TOKENS_RECOVERY),
                next_state=state.model_copy(update={
                    "max_output_tokens_recovery_count":
                    state.max_output_tokens_recovery_count + 1,
                }),   # ★ 不 update messages(引用保持 agent_state.messages)
            )
```
(`state: QueryState` 注解;顶部 import QueryState。)

- [ ] **Step 7: submit 签名 + messages append + QueryParams 去 messages**(`core/agent_loop.py`)

```python
async def submit(
    prompt: str, agent_state: AgentState, config: AgentConfig, tracer: Tracer
) -> AsyncIterator[dict]:
    agent_state.messages.append(UserMessage(content=prompt))   # ★ 跨 submit 累积
    await record_transcript(agent_state.messages, config.transcript_path)

    # 暂用旧 system/tools/budget(Task 4 完善:build_system_prompt + builtin_tools() + budget 累积)
    read_state = agent_state.file_read_state
    tools = [*config.tools, *builtin_tools(read_state)]   # Task 3 改 builtin_tools() 无参
    params = QueryParams(
        system=config.system,
        model=config.model,
        max_tokens=config.max_tokens,
        provider=config.provider,
        abort_signal=config.abort_signal,
        tools=tools,
        max_turns=config.max_turns,
        can_use_tool=config.can_use_tool,
        tool_execution_mode=config.tool_execution_mode,
    )

    last_stop_reason: str | None = None
    total_in = total_out = 0   # Task 4 改 agent_state.total_*_tokens
    async for msg in query_loop(agent_state, params, tracer):   # ★ agent_state 传入
        if isinstance(msg, AssistantMessage):
            await record_transcript(agent_state.messages, config.transcript_path)
            last_stop_reason = msg.stop_reason
            if msg.usage:
                total_in += msg.usage.input_tokens
                total_out += msg.usage.output_tokens
        elif isinstance(msg, Tombstone):
            continue
        elif isinstance(msg, StreamEvent):
            continue
        if config.max_budget_usd is not None:
            if _rough_cost(total_in, total_out) >= config.max_budget_usd:
                yield {"type": "result", "subtype": "error_budget", "error": "budget exceeded"}
                return

    result = _last_message(agent_state.messages, ("assistant", "user"))
    if not is_result_successful(result, last_stop_reason):
        yield {"type": "result", "subtype": "error_during_execution"}
        return
    yield {
        "type": "result",
        "subtype": "success",
        "text": _extract_text(result),
        "usage": {"input_tokens": total_in, "output_tokens": total_out},
    }
```
顶部 `from .types import ... AgentState ...`。注意:`submit` 不再 append AssistantMessage(query_loop 内 `state.messages.extend` 已累积到 agent_state.messages)。

- [ ] **Step 8: 适配测试**(`tests/test_orchestrator.py`, `tests/test_agent_loop.py`)

所有 `query_loop(params, tracer)` 调用 → `query_loop(agent_state, params, tracer)`,先 `agent_state = AgentState(messages=params_messages_copy)`(测试构造)。所有 `submit(prompt, cfg, tracer)` → `submit(prompt, AgentState(), cfg, tracer)`。`QueryParams(messages=...)` 去掉 messages。

- [ ] **Step 9: 跑测试 + pyright**

```bash
uv run pytest -q
uv run pyright core/tools.py core/loop/orchestrator.py core/loop/phases/stream_turn.py core/loop/phases/compact.py core/loop/recovery/rules.py core/agent_loop.py
```
Expected: 全量 passed;pyright 0 errors。

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor: ToolContext 双 state + agent_state 流转链 + messages 引用(query_loop/stream_turn/compact/recovery/submit)"
```

---

### Task 3: 工具改造(闭包退场,从 ctx.agent_state 取)

**Files:**
- Modify: `core/builtin_tools/read.py`, `write.py`, `glob.py`, `grep.py`(工厂无参,func 从 ctx 取)
- Create: `core/builtin_tools/load_skill.py`(从 `core/skills/load_skill.py` 移入,func 从 ctx 取)
- Delete: `core/skills/load_skill.py`(移走)
- Modify: `core/builtin_tools/__init__.py`(`builtin_tools()` 无参,含 load_skill)
- Modify: `core/agent_loop.py`(submit 的 `builtin_tools()` 调用)
- Test: `tests/test_builtin_tools/`(全适配)

**Interfaces:**
- Consumes: `ToolContext.agent_state`(Task 2)
- Produces:
  - `read_tool() -> Tool`(无参;func 内 `ctx.agent_state.file_read_state` + `ctx.agent_state.cwd`)
  - `write_tool() / glob_tool() / grep_tool() -> Tool`(无参)
  - `load_skill_tool`(模块级常量;func 内 `ctx.agent_state.skills`)
  - `builtin_tools() -> list[Tool]`(无参,返回 5 个)

- [ ] **Step 1: read_tool 无参 + func 从 ctx 取**(`core/builtin_tools/read.py`)

```python
def read_tool() -> Tool:
    async def _read(inp: ReadIn, ctx: ToolContext) -> str:
        read_state = ctx.agent_state.file_read_state   # ★ 从 ctx 取(原闭包)
        path = Path(inp.file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {inp.file_path}")
        if path.suffix.lower() in _BINARY_EXTS:
            raise ValueError(f"Cannot read binary file ({path.suffix}). Use a different tool.")
        if _is_blocked_device(str(path)):
            raise ValueError(f"Cannot read '{inp.file_path}': device file would block or produce infinite output.")
        disk_mtime = path.stat().st_mtime
        if read_state.is_unchanged(str(path), inp.offset, inp.limit, disk_mtime):
            return "File unchanged"
        all_lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
        if all_lines and all_lines[-1] == "":
            all_lines = all_lines[:-1]
        total = len(all_lines)
        if total == 0:
            read_state.set(str(path), "", disk_mtime, inp.offset, inp.limit)
            return "<File is empty>"
        start_idx = inp.offset - 1
        if start_idx >= total:
            read_state.set(str(path), "", disk_mtime, inp.offset, inp.limit)
            return f"<File has {total} line(s); offset {inp.offset} out of range.>"
        end_idx = total if inp.limit is None else min(start_idx + inp.limit, total)
        selected = all_lines[start_idx:end_idx]
        kept: list[str] = []
        size = 0
        for ln in selected:
            if size + len(ln) > MAX_READ_BYTES:
                break
            kept.append(ln)
            size += len(ln) + 1
        truncated_bytes = len(kept) < len(selected)
        read_state.set(str(path), "\n".join(kept), disk_mtime, inp.offset, inp.limit)
        out = _add_line_numbers(kept, inp.offset, total)
        if truncated_bytes:
            out += f"\n<Read truncated at {MAX_READ_BYTES} bytes; use offset/limit for more.>"
        return out
    return Tool(name="read", description=_DESCRIPTION, input_model=ReadIn, func=_read, is_concurrency_safe=True)
```

- [ ] **Step 2: write_tool / glob_tool / grep_tool 无参 + func 从 ctx 取**

同 read 模式。各文件:
- `write_tool()`:`func` 内 `read_state = ctx.agent_state.file_read_state`(原逻辑不变)
- `glob_tool()`:`func` 内 `cwd = ctx.agent_state.cwd`(原逻辑用 cwd)
- `grep_tool()`:`func` 内 `cwd = ctx.agent_state.cwd`(原逻辑用 cwd)

各工厂去掉 `(read_state, cwd)` / `(cwd)` 参数,改成无参 `()`。

- [ ] **Step 3: load_skill 移 core/builtin_tools/load_skill.py + func 从 ctx 取**

创建 `core/builtin_tools/load_skill.py`:
```python
"""load_skill 工具:按需加载 SKILL.md 全文(从 ctx.agent_state.skills 动态取,不闭包)。"""
from __future__ import annotations

from pydantic import BaseModel

from ..tools import Tool, ToolContext
from ..types import SkillMeta


class LoadSkillInput(BaseModel):
    name: str


async def _load(inp: LoadSkillInput, ctx: ToolContext) -> str:
    skills: list[SkillMeta] = ctx.agent_state.skills   # ★ 从 ctx 动态取
    index = {m.name: m for m in skills}
    meta = index.get(inp.name)
    if meta is None:
        return f"Error: skill '{inp.name}' not found. Available: {sorted(index)}"
    try:
        return meta.skill_md.read_text(encoding="utf-8")
    except OSError as e:
        return f"Error: cannot read skill '{inp.name}': {e}"


load_skill_tool = Tool(
    name="load_skill",
    description="加载指定 skill 的完整指令。先看 <skills> 目录决定用哪个 skill,再调用此工具。",
    input_model=LoadSkillInput,
    func=_load,
    is_concurrency_safe=True,
)
```
删除 `core/skills/load_skill.py`(移走)。

- [ ] **Step 4: builtin_tools() 无参含 load_skill**(`core/builtin_tools/__init__.py`)

```python
"""builtin 工具集(read/write/glob/grep/load_skill)。全部无状态:func 从 ctx.agent_state 取。"""
from __future__ import annotations

from ..tools import Tool
from .glob import glob_tool
from .grep import grep_tool
from .load_skill import load_skill_tool
from .read import read_tool
from .write import write_tool

__all__ = ["builtin_tools"]


def builtin_tools() -> list[Tool]:
    """产出 5 个 builtin Tool(无参;func 从 ctx.agent_state 取运行时数据)。"""
    return [glob_tool(), grep_tool(), read_tool(), write_tool(), load_skill_tool]
```
(去掉 `FileReadState` 导出/参数。)

- [ ] **Step 5: submit 调用点改 builtin_tools()**(`core/agent_loop.py`)

```python
    tools = [*config.tools, *builtin_tools()]   # 无参(Task 2 的旧 read_state 调用改掉)
```
去掉 `read_state = agent_state.file_read_state`(不再需要)。

- [ ] **Step 6: 工具测试适配**(`tests/test_builtin_tools/`)

每个工具测试:构造 `agent_state`(含 file_read_state/cwd/skills)+ `ToolContext(..., agent_state=agent_state)`,调 `read_tool().func(inp, ctx)`(无参工厂)。例:
```python
import asyncio
from core.agent_loop import AgentConfig  # 或直接构造 AgentState
from core.builtin_tools.read import ReadIn, read_tool
from core.types import AgentState
from core.tools import ToolContext
from telemetry.tracer import NoopTracer

def _ctx(agent_state):
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event(), agent_state=agent_state)

async def test_read_adds_line_numbers(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("line1\nline2\n")
    agent_state = AgentState(cwd=str(tmp_path))
    result = await read_tool().func(ReadIn(file_path=str(f)), _ctx(agent_state))
    assert "line1" in result and "line2" in result
```
load_skill 测试:`agent_state = AgentState(skills=[...])`(构造 SkillMeta 或 scan)。

- [ ] **Step 7: 跑测试 + pyright**

```bash
uv run pytest -q
uv run pyright core/builtin_tools/
```
Expected: 全量 passed;pyright 0 errors。

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor: builtin 工具闭包退场(从 ctx.agent_state 取)+ load_skill 移 builtin_tools"
```

---

### Task 4: build_agent_state + build_system_prompt + submit 完善 + AgentConfig.cwd

**Files:**
- Modify: `core/agent_loop.py`(`build_agent_state` + `build_system_prompt` + `submit` 完善 + `AgentConfig.cwd`)
- Test: `tests/test_agent_loop_skill.py`(重写为 build_agent_state/build_system_prompt 测试)

**Interfaces:**
- Consumes: `SkillLoader.scan`(Task 1 loader)、`builtin_tools()`(Task 3)
- Produces:
  - `build_agent_state(config: AgentConfig) -> AgentState`
  - `build_system_prompt(agent_state: AgentState, config: AgentConfig) -> str | list[dict]`
  - `AgentConfig.cwd: str`(default `os.getcwd()`)
  - `submit` 完善:用 build_system_prompt + budget 累积 agent_state

- [ ] **Step 1: AgentConfig 加 cwd**(`core/agent_loop.py`)

```python
import os
@dataclass
class AgentConfig:
    provider: Provider
    system: str | list[dict]
    model: str
    max_tokens: int
    abort_signal: asyncio.Event = field(default_factory=asyncio.Event)
    max_turns: int = 20
    initial_messages: list[Message] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)
    can_use_tool: Callable = default_can_use_tool
    max_budget_usd: float | None = None
    transcript_path: str = "transcript.jsonl"
    tool_execution_mode: Literal["streaming", "batch"] = "streaming"
    skill_dirs: list[str] = field(default_factory=lambda: ["skills/"])
    cwd: str = field(default_factory=os.getcwd)   # ★ 新增
```

- [ ] **Step 2: build_agent_state 工厂**(`core/agent_loop.py`)

```python
import logging
from .skills.loader import SkillLoader
logger = logging.getLogger(__name__)

def build_agent_state(config: AgentConfig) -> AgentState:
    """调用者初始化 agent_state:scan skills + 新建 file_read_state + 设 cwd。"""
    try:
        skills = SkillLoader.scan(config.skill_dirs)
    except Exception as e:
        logger.warning("skill scan failed: %s", e)
        skills = []
    return AgentState(
        messages=[],
        skills=skills,
        file_read_state=FileReadState(),
        cwd=config.cwd,
    )
```

- [ ] **Step 3: build_system_prompt 单独函数**(`core/agent_loop.py`)

```python
def build_system_prompt(agent_state: AgentState, config: AgentConfig) -> str | list[dict]:
    """生成最终 system:config.system + skill 目录(从 agent_state.skills)。"""
    skills = agent_state.skills
    if not skills:
        return config.system
    lines = ["", "", "<skills>"]
    for m in skills:
        desc = " ".join(m.description.split())
        lines.append(f"- name: {m.name}")
        lines.append(f"  description: {desc}")
    lines.append("</skills>")
    lines.append("")
    lines.append("当用户请求匹配某个 skill 时,调用 load_skill(name) 加载完整指令后再执行。")
    catalog = "\n".join(lines)
    if isinstance(config.system, str):
        return config.system + catalog
    return [*config.system, {"type": "text", "text": catalog}]
```

- [ ] **Step 4: submit 完善(用 build_system_prompt + budget 累积 agent_state)**(`core/agent_loop.py`)

```python
async def submit(prompt, agent_state, config, tracer):
    agent_state.messages.append(UserMessage(content=prompt))
    await record_transcript(agent_state.messages, config.transcript_path)

    system = build_system_prompt(agent_state, config)          # ★ skill 目录
    tools = [*config.tools, *builtin_tools()]                  # 无参含 load_skill
    params = QueryParams(
        system=system, model=config.model, max_tokens=config.max_tokens,
        provider=config.provider, abort_signal=config.abort_signal,
        tools=tools, max_turns=config.max_turns,
        can_use_tool=config.can_use_tool, tool_execution_mode=config.tool_execution_mode,
    )

    last_stop_reason = None
    async for msg in query_loop(agent_state, params, tracer):
        if isinstance(msg, AssistantMessage):
            await record_transcript(agent_state.messages, config.transcript_path)
            last_stop_reason = msg.stop_reason
            if msg.usage:
                agent_state.total_input_tokens += msg.usage.input_tokens    # ★ 累积 agent_state
                agent_state.total_output_tokens += msg.usage.output_tokens
        elif isinstance(msg, Tombstone):
            continue
        elif isinstance(msg, StreamEvent):
            continue
        if config.max_budget_usd is not None and _rough_cost(
            agent_state.total_input_tokens, agent_state.total_output_tokens) >= config.max_budget_usd:
            yield {"type": "result", "subtype": "error_budget", "error": "budget exceeded"}
            return

    result = _last_message(agent_state.messages, ("assistant", "user"))
    if not is_result_successful(result, last_stop_reason):
        yield {"type": "result", "subtype": "error_during_execution"}
        return
    yield {
        "type": "result", "subtype": "success",
        "text": _extract_text(result),
        "usage": {"input_tokens": agent_state.total_input_tokens,
                  "output_tokens": agent_state.total_output_tokens},
    }
```

- [ ] **Step 5: 写测试** `tests/test_agent_loop_skill.py`(重写)

```python
"""build_agent_state + build_system_prompt 测试。"""
from core.agent_loop import AgentConfig, build_agent_state, build_system_prompt
from core.types import AgentState, SkillMeta
from pathlib import Path


def _cfg(**kw):
    base = dict(provider=None, system="base", model="m", max_tokens=100)
    base.update(kw)
    return AgentConfig(**base)


def test_build_agent_state_scans_skills(tmp_path):
    skills = tmp_path / "skills"
    d = skills / "foo"; d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: foo\n---\n# foo\n", encoding="utf-8")
    cfg = _cfg(skill_dirs=[str(skills)], cwd=str(tmp_path))
    astate = build_agent_state(cfg)
    assert len(astate.skills) == 1 and astate.skills[0].name == "foo"
    assert astate.cwd == str(tmp_path)
    assert astate.messages == []


def test_build_agent_state_scan_failure_degrades(tmp_path):
    cfg = _cfg(skill_dirs=[str(tmp_path / "nope")])
    astate = build_agent_state(cfg)
    assert astate.skills == []   # 降级


def test_build_system_prompt_empty_skills():
    astate = AgentState(skills=[])
    assert build_system_prompt(astate, _cfg(system="base")) == "base"


def test_build_system_prompt_str():
    m = SkillMeta(name="foo", description="d", skill_dir=Path("/x"), skill_md=Path("/x/SKILL.md"))
    astate = AgentState(skills=[m])
    out = build_system_prompt(astate, _cfg(system="base"))
    assert isinstance(out, str) and out.startswith("base") and "<skills>" in out and "foo" in out


def test_build_system_prompt_list():
    m = SkillMeta(name="foo", description="d", skill_dir=Path("/x"), skill_md=Path("/x/SKILL.md"))
    astate = AgentState(skills=[m])
    out = build_system_prompt(astate, _cfg(system=[{"type": "text", "text": "a"}]))
    assert isinstance(out, list) and out[0] == {"type": "text", "text": "a"}
    assert "<skills>" in out[-1]["text"]
```

- [ ] **Step 6: 跑测试 + pyright**

```bash
uv run pytest -q
uv run pyright core/agent_loop.py
```
Expected: 全量 passed;pyright 0 errors。

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: build_agent_state 工厂 + build_system_prompt + submit 完善(budget 累积 + AgentConfig.cwd)"
```

---

### Task 5: 多 submit 累积端到端 + 全量回归 + 清理

**Files:**
- Modify: `tests/test_agent_loop.py`(多 submit 累积测试)
- Modify: `core/tools.py`(删 Task 2 的过渡 `state` property)
- Modify: `main.py` / demo 入口(适配 submit 新签名,若需要)
- Test: 全量回归

**Interfaces:**
- Consumes: 全部前序 task
- Produces: 多 submit 端到端验证 + 清理

- [ ] **Step 1: 多 submit 累积测试**(`tests/test_agent_loop.py` 追加)

mock `query_loop`(直接 yield AssistantMessage,绕开 provider 事件流协议),聚焦验证 submit 层的 messages/budget 跨 submit 累积:

```python
async def test_submit_accumulates_across_submits(monkeypatch, tmp_path):
    """同一 agent_state 跨两次 submit:messages 累积 + budget 累积。"""
    import core.agent_loop as al
    from core.agent_loop import AgentConfig, build_agent_state, submit
    from core.types import AssistantMessage, TextBlock, Usage
    from telemetry.tracer import NoopTracer

    async def _fake_query_loop(agent_state, params, tracer):
        # 直接 yield 一条 AssistantMessage(带 usage),不依赖 provider/aggregate_stream 协议
        yield AssistantMessage(
            content=[TextBlock(text="reply")],
            usage=Usage(input_tokens=10, output_tokens=5),
            stop_reason="end_turn",
        )
    monkeypatch.setattr(al, "query_loop", _fake_query_loop)

    cfg = AgentConfig(provider=None, system="base", model="m",
                      max_tokens=100, transcript_path=str(tmp_path / "t.jsonl"))
    astate = build_agent_state(cfg)
    tracer = NoopTracer()

    r1 = [r async for r in submit("hi1", astate, cfg, tracer)]
    r2 = [r async for r in submit("hi2", astate, cfg, tracer)]

    # messages 累积:user1 + assistant1 + user2 + assistant2 = 4
    assert len(astate.messages) == 4
    # budget 累积(两次 submit 各 10 input + 5 output)
    assert astate.total_input_tokens == 20
    assert astate.total_output_tokens == 10
    assert r1[-1]["subtype"] == "success" and r2[-1]["subtype"] == "success"
```

- [ ] **Step 2: 删 ToolContext 过渡 state property**(`core/tools.py`)

Task 2 加的 `state` property(过渡)删除——Task 3 后工具全用 `ctx.agent_state`,无 `ctx.state` 引用:
```python
@dataclass
class ToolContext:
    tracer: Tracer
    abort_signal: asyncio.Event
    agent_state: "AgentState"
    query_state: "QueryState | None" = None
    # state property 删除
```

- [ ] **Step 3: 适配 demo 入口**(`main.py`)

`main.py` 的 `submit(prompt, cfg, tracer)` 调用改为:
```python
from core.agent_loop import build_agent_state
astate = build_agent_state(cfg)
async for r in submit(prompt, astate, cfg, tracer):
    ...
```

- [ ] **Step 4: 全量回归 + pyright**

```bash
uv run pytest -q
uv run pyright core/ tests/
```
Expected: 全量 passed;pyright 0 errors(忽略 pre-existing test_types.py 的 streaming 残留 errors,若仍在)。

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: 多 submit 累积端到端 + 清理 ToolContext 过渡 state"
```

---

## Self-Review

**1. Spec 覆盖:**
- AgentState 引入 + 字段 → Task 1 ✓
- State→QueryState 改名 + 保留 messages 引用 → Task 1/2 ✓
- ToolContext 双 state → Task 2 ✓
- 工具闭包退场(read/write/glob/grep/load_skill 从 ctx 取)→ Task 3 ✓
- load_skill 移 builtin_tools → Task 3 ✓
- SkillMeta 移 types → Task 1 ✓
- 删 render_catalog/append_catalog → Task 1(load)+ Task 4(build_system_prompt 内联)✓
- build_agent_state 工厂 → Task 4 ✓
- build_system_prompt 单独函数 → Task 4 ✓
- submit 改签名 + messages/budget 累积 → Task 2(签名/messages)+ Task 4(budget/system)✓
- AgentConfig.cwd → Task 4 ✓
- QueryParams 瘦身(去 messages,留 tools)→ Task 2 ✓
- query_loop(agent_state, params, tracer)→ Task 2 ✓
- stream_turn/maybe_compact 签名 → Task 2 ✓
- recovery model_copy 不重建 messages(MaxOutputTokensRule + needs_follow_up)→ Task 2 ✓
- 多 submit 累积测试 → Task 5 ✓

**2. 占位符扫描:** 无 TBD/TODO;Task 5 Step 1 的 mock provider 注释说明协议匹配策略(非占位)。✓

**3. 类型一致性:**
- `AgentState` 字段跨 task 一致(messages/skills/file_read_state/cwd/total_*_tokens)✓
- `QueryState`(原 State)字段一致 ✓
- `query_loop(agent_state, params, tracer)` / `stream_turn(agent_state, query_state, params, tracer, executor)` / `maybe_compact(agent_state, query_state, params, tracer)` 签名一致 ✓
- `submit(prompt, agent_state, config, tracer)` 一致 ✓
- `builtin_tools()` 无参 → `read_tool()`/`write_tool()`/`glob_tool()`/`grep_tool()` 无参 ✓
- `ToolContext(tracer, abort_signal, agent_state, query_state=None)` 一致 ✓

**注**:Task 5 Step 1 的 mock provider stream 协议若与 `aggregate_stream` 不完全匹配,实现时简化为 mock `query_loop`(直接 yield AssistantMessage)验证 messages 累积——这是测试策略弹性,非占位。
