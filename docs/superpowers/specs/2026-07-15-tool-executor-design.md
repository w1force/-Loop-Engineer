# ToolExecutor 设计：工具执行器

- 日期：2026-07-15
- 状态：待审阅
- 范围：把 `run_tools` 的 Phase 1 桩替换为统一的 `ToolExecutor` 抽象（`StreamingToolExecutor` 为主 + `BatchToolExecutor` 薄子类）
- 参考范式：`/Users/liweitian.7/CtData/code/ts/Claude-Code/notes/tool-execute.md`（md §4 StreamingToolExecutor、§5 runTools、§6 顺序保持）

---

## 1. 背景与目标

`core/tools.py` 的 `run_tools` 当前是 Phase 1 桩，抛 `NotImplementedError`。本设计把它替换为一个统一的工具执行器抽象，要求：

1. **机会主义执行**：流式期间 tool_use 一到就尝试启动（与 LLM 吐 token 重叠，省 wall-clock）。
2. **并发 + 保序**：只读工具可并行、写工具独占；产出严格按 LLM 的 tool_use 顺序回灌。
3. **收尾保证**：一次 `stream_turn` 完整回复结束后，本轮所有 tool 必执行完并回灌。
4. **两种模式同抽象**：Streaming（机会主义）与 Batch（攒批 partition）共用基类，继承区分。

## 2. 范围

**做**：
- `core/tool_executor/` 包：`ToolExecutor` 基类 + `StreamingToolExecutor` + `BatchToolExecutor` + `TrackedTool` + `make_executor`。
- `stream_turn` 的 `aggregate_stream` 改 block 级固化产出。
- `query_loop` 接线：创建/传递/收尾 executor，回灌内联。
- `Tool.is_concurrency_safe` 字段；`run_tools` 废弃。
- 模式开关 `AgentConfig.tool_execution_mode` / `QueryParams.tool_execution_mode`。
- executor 单测 + 受影响现有测试改写。

**不做（YAGNI，留接口）**：
- 增量 progress yield（项目无 UI 消费者，`agent_loop` 明确忽略 `StreamEvent`）。
- 单工具超时（`Tool` 无 timeout 字段，由 `func` 自行管理，如 httpx 超时）。
- Bash `siblingAbort` 级联（本项目无 Bash 概念，工具彼此独立失败）。
- `StreamingToolExecutor` 的 `completed`/`yielded` 缓冲分离（那是增量 yield 才需要的保序机制；本设计一次性交付，保序退化为"全完成后按序取"）。

## 3. 现状与约束

- `run_tools` 桩签名：`run_tools(tool_calls, tools, can_use_tool, tracer) -> list[ToolResultBlock]`。
- `stream_turn` 的 `aggregate_stream` 在 `message_stop` 才固化**整轮**一条 `AssistantMessage`（`stream_turn.py:92-93`）——要机会主义执行必须改 block 级。
- `execute_tools_phase`（`core/loop/phases/execute_tools.py`）当前调 `run_tools` + 回灌。
- `StreamOutcome` 是 `BaseModel`，含 `assistant_msgs/tool_calls/needs_follow_up/stop_reason/withheld/yielded`。
- `Tool`（`core/tools.py`）：`name/description/input_model/func`，`func: Callable[[BaseModel], Awaitable[str|dict]]`，有 `to_schema()`。
- `can_use_tool` 返回 `CanUseDecision(allow, reason)`；`default_can_use_tool` 放行。
- telemetry 已定义 `TOOL_EXEC_START`/`TOOL_EXEC_END`（未用）；`Tracer.emit` fire-and-forget、永不抛错。
- `QueryParams.abort_signal: asyncio.Event`；`orchestrator` 现在在 `stream_turn` 之后检查它。
- 测试设施：pytest + pytest-asyncio（`asyncio_mode=auto`），`tests/` 下已有 11 个测试文件。

## 4. 架构与组件（§1）

### 4.1 包结构 `core/tool_executor/`

```
core/tool_executor/
  __init__.py     # 导出 ToolExecutor/StreamingToolExecutor/BatchToolExecutor + make_executor
  base.py         # ToolExecutor(ABC) + TrackedTool
  streaming.py    # StreamingToolExecutor
  batch.py        # BatchToolExecutor
```

### 4.2 `TrackedTool`（base.py）——一次 tool_use 的执行档案

| 字段 | 类型 | 作用 |
|---|---|---|
| `block` | `ToolUseBlock` | 这个 tool_use 的输入（`id`/`name`/`input`），来自 LLM |
| `status` | `Literal["queued","executing","completed"]` | 状态机，并发判定与收尾依据 |
| `result` | `ToolResultBlock \| None` | 执行产出 |
| `task` | `asyncio.Task \| None` | 后台任务句柄（Streaming 收尾/取消用） |

不存 `assistant_msg`——结果靠 `block.id`(=tool_use_id) 关联，无需消息引用（精简自 md §4.1）。

### 4.3 `ToolExecutor`（base.py，Template Method）

```python
class ToolExecutor(ABC):
    def __init__(self, can_use_tool, tracer: Tracer, ctx: ToolContext,
                 tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        self._can_use_tool = can_use_tool
        self._tracer = tracer
        self._ctx = ctx                          # 传给 tool.func(见 §4.7)
        self._tracked: list[TrackedTool] = []   # 保序收集
        self._discarded = False
        for t in (tools or []):
            self.register_tool(t)

    def register_tool(self, tool: Tool) -> None:
        """注册可执行工具(含 func)。测试注册 mock tool;未来注册 MCP 工具(见 §4.9)。"""
        self._tools[tool.name] = tool

    def add_tool(self, block: ToolUseBlock) -> None:
        """基类:入队(保序) + _on_add 钩子。流式期间由 stream_turn 调用。"""
        if self._discarded: return
        tracked = TrackedTool(block)
        self._tracked.append(tracked)
        if block.name not in self._tools:              # 未知工具:直接 error,不参与并发调度(对齐 md §4.3)
            tracked.result = ToolResultBlock(tool_use_id=block.id,
                                             content=f"未知工具: {block.name}", is_error=True)
            tracked.status = "completed"
            return
        self._on_add(tracked)

    @abstractmethod
    def _on_add(self, tracked: TrackedTool) -> None: ...      # 子类:是否立即调度
    @abstractmethod
    async def _run_all(self) -> None: ...                     # 子类:执行驱动

    async def get_results(self) -> list[ToolResultBlock]:
        """基类模板:await _run_all() → 按 _tracked 顺序取 result(保序)。"""
        await self._run_all()
        return [t.result for t in self._tracked]

    async def _execute_single(self, tracked: TrackedTool) -> None:
        """共享:权限→校验→func→异常 is_error→埋点(见 §6.1)"""

    def discard(self) -> None:
        """abort 清理:取消 executing task + 标记 discarded。"""
        self._discarded = True
        for t in self._tracked:
            if t.task and not t.task.done(): t.task.cancel()
```

`get_results` 契约：返回长度 == `add_tool` 次数，按序，每个 `result` 非 None（成功或 is_error 都算已交付）。

### 4.4 `StreamingToolExecutor`（streaming.py）——机会主义

- `_on_add` → `_try_schedule()`（来一个立即尝试）。
- `_can_execute(t)`（照 md §4.2）：当前无 executing 则可跑；否则仅当 `t` 安全且当前所有 executing 也安全才可跑；非安全工具有人在跑就等。
- `_try_schedule`：按 `_tracked` 顺序遍历 queued，能跑就 `asyncio.create_task(self._run(t))`（不 await，fire-and-forget）；遇到跑不了的非安全工具 `break`（保序：不让他后面的插队）。
- `_run(t)`：`await self._execute_single(t)`，`finally: self._try_schedule()`（完成回调再扫，事件驱动）。
- `_run_all()`（收尾）：`_try_schedule()` 后 `await` 所有未完成 task；对流式期间因并发限制没启动的，由 `_try_schedule` 在此陆续启动。

### 4.5 `BatchToolExecutor`（batch.py）——攒批 partition

- `_on_add`：noop（只收集）。
- `_run_all()`：`_partition()` 切批 → 每批 `asyncio.gather(*[_execute_single(t) for t in batch])`，批间顺序 await（批内并发、批间串行天然保序）。
- `_partition()`（照 md §5）：连续 `is_concurrency_safe=True` 的合一批，非安全的单独一批（reduce 保序，不 sort）。

### 4.6 `Tool.is_concurrency_safe` + 模式开关

- `core/tools.py` 的 `Tool` 加 `is_concurrency_safe: bool = False`（只读工具置 True，写工具默认 False）。
- `AgentConfig` 加 `tool_execution_mode: Literal["streaming","batch"] = "streaming"`，`submit` 透传到 `QueryParams.tool_execution_mode`。
- `make_executor(mode, tools, can_use_tool, tracer, ctx) -> ToolExecutor` 放 `core/tool_executor/__init__.py`。

### 4.7 `ToolContext`（运行时上下文注入，扩展性预留）

为支持未来 tool 函数按需获取 LLM 参数之外的运行时上下文（agent 状态、服务、配置等），且不同 tool 取不同字段，引入统一 `ToolContext` 容器（对齐 md 的 `ToolUseContext`）:

```python
@dataclass
class ToolContext:
    """工具执行时注入的运行时上下文(LLM 参数之外)。各 tool 按需读取。"""
    tracer: Tracer
    abort_signal: asyncio.Event
    state: State | None = None      # 预留:当前 agent 状态
    # 未来按需扩展: services / httpx client / config / messages(加字段不破坏旧 tool)
```

- `Tool.func` 签名：`Callable[[BaseModel], Awaitable[str|dict]]` → `Callable[[BaseModel, ToolContext], Awaitable[str|dict]]`。
- executor 创建时持有 `ctx`，`_execute_single` 调 `await tool.func(validated, self._ctx)`。
- `query_loop` 每轮构造 `ctx = ToolContext(tracer=tracer, abort_signal=params.abort_signal, state=state)` 传入 `make_executor`。
- "不同 tool 不同需求"靠"统一 ctx + 各取所需"解决，签名稳定为 `(input, ctx)`，未来加字段零破坏。当前无真实 tool（虚假工具是 dict 透传），改签名零波及。

### 4.8 校验与钩子（两层）

**第一层·参数结构校验（已实现）**：`_execute_single` 用 `tool.input_model.model_validate(block.input)` 做 pydantic 校验。LLM 产的参数不符合 schema → `ValidationError` → `is_error`（content=校验错误）。无需额外代码，pydantic 自动完成。

**第二层·执行前语义校验钩子（预留，本 spec 不实现具体逻辑）**：`Tool` 加可选 `pre_execute: Callable[[BaseModel, ToolContext], Awaitable[None]] | None = None`。`_execute_single` 在 `func` 前 `await tool.pre_execute(validated, ctx)`（若存在）；钩子抛异常 → `is_error`，留给未来"危险命令检查"等语义校验。默认 `None`（放行）。本 spec 只预留字段与调用点，不实现任何具体 `pre_execute` 逻辑。

### 4.9 工具注册（`register_tool`）

executor 提供显式注册接口（见 §4.3 `register_tool`），而非仅构造时传 tools 列表。用途：

- **测试**：在测试文件里声明 mock `Tool`（带假 `func`），`new executor + register_tool(mock)` 后跑，虚假工具不污染主代码。
- **未来 MCP**：MCP 工具适配为 `Tool` 对象后，动态 `register_tool` 注册。

**工具来源统一为 `Tool` 对象**：`QueryParams.tools` / `AgentConfig.tools` 从 `list[ToolDef]`(dict) 改为 `list[Tool]`；`to_anthropic_tools` 用 `t.to_schema()` 转 dict 发 LLM（已支持 `Tool` 对象）。executor 只注册 `Tool`（含 `func`）；裸 dict 不进 `_tools`，执行时按"未知工具" `is_error`。调试入口（如 `main-lwt.py`）的虚假工具需相应改为 `Tool` 对象（`func` 返回假数据或抛错）。

## 5. 数据流（§2，方案 W：executor 由 query_loop 管控，不进 outcome）

### 5.1 `query_loop` 接线（orchestrator.py，每轮 while）

```python
ctx = ToolContext(tracer=tracer, abort_signal=params.abort_signal, state=state)
executor = make_executor(params.tool_execution_mode, params.tools,
                         params.can_use_tool, tracer, ctx)        # ① 每轮新建
outcome = await stream_turn(state, params, tracer, executor)     # ② 流式 + tool_use→add_tool
print("stream outcome: " + outcome.model_dump_json(ensure_ascii=False))
for m in outcome.yielded: yield m
if params.abort_signal.is_set():
    executor.discard()                                            # ③ 清理 stream_turn 已启动的 task
    _emit_transition(tracer, Terminal(reason=TerminalReason.ABORTED)); return
if outcome.needs_follow_up:
    tool_results = await executor.get_results()                   # ④ 收尾,保证全跑完
    base = state.model_dump()                                     # ⑤ 回灌内联(无 execute_tools_phase)
    base["messages"] = (state.messages + outcome.assistant_msgs
                        + [UserMessage(content=cast(list[ContentBlock], tool_results))])
    base["turn_count"] = state.turn_count + 1
    base["transition"] = Continue(reason=ContinueReason.NEXT_TURN)
    state = State(**base)
    if state.turn_count > params.max_turns: _emit_transition(...MAX_TURNS); return
    _emit_transition(tracer, state.transition); continue
# 无 tool_use:责任链
decision = chain.handle(state, outcome, params, tracer); ...
```

executor 在 `query_loop` 创建/传递/收尾/清理，`stream_turn` 和回灌都不 owns 它。

### 5.2 `aggregate_stream` block 级改造（stream_turn.py）

当前在 `message_stop` 组装整轮。改为：**每个 `content_block_stop` 固化该 block 就 yield 一条 block 级 `AssistantMessage(content=[block])`**；`message_stop` 不再组装整轮（仅保留 `STREAM_END` 埋点）。tool input 的 `json.loads` 仍在 `content_block_stop` 完成——喂给执行器的永远是拼好的 block，不是分片（守 md §1.2 红线）。

### 5.3 `stream_turn` 接线（加 `executor` 参数）

```python
async def stream_turn(state, params, tracer, executor) -> StreamOutcome:
    all_blocks, tool_calls, needs_follow_up = [], [], False
    stop_reason, usage = None, Usage()
    yielded = []
    async for item in aggregate_stream(events, tracer):
        yielded.append(item)
        if isinstance(item, StreamEvent):
            if item.type == "message_delta":           # 取 usage/stop_reason
                d = item.delta or {}
                if "stop_reason" in d: stop_reason = d["stop_reason"]
                if item.message and "usage" in item.message:
                    usage = Usage(**item.message["usage"])
        elif isinstance(item, AssistantMessage):        # block 级
            block = item.content[0]
            all_blocks.append(block)
            if isinstance(block, ToolUseBlock):
                executor.add_tool(block)                # ★ 机会主义启动
                tool_calls.append(block); needs_follow_up = True
    整轮 = AssistantMessage(content=all_blocks, usage=usage, stop_reason=stop_reason)
    yielded.append(整轮)                                 # ★ block 级不进 yielded,只追加整轮
    return StreamOutcome(assistant_msgs=[整轮], tool_calls=tool_calls,
                         needs_follow_up=needs_follow_up, stop_reason=stop_reason,
                         withheld=None, yielded=yielded)
```

**关键**：block 级 `AssistantMessage` 只在 `stream_turn` 内部用于喂 executor + 累积整轮，**绝不进 `yielded`**；`yielded` 只透传 `StreamEvent` + 末尾一条整轮，保证 `agent_loop` 记录的历史仍是"一轮一条"。

### 5.4 `execute_tools_phase` 去除

删除 `core/loop/phases/execute_tools.py`，回灌逻辑内联到 `query_loop`（§5.1 ⑤）。executor 不碰 `State`/`transition`，保持"tool_use→tool_result"单一职责、可独立单测。

### 5.5 `StreamOutcome` 不变

executor 不进 outcome，`StreamOutcome` 保持 `BaseModel`、字段不变；`orchestrator.py:62` 的 `model_dump_json()` 照常可用（无需改 dataclass、无需改 dump 子集——这是方案 W 的连带红利）。

## 6. 错误处理 / 权限 / 收尾 / abort（§3）

### 6.1 `_execute_single` 六路径（失败一律 `is_error`，绝不中断整批）

```python
async def _execute_single(self, tracked):
    block = tracked.block; tracked.status = "executing"
    emit(TOOL_EXEC_START, {"tool_name": block.name, "tool_use_id": block.id})
    try:
        tool = self._tools.get(block.name)
        if tool is None: raise ToolError(f"未知工具: {block.name}")  # 防御兜底(正常在 add_tool 已处理)
        if not (await self._can_use_tool(block)).allow:
            result = ToolResultBlock(tool_use_id=block.id, content=reason or "权限拒绝", is_error=True)
        else:
            validated = tool.input_model.model_validate(block.input)   # 第一层:结构校验
            if tool.pre_execute:                                        # 第二层:语义钩子(预留,见 §4.8)
                await tool.pre_execute(validated, self._ctx)
            ret = await tool.func(validated, self._ctx)
            result = _to_result(block.id, ret)                          # str→content=str; dict→content=[dict]
    except ValidationError as e: result = ToolResultBlock(block.id, content=f"参数校验失败: {e}", is_error=True)
    except Exception as e:         result = ToolResultBlock(block.id, content=f"工具执行错误: {e}", is_error=True)
    finally:
        tracked.result, tracked.status = result, "completed"
        emit(TOOL_EXEC_END, {"tool_use_id": block.id, "is_error": result.is_error})
```

`_to_result`：`str → content=str`；`dict → content=[dict]`（`ToolResultBlock.content: str | list[dict]`）。

### 6.2 收尾保证

`get_results` 返回的 list 长度 == `add_tool` 次数，按 `_tracked` 顺序，每个 `result` 非 None。绝不漏（漏了回灌 LLM 会因缺 tool_use_id 报错）。

### 6.3 abort：`discard()` 是必要清理

Streaming 在 `stream_turn` 期间已后台启动 task；若 `stream_turn` 后 abort，这些 task 仍在跑，必须 `executor.discard()` 取消 + 标记，避免泄漏（§5.1 ③）。Batch 模式下 `_on_add` noop 无 task 启动，`discard` 仍安全（仅标记）。`get_results` 正常路径不走到 discarded 态（abort 分支已 return）。

### 6.4 不做项

单工具超时、Bash 级联——见 §2。

## 7. 测试（§4）

设施：pytest + pytest-asyncio（auto），已就位。

**受影响现有测试（改写）**：
- `tests/test_tools.py`：`run_tools` 相关删除；补 `Tool.is_concurrency_safe` 默认值断言。
- `tests/test_stub_raises.py`：`run_tools` 桩抛错用例删除（`_not_impl` 保留——`recovery/rules.py` 仍用）。
- `tests/test_aggregate.py`：断言从"整轮一条 AssistantMessage"改为"每个 content_block_stop 一条 block 级 + 不再在 message_stop 组装整轮"。
- `tests/test_orchestrator.py`：`query_loop` 接线变了（executor 创建/传递/get_results/回灌内联、execute_tools_phase 去除），改写。

**新增 `tests/test_tool_executor/`（与源码包对应）**：
- `test_base.py`：`_execute_single` 七路径（权限拒绝/未知工具/校验失败/`pre_execute` 钩子抛异常/func 异常/str 返回/dict 返回）；`register_tool` 注册 mock tool 后跑；`get_results` 保序 + 不漏。
- `test_streaming.py`：保序（构造完成顺序随机、产出按序）、安全工具到达即并行、非安全独占排队、`_run_all` 收尾全交付、`discard` 取消 task。
- `test_batch.py`：`_partition` 切批（连续 safe 合批、非 safe 单独）、批内并发批间串行、保序。
- `test_integration.py`：mock provider 产 tool_use 流 → `stream_turn` 喂 executor → `query_loop` 收尾回灌，断言 `tool_result` 按 `tool_use_id` 对齐、`transition=NEXT_TURN`、abort 走 `discard`。

## 8. 改动清单（文件级）

| 文件 | 动作 |
|---|---|
| `core/tool_executor/__init__.py` | 新增：`make_executor` + 导出三类 |
| `core/tool_executor/base.py` | 新增：`ToolExecutor`(ABC) + `TrackedTool` |
| `core/tool_executor/streaming.py` | 新增：`StreamingToolExecutor` |
| `core/tool_executor/batch.py` | 新增：`BatchToolExecutor` |
| `core/tools.py` | 改：`Tool` 加 `is_concurrency_safe`、`func` 签名加 `ctx` 参数、`pre_execute` 钩子字段；新增 `ToolContext`；删 `run_tools`（保留 `_not_impl`，recovery 用） |
| `core/loop/phases/stream_turn.py` | 改：`aggregate_stream` block 级 yield；`stream_turn` 加 `executor` 参数、喂 executor、组装整轮、`yielded` 不含 block 级 |
| `core/loop/orchestrator.py` | 改：`query_loop` 创建/传递/收尾 executor、回灌内联、abort 调 `discard`；`QueryParams` 加 `tool_execution_mode`、`tools` 改 `list[Tool]` |
| `core/loop/phases/execute_tools.py` | **删除** |
| `core/agent_loop.py` | 改：`AgentConfig` 加 `tool_execution_mode`、`tools` 改 `list[Tool]`；`submit` 透传 `QueryParams` |
| `tests/test_tools.py`、`test_stub_raises.py`、`test_aggregate.py`、`test_orchestrator.py` | 改写 |
| `tests/test_tool_executor/` | 新增（base/streaming/batch/integration） |

## 9. 关键取舍记录

- **为什么 executor 不进 `StreamOutcome`**：它含 `asyncio.Task`/可变状态，是行为对象；塞进数据结构会逼出 dataclass 改造 + `orchestrator.py:62` dump 改造。改由 `query_loop` 管控生命周期（方案 W，对齐 md 的 queryLoop 角色），outcome 保持纯数据 BaseModel。
- **为什么收尾在 `query_loop` 而非 `stream_turn`**：对齐 md（queryLoop 创建 executor、流式 addTool、最后 getRemainingResults）；executor 管控（abort/discard/未来超时）集中在编排层；`stream_turn` 保持"流式 + 喂"的纯职责。
- **为什么去掉 `execute_tools_phase`**：方案 W 下它只剩回灌几行；回灌是编排职责，内联 `query_loop`；executor 不碰 `State` 以保单一职责与可测性。
- **为什么 `aggregate_stream` 改 block 级**：机会主义执行要求 tool_use 一固化就喂 executor，不能等整轮 `message_stop`。block 级是 md §1.3 的原始设计，也是松耦合的正确重构。
- **为什么不做增量 progress yield / 超时 / Bash 级联**：YAGNI（见 §2）。
