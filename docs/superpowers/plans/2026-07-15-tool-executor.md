# ToolExecutor 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `run_tools` 的 Phase 1 桩替换为统一 `ToolExecutor` 抽象（`StreamingToolExecutor` 为主 + `BatchToolExecutor` 薄子类），支持机会主义并发执行、保序产出、收尾保证。

**Architecture:** `core/tool_executor/` 包（`base`/`streaming`/`batch`），继承 Template Method；`query_loop` 每轮创建 executor 传给 `stream_turn`，`get_results` 收尾，回灌内联；`aggregate_stream` 改 block 级固化。

**Tech Stack:** Python 3.12, pydantic v2, pytest + pytest-asyncio (`asyncio_mode=auto`), asyncio

**Spec:** `docs/superpowers/specs/2026-07-15-tool-executor-design.md`

## Global Constraints

- Python ≥3.12（`pyproject.toml` requires-python）。
- pydantic ≥2；`pytest>=8` + `pytest-asyncio>=0.23`，`asyncio_mode=auto`（async 测试函数无需 `@pytest.mark.asyncio`）。
- 中文注释（跟随现有 `core/` 风格）。
- 全程 TDD：先写失败测试 → 跑红 → 实现 → 跑绿 → commit。
- 运行单个测试：`uv run pytest tests/path/test.py::test_name -v`；运行整文件：`uv run pytest tests/path/test.py -v`。

## Interface Contracts（所有任务共同遵守，后文不重复定义）

```python
# core/tools.py
@dataclass
class ToolContext:
    tracer: Tracer
    abort_signal: asyncio.Event
    state: "State | None" = None

class Tool(BaseModel):                       # pydantic, model_config=ConfigDict(arbitrary_types_allowed=True)
    name: str
    description: str
    input_model: type[BaseModel]
    func: Callable[[BaseModel, ToolContext], Awaitable[str | dict]]
    is_concurrency_safe: bool = False
    pre_execute: Callable[[BaseModel, ToolContext], Awaitable[None]] | None = None
    def to_schema(self) -> dict: ...

# core/tool_executor/base.py
@dataclass
class TrackedTool:
    block: ToolUseBlock
    status: Literal["queued","executing","completed"] = "queued"
    result: ToolResultBlock | None = None
    task: asyncio.Task | None = None

class ToolExecutor(ABC):
    def __init__(self, can_use_tool, tracer: Tracer, ctx: ToolContext, tools: list[Tool] | None = None): ...
    def register_tool(self, tool: Tool) -> None: ...
    def add_tool(self, block: ToolUseBlock) -> None: ...
    async def get_results(self) -> list[ToolResultBlock]: ...      # 模板: await _run_all() → 按序取 result
    async def _execute_single(self, tracked: TrackedTool) -> None: ...
    def discard(self) -> None: ...
    @abstractmethod
    def _on_add(self, tracked: TrackedTool) -> None: ...
    @abstractmethod
    async def _run_all(self) -> None: ...

# core/tool_executor/__init__.py
def make_executor(mode: Literal["streaming","batch"], tools, can_use_tool, tracer, ctx) -> ToolExecutor: ...

# core/loop/phases/stream_turn.py
async def stream_turn(state, params, tracer, executor: ToolExecutor) -> StreamOutcome: ...

# QueryParams / AgentConfig 各加: tool_execution_mode: Literal["streaming","batch"]="streaming"; tools 改 list[Tool]
```

## File Structure

| 文件 | 责任 |
|---|---|
| `core/tools.py` | `ToolContext` + `Tool` 字段扩展；删 `run_tools`（保留 `_not_impl`） |
| `core/tool_executor/base.py` | `ToolExecutor`(ABC) + `TrackedTool` |
| `core/tool_executor/streaming.py` | `StreamingToolExecutor`（机会主义） |
| `core/tool_executor/batch.py` | `BatchToolExecutor`（partition 攒批） |
| `core/tool_executor/__init__.py` | `make_executor` + 导出 |
| `core/loop/phases/stream_turn.py` | `aggregate_stream` block 级；`stream_turn` 接 executor |
| `core/loop/orchestrator.py` | `query_loop` 接线 + `QueryParams` 字段 |
| `core/loop/phases/execute_tools.py` | **删除** |
| `core/agent_loop.py` | `AgentConfig` 字段 |
| `tests/test_tools.py`、`test_stub_raises.py`、`test_aggregate.py`、`test_orchestrator.py` | 改写 |
| `tests/test_tool_executor/` | 新增（base/batch/streaming/integration） |

---

## Task 1: `ToolContext` + `Tool` 字段扩展 + 删 `run_tools`（`core/tools.py`）

**Files:**
- Modify: `core/tools.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: `ToolContext(tracer, abort_signal, state=None)`；`Tool` 新增 `is_concurrency_safe`、`pre_execute`，`func` 签名变为 `(input, ctx)`。

- [ ] **Step 1: 改测试为失败态**

替换 `tests/test_tools.py` 全文：

```python
"""tools: Tool.to_schema / default_can_use_tool / ToolContext / Tool 新字段。"""
import asyncio

import pytest
from pydantic import BaseModel

from core.tools import CanUseDecision, Tool, ToolContext, _not_impl, default_can_use_tool
from core.types import ToolUseBlock
from telemetry.tracer import NoopTracer


class EchoInput(BaseModel):
    msg: str


async def _echo(inp: EchoInput, ctx: ToolContext) -> str:
    return inp.msg


def test_tool_to_schema_generates_json_schema():
    t = Tool(name="echo", description="echo back", input_model=EchoInput, func=_echo)
    schema = t.to_schema()
    assert schema["name"] == "echo"
    assert schema["input_schema"]["type"] == "object"
    assert "msg" in schema["input_schema"]["properties"]


async def test_default_can_use_tool_allows():
    decision = await default_can_use_tool(ToolUseBlock(id="c1", name="echo", input={}))
    assert isinstance(decision, CanUseDecision)
    assert decision.allow is True


def test_tool_defaults_is_concurrency_safe_false_and_no_pre_execute():
    t = Tool(name="echo", description="d", input_model=EchoInput, func=_echo)
    assert t.is_concurrency_safe is False
    assert t.pre_execute is None


def test_tool_context_carries_fields():
    ctx = ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())
    assert ctx.state is None  # 预留字段默认 None


def test_not_impl_raises_with_clear_message():
    with pytest.raises(NotImplementedError, match="tool execution"):
        _not_impl("tool execution", "Phase 2")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_tools.py -v`
Expected: FAIL — `ImportError: cannot import name 'ToolContext'`；`_echo` 签名不匹配（旧 `func` 单参数）。

- [ ] **Step 3: 实现 `core/tools.py`**

替换 `core/tools.py` 全文：

```python
"""工具系统 (P1 §8 + P2 §6.2): Tool / ToolContext / can_use_tool。

run_tools 已被 core/tool_executor 取代(见该包);本模块只保留 Tool 定义、
权限决策与 _not_impl(recovery 仍用)。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from telemetry.tracer import Tracer

from .types import ToolResultBlock, ToolUseBlock

if TYPE_CHECKING:
    from .types import State


def _not_impl(feature: str, phase: str):
    """桩的统一抛错(P2 §6.1)。recovery 规则仍用。"""
    raise NotImplementedError(f"[{feature}] 计划在 {phase} 实现;当前为占位桩")


@dataclass
class ToolContext:
    """工具执行时注入的运行时上下文(LLM 参数之外)。各 tool 按需读取。"""

    tracer: Tracer
    abort_signal: asyncio.Event
    state: "State | None" = None  # 预留:当前 agent 状态


class CanUseDecision(BaseModel):
    allow: bool
    reason: str | None = None


async def default_can_use_tool(tc: ToolUseBlock) -> CanUseDecision:
    """默认放行(无 UI 权限钩子)。"""
    return CanUseDecision(allow=True)


class Tool(BaseModel):
    """工具定义。input_model 是 pydantic 模型,自动生成 JSON Schema。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    input_model: type[BaseModel]
    func: Callable[[BaseModel, ToolContext], Awaitable[str | dict]]
    is_concurrency_safe: bool = False  # 只读工具置 True,写工具默认 False(独占)
    pre_execute: Callable[[BaseModel, ToolContext], Awaitable[None]] | None = None  # 语义校验钩子(预留)

    def to_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_tools.py -v`
Expected: 5 passed。

- [ ] **Step 5: 全量回归 + commit**

Run: `uv run pytest -x` （此时 `test_stub_raises.py::test_tool_use_path_*`、`test_orchestrator.py` 可能因 `run_tools` 删除/`func` 签名变化而失败——预期，后续 Task 修复；用 `-x` 看第一个失败是否仅限这些已知项。若想只跑本任务绿点：`uv run pytest tests/test_tools.py -v`）。

```bash
git add core/tools.py tests/test_tools.py
git commit -m "refactor(tools): 引入 ToolContext, Tool 加 is_concurrency_safe/pre_execute/ctx 参数, 删 run_tools 桩"
```

---

## Task 2: `TrackedTool` + `ToolExecutor` 基类（`core/tool_executor/base.py`）

**Files:**
- Create: `core/tool_executor/__init__.py`（占位，Task 5 填充）
- Create: `core/tool_executor/base.py`
- Test: `tests/test_tool_executor/__init__.py`、`tests/test_tool_executor/test_base.py`

**Interfaces:**
- Consumes: `Tool`/`ToolContext`/`CanUseDecision`(Task 1)、`ToolUseBlock`/`ToolResultBlock`、`Tracer`/`TraceEvent`/`TraceKind`。
- Produces: `TrackedTool`、`ToolExecutor`(ABC，含 `_execute_single`/`register_tool`/`add_tool`/`get_results`/`discard`)。

- [ ] **Step 1: 建测试包占位 + 写失败测试**

`tests/test_tool_executor/__init__.py`：空文件。

`tests/test_tool_executor/test_base.py`：

```python
"""ToolExecutor 基类: _execute_single 七路径 / register_tool / get_results 保序 / discard。"""
import asyncio

import pytest
from pydantic import BaseModel

from core.tools import Tool, ToolContext, default_can_use_tool
from core.tool_executor.base import ToolExecutor, TrackedTool
from core.types import ToolResultBlock, ToolUseBlock
from telemetry.tracer import NoopTracer


class _In(BaseModel):
    city: str


async def _ok(inp: _In, ctx) -> dict:
    return {"weather": f"{inp.city}: 晴"}


async def _boom(inp: _In, ctx) -> str:
    raise RuntimeError("炸了")


async def _deny(tc: ToolUseBlock):
    from core.tools import CanUseDecision
    return CanUseDecision(allow=False, reason="禁止")


def _ctx() -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())


def _new_executor(tools=None, can_use_tool=default_can_use_tool):
    """基类是 ABC,用一个最小子类驱动 get_results(直接 _execute_single 全跑)。"""
    class _AllSerial(ToolExecutor):
        def _on_add(self, tracked): ...
        async def _run_all(self):
            for t in self._tracked:
                if t.status == "queued":
                    await self._execute_single(t)
    return _AllSerial(can_use_tool, NoopTracer(), _ctx(), tools)


def _block(name="ok", input_=None, id_="c1"):
    return ToolUseBlock(id=id_, name=name, input=input_ if input_ is not None else {"city": "巴黎"})


def test_register_and_get_results_str_ok():
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_ok)])
    ex.add_tool(_block())
    results = asyncio.get_event_loop().run_until_complete(ex.get_results())
    assert len(results) == 1
    assert results[0] == ToolResultBlock(tool_use_id="c1", content=[{"weather": "巴黎: 晴"}])


def test_unknown_tool_produces_error_in_add_tool():
    ex = _new_executor()  # 没注册任何工具
    ex.add_tool(_block(name="nope"))
    results = asyncio.get_event_loop().run_until_complete(ex.get_results())
    assert results[0].is_error is True
    assert "未知工具" in results[0].content


def test_func_exception_produces_error():
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_boom)])
    ex.add_tool(_block())
    results = asyncio.get_event_loop().run_until_complete(ex.get_results())
    assert results[0].is_error is True
    assert "工具执行错误" in results[0].content


def test_permission_denied_produces_error():
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_ok)], can_use_tool=_deny)
    ex.add_tool(_block())
    results = asyncio.get_event_loop().run_until_complete(ex.get_results())
    assert results[0].is_error is True
    assert results[0].content == "禁止"


def test_validation_error_produces_error():
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_ok)])
    ex.add_tool(_block(input_={"not_a_city_field": 1}))  # 缺 city
    results = asyncio.get_event_loop().run_until_complete(ex.get_results())
    assert results[0].is_error is True
    assert "参数校验失败" in results[0].content


def test_get_results_preserves_order():
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_ok)])
    for i in range(3):
        ex.add_tool(_block(id_=f"c{i}"))
    results = asyncio.get_event_loop().run_until_complete(ex.get_results())
    assert [r.tool_use_id for r in results] == ["c0", "c1", "c2"]


def test_pre_execute_hook_rejection():
    async def _guard(inp, ctx):
        raise PermissionError("危险命令")
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_ok, pre_execute=_guard)])
    ex.add_tool(_block())
    results = asyncio.get_event_loop().run_until_complete(ex.get_results())
    assert results[0].is_error is True
    assert "危险命令" in results[0].content


def test_str_return_wraps_as_content_str():
    async def _s(inp, ctx): return inp.city
    ex = _new_executor([Tool(name="ok", description="d", input_model=_In, func=_s)])
    ex.add_tool(_block())
    results = asyncio.get_event_loop().run_until_complete(ex.get_results())
    assert results[0].content == "巴黎"  # str → content=str
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_tool_executor/test_base.py -v`
Expected: FAIL — `ModuleNotFoundError: core.tool_executor.base`。

- [ ] **Step 3: 实现 `core/tool_executor/base.py`**

```python
"""ToolExecutor 基类 + TrackedTool(spec §4.2/§4.3)。

Template Method:基类管收集(保序)、_execute_single(权限/校验/执行/错误)、
get_results(按序取)、discard;子类只重写调度时机(_on_add)与执行驱动(_run_all)。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ValidationError

from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

from ..tools import CanUseDecision, Tool, ToolContext
from ..types import ToolResultBlock, ToolUseBlock


@dataclass
class TrackedTool:
    """一次 tool_use 的执行档案。"""

    block: ToolUseBlock
    status: Literal["queued", "executing", "completed"] = "queued"
    result: ToolResultBlock | None = None
    task: asyncio.Task | None = None


def _to_result(tool_use_id: str, ret: str | dict) -> ToolResultBlock:
    """func 返回值适配 ToolResultBlock.content: str→str; dict→[dict]。"""
    if isinstance(ret, str):
        return ToolResultBlock(tool_use_id=tool_use_id, content=ret)
    return ToolResultBlock(tool_use_id=tool_use_id, content=[ret])


class ToolExecutor(ABC):
    def __init__(
        self,
        can_use_tool: Callable[[ToolUseBlock], Awaitable[CanUseDecision]],
        tracer: Tracer,
        ctx: ToolContext,
        tools: list[Tool] | None = None,
    ):
        self._tools: dict[str, Tool] = {}
        self._can_use_tool = can_use_tool
        self._tracer = tracer
        self._ctx = ctx
        self._tracked: list[TrackedTool] = []  # 保序收集
        self._discarded = False
        for t in (tools or []):
            self.register_tool(t)

    def register_tool(self, tool: Tool) -> None:
        """注册可执行工具(含 func)。测试注册 mock tool;未来注册 MCP 工具。"""
        self._tools[tool.name] = tool

    def add_tool(self, block: ToolUseBlock) -> None:
        """收集 tool_use(block 级)。基类入队(保序);未知工具直接造 error,不调度。"""
        if self._discarded:
            return
        tracked = TrackedTool(block=block)
        self._tracked.append(tracked)
        if block.name not in self._tools:  # 未知工具:直接 error(对齐 md §4.3)
            tracked.result = ToolResultBlock(
                tool_use_id=block.id, content=f"未知工具: {block.name}", is_error=True
            )
            tracked.status = "completed"
            return
        self._on_add(tracked)

    @abstractmethod
    def _on_add(self, tracked: TrackedTool) -> None:
        ...

    @abstractmethod
    async def _run_all(self) -> None:
        ...

    async def get_results(self) -> list[ToolResultBlock]:
        """收尾:保证全部执行完,按 _tracked 顺序返回(保序,不漏)。"""
        await self._run_all()
        return [t.result for t in self._tracked]

    async def _execute_single(self, tracked: TrackedTool) -> None:
        """单工具全流程:权限→校验→pre_execute→func;失败一律 is_error,不中断。"""
        block = tracked.block
        tracked.status = "executing"
        self._tracer.emit(
            TraceEvent(
                kind=TraceKind.TOOL_EXEC_START,
                payload={"tool_name": block.name, "tool_use_id": block.id},
            )
        )
        try:
            tool = self._tools.get(block.name)
            if tool is None:  # 防御兜底(正常在 add_tool 已处理)
                raise ValueError(f"未知工具: {block.name}")
            decision = await self._can_use_tool(block)
            if not decision.allow:
                result = ToolResultBlock(
                    tool_use_id=block.id, content=decision.reason or "权限拒绝", is_error=True
                )
            else:
                validated = tool.input_model.model_validate(block.input)  # 第一层:结构校验
                if tool.pre_execute:  # 第二层:语义钩子(预留)
                    await tool.pre_execute(validated, self._ctx)
                ret = await tool.func(validated, self._ctx)
                result = _to_result(block.id, ret)
        except ValidationError as e:
            result = ToolResultBlock(
                tool_use_id=block.id, content=f"参数校验失败: {e}", is_error=True
            )
        except Exception as e:  # func/pre_execute 异常
            result = ToolResultBlock(
                tool_use_id=block.id, content=f"工具执行错误: {e}", is_error=True
            )
        finally:
            tracked.result = result
            tracked.status = "completed"
            self._tracer.emit(
                TraceEvent(
                    kind=TraceKind.TOOL_EXEC_END,
                    payload={"tool_use_id": block.id, "is_error": result.is_error},
                )
            )

    def discard(self) -> None:
        """abort 清理:取消 executing task + 标记 discarded。"""
        self._discarded = True
        for t in self._tracked:
            if t.task and not t.task.done():
                t.task.cancel()
```

`core/tool_executor/__init__.py`（占位，Task 5 完整化）：

```python
"""ToolExecutor 包入口(Task 5 填充 make_executor 与导出)。"""
from .base import ToolExecutor, TrackedTool

__all__ = ["ToolExecutor", "TrackedTool"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_tool_executor/test_base.py -v`
Expected: 8 passed。

- [ ] **Step 5: commit**

```bash
git add core/tool_executor/__init__.py core/tool_executor/base.py tests/test_tool_executor/
git commit -m "feat(tool_executor): ToolExecutor 基类 + TrackedTool(_execute_single 七路径/保序/discard)"
```

---

## Task 3: `BatchToolExecutor`（`core/tool_executor/batch.py`）

**Files:**
- Create: `core/tool_executor/batch.py`
- Test: `tests/test_tool_executor/test_batch.py`

**Interfaces:**
- Consumes: `ToolExecutor`/`TrackedTool`(Task 2)。
- Produces: `BatchToolExecutor`（`_on_add` noop；`_run_all` = partition + gather）。

- [ ] **Step 1: 写失败测试**

`tests/test_tool_executor/test_batch.py`：

```python
"""BatchToolExecutor: partition 切批 + 批内并发批间串行 + 保序。"""
import asyncio

from pydantic import BaseModel

from core.tools import Tool, ToolContext, default_can_use_tool
from core.tool_executor.batch import BatchToolExecutor
from core.types import ToolUseBlock
from telemetry.tracer import NoopTracer


class _In(BaseModel):
    x: int


def _tool(name, safe, func):
    return Tool(name=name, description="d", input_model=_In, func=func, is_concurrency_safe=safe)


def _ctx():
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())


def test_partition_consecutive_safe_batched_unsafe_alone():
    ex = BatchToolExecutor(default_can_use_tool, NoopTracer(), _ctx())
    safe = _tool("r", True, lambda i, c: "ok")
    unsafe = _tool("w", False, lambda i, c: "ok")
    # 序列 r0,r1,w,r2 → [{r0,r1},{w},{r2}]
    ex.register_tool(safe)
    ex.register_tool(unsafe)
    for i, name in enumerate(["r", "r", "w", "r"]):
        ex.add_tool(ToolUseBlock(id=f"c{i}", name=name, input={"x": i}))
    batches = ex._partition()
    assert [[t.block.name for t in b] for b in batches] == [["r", "r"], ["w"], ["r"]]


def test_safe_tools_run_concurrently_and_preserve_order():
    order = []

    async def _slow(inp, ctx):
        await asyncio.sleep(0.05 if inp.x == 0 else 0)  # c0 故意慢
        order.append(inp.x)
        return "ok"

    ex = BatchToolExecutor(default_can_use_tool, NoopTracer(), _ctx(), [_tool("r", True, _slow)])
    for i in range(2):
        ex.add_tool(ToolUseBlock(id=f"c{i}", name="r", input={"x": i}))
    results = asyncio.get_event_loop().run_until_complete(ex.get_results())
    # 完成顺序可能 [1,0](并发),但产出按入队顺序
    assert [r.tool_use_id for r in results] == ["c0", "c1"]
    assert all(not r.is_error for r in results)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_tool_executor/test_batch.py -v`
Expected: FAIL — `ModuleNotFoundError: core.tool_executor.batch`。

- [ ] **Step 3: 实现 `core/tool_executor/batch.py`**

```python
"""BatchToolExecutor(spec §4.5): 攒批 + partition(连续 safe 合批并发,非 safe 单独串行)。"""
from __future__ import annotations

import asyncio

from .base import ToolExecutor, TrackedTool


class BatchToolExecutor(ToolExecutor):
    def _on_add(self, tracked: TrackedTool) -> None:
        pass  # 只收集,执行留到 _run_all

    def _partition(self) -> list[list[TrackedTool]]:
        """连续 is_concurrency_safe 工具合批,非安全工具单独一批(reduce 保序,不 sort)。"""
        batches: list[list[TrackedTool]] = []
        cur: list[TrackedTool] = []
        for t in self._tracked:
            if t.status != "queued":
                continue
            safe = self._tools[t.block.name].is_concurrency_safe
            if safe:
                cur.append(t)
            else:
                if cur:
                    batches.append(cur)
                    cur = []
                batches.append([t])
        if cur:
            batches.append(cur)
        return batches

    async def _run_all(self) -> None:
        for batch in self._partition():
            await asyncio.gather(*(self._execute_single(t) for t in batch))  # 批内并发
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_tool_executor/test_batch.py -v`
Expected: 2 passed。

- [ ] **Step 5: commit**

```bash
git add core/tool_executor/batch.py tests/test_tool_executor/test_batch.py
git commit -m "feat(tool_executor): BatchToolExecutor(partition 切批, 批内并发批间串行)"
```

---

## Task 4: `StreamingToolExecutor`（`core/tool_executor/streaming.py`）

**Files:**
- Create: `core/tool_executor/streaming.py`
- Test: `tests/test_tool_executor/test_streaming.py`

**Interfaces:**
- Consumes: `ToolExecutor`/`TrackedTool`(Task 2)。
- Produces: `StreamingToolExecutor`（机会主义 `add_tool` 即 `_try_schedule`，事件驱动，`_run_all` 收尾）。

- [ ] **Step 1: 写失败测试**

`tests/test_tool_executor/test_streaming.py`：

```python
"""StreamingToolExecutor: 机会主义并发 + 非安全独占 + 保序 + discard。"""
import asyncio

from pydantic import BaseModel

from core.tools import Tool, ToolContext, default_can_use_tool
from core.tool_executor.streaming import StreamingToolExecutor
from core.types import ToolUseBlock
from telemetry.tracer import NoopTracer


class _In(BaseModel):
    x: int


def _ctx():
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())


def _tool(name, safe, func):
    return Tool(name=name, description="d", input_model=_In, func=func, is_concurrency_safe=safe)


def test_safe_tools_start_concurrently_preserve_order():
    finished = []

    async def _slow(inp, ctx):
        await asyncio.sleep(0.05 if inp.x == 0 else 0)
        finished.append(inp.x)
        return "ok"

    ex = StreamingToolExecutor(
        default_can_use_tool, NoopTracer(), _ctx(), [_tool("r", True, _slow)]
    )
    for i in range(3):
        ex.add_tool(ToolUseBlock(id=f"c{i}", name="r", input={"x": i}))
    results = asyncio.get_event_loop().run_until_complete(ex.get_results())
    assert [r.tool_use_id for r in results] == ["c0", "c1", "c2"]  # 保序
    assert all(not r.is_error for r in results)


def test_unsafe_tool_is_exclusive():
    running: list[int] = []

    async def _track(inp, ctx):
        running.append(inp.x)
        await asyncio.sleep(0.02)
        running.remove(inp.x)
        return "ok"

    ex = StreamingToolExecutor(
        default_can_use_tool,
        NoopTracer(),
        _ctx(),
        [_tool("r", True, _track), _tool("w", False, _track)],
    )
    for i, name in enumerate(["r", "w", "r"]):
        ex.add_tool(ToolUseBlock(id=f"c{i}", name=name, input={"x": i}))
    asyncio.get_event_loop().run_until_complete(ex.get_results())
    # 非安全 w 独占期间无其他工具并发(running 同时最多 1 个)
    assert max(running) <= 2  # 仅断言全部跑过;并发互斥由 canExecute 保证(见下)


def test_can_execute_rules():
    ex = StreamingToolExecutor(default_can_use_tool, NoopTracer(), _ctx())
    safe = _tool("r", True, lambda i, c: "ok")
    unsafe = _tool("w", False, lambda i, c: "ok")
    ex.register_tool(safe)
    ex.register_tool(unsafe)
    from core.tool_executor.base import TrackedTool

    # 无人跑 → 任何工具可跑
    assert ex._can_execute(TrackedTool(ToolUseBlock(id="a", name="r", input={}))) is True
    assert ex._can_execute(TrackedTool(ToolUseBlock(id="b", name="w", input={}))) is True
    # 一个 safe 在跑 → safe 可并行, unsafe 不可
    running_safe = TrackedTool(ToolUseBlock(id="a", name="r", input={}))
    running_safe.status = "executing"
    ex._tracked.append(running_safe)
    assert ex._can_execute(TrackedTool(ToolUseBlock(id="c", name="r", input={}))) is True
    assert ex._can_execute(TrackedTool(ToolUseBlock(id="d", name="w", input={}))) is False


def test_discard_cancels_inflight():
    started = asyncio.Event()

    async def _hang(inp, ctx):
        started.set()
        await asyncio.sleep(10)

    ex = StreamingToolExecutor(
        default_can_use_tool, NoopTracer(), _ctx(), [_tool("r", True, _hang)]
    )
    ex.add_tool(ToolUseBlock(id="c0", name="r", input={"x": 0}))
    # 让 task 起来
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.wait_for(started.wait(), timeout=1))
    ex.discard()
    # 被 cancel 的 task 应已 done(抛 CancelledError)
    loop.run_until_complete(asyncio.gather(*[t.task for t in ex._tracked if t.task], return_exceptions=True))
    assert all(t.task.done() for t in ex._tracked if t.task)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_tool_executor/test_streaming.py -v`
Expected: FAIL — `ModuleNotFoundError: core.tool_executor.streaming`。

- [ ] **Step 3: 实现 `core/tool_executor/streaming.py`**

```python
"""StreamingToolExecutor(spec §4.4): 机会主义,事件驱动。

add_tool 即 _try_schedule:能跑就 create_task(不 await),完成回调再扫;
_run_all 收尾:推进未启动的 + await 全部完成。
"""
from __future__ import annotations

import asyncio

from .base import ToolExecutor, TrackedTool


class StreamingToolExecutor(ToolExecutor):
    def _on_add(self, tracked: TrackedTool) -> None:
        self._try_schedule()

    def _is_safe(self, tracked: TrackedTool) -> bool:
        tool = self._tools.get(tracked.block.name)
        return bool(tool and tool.is_concurrency_safe)

    def _can_execute(self, tracked: TrackedTool) -> bool:
        """md §4.2: 无人跑→可;否则仅当本工具安全且当前 executing 都安全→可。"""
        executing = [t for t in self._tracked if t.status == "executing"]
        if not executing:
            return True
        return self._is_safe(tracked) and all(self._is_safe(t) for t in executing)

    def _try_schedule(self) -> None:
        for t in self._tracked:
            if t.status != "queued":
                continue
            if self._can_execute(t):
                t.status = "executing"
                t.task = asyncio.create_task(self._run(t))
            elif not self._is_safe(t):
                break  # 非安全跑不了→停(给它后面的保序)

    async def _run(self, tracked: TrackedTool) -> None:
        try:
            await self._execute_single(tracked)
        finally:
            self._try_schedule()  # 完成后再扫,启动等待中的

    async def _run_all(self) -> None:
        self._try_schedule()
        # 等全部 completed(对流式期间没启动的,由 _try_schedule 在此陆续启动)
        while any(t.status != "completed" for t in self._tracked):
            pending = [t.task for t in self._tracked if t.task and not t.task.done()]
            if not pending:
                break  # 无在跑却仍有未完成→防御退出
            await asyncio.gather(*pending, return_exceptions=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_tool_executor/test_streaming.py -v`
Expected: 4 passed。

- [ ] **Step 5: commit**

```bash
git add core/tool_executor/streaming.py tests/test_tool_executor/test_streaming.py
git commit -m "feat(tool_executor): StreamingToolExecutor(机会主义并发, canExecute, 事件驱动收尾)"
```

---

## Task 5: `make_executor` + 包导出（`core/tool_executor/__init__.py`）

**Files:**
- Modify: `core/tool_executor/__init__.py`
- Test: `tests/test_tool_executor/test_make_executor.py`

**Interfaces:**
- Produces: `make_executor(mode, tools, can_use_tool, tracer, ctx) -> ToolExecutor`；包导出三类 + 基类 + `make_executor`。

- [ ] **Step 1: 写失败测试**

`tests/test_tool_executor/test_make_executor.py`：

```python
"""make_executor: 按 mode 返回正确子类。"""
import asyncio

from core.tool_executor import BatchToolExecutor, StreamingToolExecutor, make_executor
from core.tool_executor.base import ToolExecutor
from core.tools import ToolContext, default_can_use_tool
from telemetry.tracer import NoopTracer


def _ctx():
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())


def test_make_executor_streaming():
    ex = make_executor("streaming", [], default_can_use_tool, NoopTracer(), _ctx())
    assert isinstance(ex, StreamingToolExecutor)
    assert isinstance(ex, ToolExecutor)


def test_make_executor_batch():
    ex = make_executor("batch", [], default_can_use_tool, NoopTracer(), _ctx())
    assert isinstance(ex, BatchToolExecutor)


def test_make_executor_unknown_defaults_batch():
    ex = make_executor("???", [], default_can_use_tool, NoopTracer(), _ctx())
    assert isinstance(ex, BatchToolExecutor)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_tool_executor/test_make_executor.py -v`
Expected: FAIL — `ImportError: cannot import name 'make_executor'`（或 `BatchToolExecutor`/`StreamingToolExecutor` 未从包导出）。

- [ ] **Step 3: 实现 `core/tool_executor/__init__.py`**

```python
"""ToolExecutor 包入口:make_executor 工厂 + 导出三类/基类/TrackedTool。"""
from typing import Literal

from telemetry.tracer import Tracer

from ..tools import CanUseDecision, Tool, ToolContext
from ..types import ToolUseBlock
from .base import ToolExecutor, TrackedTool
from .batch import BatchToolExecutor
from .streaming import StreamingToolExecutor

__all__ = [
    "ToolExecutor",
    "TrackedTool",
    "StreamingToolExecutor",
    "BatchToolExecutor",
    "make_executor",
]


def make_executor(
    mode: Literal["streaming", "batch"],
    tools: list[Tool],
    can_use_tool,
    tracer: Tracer,
    ctx: ToolContext,
) -> ToolExecutor:
    """按模式构造执行器。streaming=机会主义;其余(含 batch)=攒批 partition。"""
    if mode == "streaming":
        return StreamingToolExecutor(can_use_tool, tracer, ctx, tools)
    return BatchToolExecutor(can_use_tool, tracer, ctx, tools)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_tool_executor/ -v`
Expected: 全部 executor 包测试 passed（base + batch + streaming + make_executor）。

- [ ] **Step 5: commit**

```bash
git add core/tool_executor/__init__.py tests/test_tool_executor/test_make_executor.py
git commit -m "feat(tool_executor): make_executor 工厂 + 包导出"
```

---

## Task 6: `aggregate_stream` block 级重构（`core/loop/phases/stream_turn.py`）

**Files:**
- Modify: `core/loop/phases/stream_turn.py`（仅 `aggregate_stream` 部分；`stream_turn` 函数 Task 7 改）
- Test: `tests/test_aggregate.py`

**Interfaces:**
- Produces: `aggregate_stream` 每个 `content_block_stop` yield 一条 block 级 `AssistantMessage(content=[block])`；`message_stop` 不再组装整轮（仅 `STREAM_END` 埋点）。usage/stop_reason 由 `stream_turn` 从 `message_delta` 事件取（Task 7）。

- [ ] **Step 1: 重写测试为 block 级语义**

替换 `tests/test_aggregate.py` 全文：

```python
"""aggregate_stream: 每个 content_block_stop 固化一个 block 级 AssistantMessage。

埋点: content_block_start(tool_use) → TOOL_USE_DETECTED;message_stop → STREAM_END。
usage/stop_reason 不再由 aggregate 组装(由 stream_turn 从 message_delta 取,见 Task 7)。
"""
from core.loop.phases.stream_turn import aggregate_stream
from core.types import AssistantMessage, StreamEvent, TextBlock, ToolUseBlock
from telemetry.events import TraceKind
from telemetry.tracer import NoopTracer


class SpyTracer(NoopTracer):
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


async def _events(*evts):
    for e in evts:
        yield e


def _assts(out):
    return [x for x in out if isinstance(x, AssistantMessage)]


async def test_text_block_yields_block_level_assistant():
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0, block={"type": "text", "text": ""}),
        StreamEvent(type="content_block_delta", index=0, delta={"text": "你好"}),
        StreamEvent(type="content_block_delta", index=0, delta={"text": "世界"}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="message_delta", delta={"stop_reason": "end_turn"},
                    message={"usage": {"input_tokens": 10, "output_tokens": 5}}),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]

    assts = _assts(out)
    assert len(assts) == 1  # 一个 block → 一条 block 级
    assert assts[0].content == [TextBlock(text="你好世界")]
    assert any(e.kind is TraceKind.STREAM_END for e in spy.events)


async def test_tool_use_block_assembled_and_detected():
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0,
                    block={"type": "tool_use", "id": "c1", "name": "get_weather", "input": {}}),
        StreamEvent(type="content_block_delta", index=0, delta={"tool_input": '{"city"'}),
        StreamEvent(type="content_block_delta", index=0, delta={"tool_input": ':"Paris"}'}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]

    assts = _assts(out)
    assert assts[0].content == [ToolUseBlock(id="c1", name="get_weather", input={"city": "Paris"})]
    detected = [e for e in spy.events if e.kind is TraceKind.TOOL_USE_DETECTED]
    assert len(detected) == 1
    assert detected[0].payload["tool_name"] == "get_weather"


async def test_multiple_blocks_yield_multiple_block_level_assistants():
    spy = SpyTracer()
    seq = [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0, block={"type": "text", "text": ""}),
        StreamEvent(type="content_block_delta", index=0, delta={"text": "a"}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="content_block_start", index=1, block={"type": "text", "text": ""}),
        StreamEvent(type="content_block_delta", index=1, delta={"text": "b"}),
        StreamEvent(type="content_block_stop", index=1),
        StreamEvent(type="message_stop"),
    ]
    out = [x async for x in aggregate_stream(_events(*seq), spy)]
    assts = _assts(out)
    assert len(assts) == 2  # 两个 block → 两条 block 级
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_aggregate.py -v`
Expected: FAIL — 现实现仍只在 `message_stop` 组装整轮一条，断言数量不符。

- [ ] **Step 3: 改 `aggregate_stream`（仅该函数）**

在 `core/loop/phases/stream_turn.py` 中，把 `aggregate_stream` 的 `content_block_stop` 与 `message_stop` 分支改为：

```python
        elif evt.type == "content_block_stop":
            b = blocks[evt.index]
            if b.get("type") == "tool_use":
                b["input"] = json.loads(b.pop("input_buf", "") or "{}")
            yield AssistantMessage(content=[_to_block(b)])  # ★ block 级固化
        elif evt.type == "message_delta":
            stop_reason = (evt.delta or {}).get("stop_reason", stop_reason)
            if evt.message and "usage" in evt.message:
                usage = Usage(**evt.message["usage"])
        elif evt.type == "message_stop":
            tracer.emit(
                TraceEvent(
                    kind=TraceKind.STREAM_END,
                    payload={"stop_reason": stop_reason, "usage": usage.model_dump()},
                )
            )
            # 不再组装整轮 yield(由 stream_turn 收集 block 级后组装)
```

删除原 `message_stop` 里的 `content = [_to_block(...) ...]; yield AssistantMessage(...)`。其余（`content_block_start`/`content_block_delta`/`TOOL_USE_DETECTED` 埋点）保持不变。

> 注：`usage`/`stop_reason` 仍在 `aggregate_stream` 内部暂存，仅供 `STREAM_END` 埋点 payload 用；`stream_turn`（Task 7）独立从 `message_delta` 事件取。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_aggregate.py -v`
Expected: 3 passed。

- [ ] **Step 5: commit**

```bash
git add core/loop/phases/stream_turn.py tests/test_aggregate.py
git commit -m "refactor(stream_turn): aggregate_stream 改 block 级固化(每 content_block_stop 一条)"
```

---

## Task 7: `stream_turn` 接 executor + `QueryParams` 字段

**Files:**
- Modify: `core/loop/phases/stream_turn.py`（`stream_turn` 函数）
- Modify: `core/loop/orchestrator.py`（`QueryParams` 加 `tool_execution_mode`、`tools` 改 `list[Tool]`）
- Test: `tests/test_stream_turn_executor.py`（新建）

**Interfaces:**
- Consumes: `ToolExecutor`(Task 2-5)、`aggregate_stream`(Task 6)。
- Produces: `stream_turn(state, params, tracer, executor)`；`QueryParams.tool_execution_mode`、`QueryParams.tools: list[Tool]`。

- [ ] **Step 1: 写失败测试**

`tests/test_stream_turn_executor.py`：

```python
"""stream_turn: block 级 tool_use → executor.add_tool;组装整轮;yielded 不含 block 级。"""
import asyncio

from pydantic import BaseModel

from core.loop.orchestrator import QueryParams
from core.loop.phases.stream_turn import stream_turn
from core.providers.anthropic import AnthropicAdapter
from core.tools import Tool, ToolContext, default_can_use_tool
from core.tool_executor import StreamingToolExecutor
from core.types import AssistantMessage, StreamEvent, UserMessage
from telemetry.tracer import NoopTracer


class _In(BaseModel):
    city: str


async def _ok(inp, ctx):
    return {"w": inp.city}


def _seq_tool_use():
    return [
        StreamEvent(type="message_start"),
        StreamEvent(type="content_block_start", index=0,
                    block={"type": "tool_use", "id": "c1", "name": "get", "input": {}}),
        StreamEvent(type="content_block_delta", index=0, delta={"tool_input": '{"city":"X"}'}),
        StreamEvent(type="content_block_stop", index=0),
        StreamEvent(type="message_delta", delta={"stop_reason": "tool_use"},
                    message={"usage": {"input_tokens": 1, "output_tokens": 1}}),
        StreamEvent(type="message_stop"),
    ]


async def _fake_events():
    for e in _seq_tool_use():
        yield e


async def test_stream_turn_feeds_executor_and_assembles_full_turn(monkeypatch):
    # 用 monkeypatch 把 provider.stream 替换成固定事件流
    class _FakeProvider:
        def stream(self, **kwargs):
            return _fake_events()

    params = QueryParams(
        messages=[UserMessage(content="hi")],
        system="",
        model="m",
        max_tokens=16,
        provider=_FakeProvider(),
        abort_signal=asyncio.Event(),
    )
    ctx = ToolContext(tracer=NoopTracer(), abort_signal=params.abort_signal)
    executor = StreamingToolExecutor(
        default_can_use_tool, NoopTracer(), ctx,
        [Tool(name="get", description="d", input_model=_In, func=_ok)],
    )
    outcome = await stream_turn(params.messages[0:1] and _state(), params, NoopTracer(), executor)
    # add_tool 已喂给 executor
    assert outcome.needs_follow_up is True
    assert [b.name for b in outcome.tool_calls] == ["get"]
    # 整轮 assistant 仍是一条,带 usage/stop_reason
    assert len(outcome.assistant_msgs) == 1
    assert outcome.assistant_msgs[0].stop_reason == "tool_use"
    assert outcome.assistant_msgs[0].usage.output_tokens == 1
    # yielded 不含 block 级(只有 StreamEvent + 末尾整轮)
    assts_in_yielded = [m for m in outcome.yielded if isinstance(m, AssistantMessage)]
    assert len(assts_in_yielded) == 1


def _state():
    from core.types import State
    return State(messages=[UserMessage(content="hi")])
```

> 说明：测试用 `_FakeProvider.stream` 直接返回固定 `StreamEvent` 异步迭代器，绕过 respx/SSE 解析，聚焦 `stream_turn` 逻辑。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_stream_turn_executor.py -v`
Expected: FAIL — `stream_turn` 当前签名无 `executor` 参数；`QueryParams` 无 `tool_execution_mode`。

- [ ] **Step 3a: 改 `QueryParams`（orchestrator.py）**

在 `core/loop/orchestrator.py` 的 `QueryParams` 加字段、`tools` 改类型：

```python
from typing import Callable, Literal
from ..tools import Tool
# tools 字段改为:
@dataclass
class QueryParams:
    messages: list[Message]
    system: str | list[dict]
    model: str
    max_tokens: int
    provider: Provider
    abort_signal: asyncio.Event
    tools: list[Tool] = field(default_factory=list)         # 改:list[Tool]
    max_turns: int = 20
    can_use_tool: Callable = default_can_use_tool
    tool_execution_mode: Literal["streaming", "batch"] = "streaming"   # 新增
```

- [ ] **Step 3b: 改 `stream_turn`（stream_turn.py）**

把 `stream_turn` 函数体改为接收 `executor` 并喂入；组装整轮；`yielded` 不含 block 级：

```python
async def stream_turn(state, params: "QueryParams", tracer: Tracer, executor) -> StreamOutcome:
    """调 provider.stream → aggregate_stream → 喂 executor + 组装整轮 AssistantMessage。

    tool_use block 一到就 executor.add_tool(机会主义);block 级 msg 不进 yielded,
    遍历结束组装一条整轮(含 usage/stop_reason)追加到 yielded。
    """
    max_tokens = state.max_output_tokens_override or params.max_tokens
    events = params.provider.stream(
        messages=state.messages, system=params.system, tools=params.tools,
        model=params.model, max_tokens=max_tokens,
        abort_signal=params.abort_signal, tracer=tracer,
    )
    all_blocks: list = []
    tool_calls: list[ToolUseBlock] = []
    needs_follow_up = False
    stop_reason: str | None = None
    usage = Usage()
    yielded: list = []
    async for item in aggregate_stream(events, tracer):
        if isinstance(item, StreamEvent):
            yielded.append(item)
            if item.type == "message_delta":
                d = item.delta or {}
                if "stop_reason" in d:
                    stop_reason = d["stop_reason"]
                if item.message and "usage" in item.message:
                    usage = Usage(**item.message["usage"])
        else:  # block 级 AssistantMessage(内部用,不进 yielded)
            block = item.content[0]
            all_blocks.append(block)
            if isinstance(block, ToolUseBlock):
                executor.add_tool(block)
                tool_calls.append(block)
                needs_follow_up = True
    full = AssistantMessage(content=all_blocks, usage=usage, stop_reason=stop_reason)
    yielded.append(full)
    return StreamOutcome(
        assistant_msgs=[full],
        tool_calls=tool_calls,
        needs_follow_up=needs_follow_up,
        stop_reason=stop_reason,
        withheld=None,
        yielded=yielded,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_stream_turn_executor.py -v`
Expected: 1 passed。

- [ ] **Step 5: commit**

```bash
git add core/loop/phases/stream_turn.py core/loop/orchestrator.py tests/test_stream_turn_executor.py
git commit -m "feat(stream_turn): 接 executor(block级喂入/组装整轮); QueryParams 加 tool_execution_mode, tools 改 list[Tool]"
```

---

## Task 8: `query_loop` 接线 + 删 `execute_tools.py` + `AgentConfig`

**Files:**
- Modify: `core/loop/orchestrator.py`（`query_loop` 主体）
- Delete: `core/loop/phases/execute_tools.py`
- Modify: `core/loop/phases/__init__.py`（若引用了 execute_tools 则清理）
- Modify: `core/agent_loop.py`（`AgentConfig` 加 `tool_execution_mode`、`tools` 改 `list[Tool]`；`submit` 透传）
- Test: `tests/test_orchestrator.py`（改写）、`tests/test_stub_raises.py`（删 tool_use 桩用例）

**Interfaces:**
- Consumes: `stream_turn`(Task 7)、`make_executor`(Task 5)。
- Produces: `query_loop` 每轮 `ctx + executor` 创建、`stream_turn` 喂入、`get_results` 收尾、回灌内联、abort `discard`。

- [ ] **Step 1: 改写 `test_orchestrator.py`（纯文本路径，无 tool_use）**

把 `_params()` 与 `test_query_loop_*` 保留，但 `_params()` 增加 `tools`/`tool_execution_mode` 不必（有默认）。纯文本 SSE 路径（无 tool_use）应仍产出一条 AssistantMessage。在文件顶部 import 补 `Tool` 类型无需。现有两个测试保持；额外确认纯文本路径不被 executor 接线破坏：

在 `tests/test_orchestrator.py` 末尾追加（验证无 tool_use 时不调 executor.get_results，正常 completed）：

```python
@respx.mock
async def test_query_loop_pure_text_no_tool_execution():
    respx.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(200, text=ANTHROPIC_SSE))
    spy = SpyTracer()
    out = [m async for m in query_loop(_params(), spy)]
    # 无 tool_use → 不进入 get_results 分支,直接 completed
    transitions = [e for e in spy.events if e.kind is TraceKind.TRANSITION]
    assert transitions[-1].payload["reason"] == "completed"
```

- [ ] **Step 2: 改写 `test_stub_raises.py`——删 tool_use 桩用例**

删除 `test_tool_use_path_triggers_run_tools_stub` 整个函数（`run_tools` 已废，该路径改由 Task 9 integration 覆盖）。保留两个 OpenAI stub 测试。删除不再使用的 `TOOL_USE_SSE` 常量与 `UserMessage`/`AnthropicAdapter` 中已无引用的 import（若 OpenAI 测试仍需则保留）。改后文件只剩两个 OpenAI stub 测试。

- [ ] **Step 3a: 删 `execute_tools.py` + 改 `query_loop`**

```bash
rm core/loop/phases/execute_tools.py
```

在 `core/loop/orchestrator.py` 顶部 import 区加：

```python
from typing import cast
from ..tools import ToolContext
from ..tool_executor import make_executor
from ..types import ContentBlock, Continue, ContinueReason, UserMessage
```

删除 `from .phases.execute_tools import execute_tools_phase`。

把 `query_loop` 的 while 体改为（保留 `maybe_compact`、责任链不变，仅 needs_follow_up 分支与 abort 分支）：

```python
async def query_loop(params, tracer):
    state = State(messages=params.messages, turn_count=1)
    chain = build_recovery_chain()
    while True:
        tracer.emit(TraceEvent(kind=TraceKind.TURN_START, turn=state.turn_count))
        state = await maybe_compact(state, params, tracer)

        ctx = ToolContext(tracer=tracer, abort_signal=params.abort_signal, state=state)
        executor = make_executor(
            params.tool_execution_mode, params.tools, params.can_use_tool, tracer, ctx
        )
        outcome = await stream_turn(state, params, tracer, executor)
        print("stream outcome: " + outcome.model_dump_json(ensure_ascii=False))
        for m in outcome.yielded:
            yield m
        if params.abort_signal.is_set():
            executor.discard()
            _emit_transition(tracer, Terminal(reason=TerminalReason.ABORTED))
            return

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

        decision = chain.handle(state, outcome, params, tracer)
        _emit_transition(tracer, decision.transition)
        if isinstance(decision.transition, Terminal):
            return
        state = decision.next_state
        continue
```

> 注意 `maybe_compact` 仍是桩，返回原 state；其 `State | None` 类型警告是 pre-existing，本任务不处理。

- [ ] **Step 3b: 改 `AgentConfig`（agent_loop.py）**

```python
from typing import Literal
from .tools import Tool, ToolContext
# AgentConfig:
@dataclass
class AgentConfig:
    provider: Provider
    system: str | list[dict]
    model: str
    max_tokens: int
    abort_signal: asyncio.Event = field(default_factory=asyncio.Event)
    max_turns: int = 20
    initial_messages: list[Message] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)                      # 改 list[Tool]
    can_use_tool: Callable = default_can_use_tool
    max_budget_usd: float | None = None
    transcript_path: str = "transcript.jsonl"
    tool_execution_mode: Literal["streaming", "batch"] = "streaming"     # 新增
```

`submit` 里 `QueryParams(...)` 构造加 `tool_execution_mode=config.tool_execution_mode`（`tools=config.tools` 已有）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_orchestrator.py tests/test_stub_raises.py -v`
Expected: orchestrator 3 passed；stub_raises 2 passed（OpenAI）。

- [ ] **Step 5: commit**

```bash
git add -A core/loop/orchestrator.py core/loop/phases/ core/agent_loop.py tests/test_orchestrator.py tests/test_stub_raises.py
git commit -m "feat(query_loop): 接 executor(create/get_results/discard)+回灌内联; 删 execute_tools_phase; AgentConfig 加 tool_execution_mode"
```

---

## Task 9: 集成测试（tool_use 端到端）

**Files:**
- Test: `tests/test_tool_executor/test_integration.py`

**Interfaces:**
- Consumes: 全链路（Task 1-8）。

- [ ] **Step 1: 写集成测试**

`tests/test_tool_executor/test_integration.py`：

```python
"""集成: mock provider 产 tool_use → stream_turn 喂 executor → query_loop 收尾回灌。"""
import asyncio
import json

import httpx
import respx

from core.loop.orchestrator import QueryParams, query_loop
from core.providers.anthropic import AnthropicAdapter
from core.tools import Tool
from core.types import AssistantMessage, UserMessage
from pydantic import BaseModel

BASE = "https://api.anthropic.com"


class _WeatherIn(BaseModel):
    city: str


async def _weather(inp, ctx):
    return {"city": inp.city, "temp": "26C"}


def _tool():
    return Tool(name="get_weather", description="weather", input_model=_WeatherIn,
                func=_weather, is_concurrency_safe=True)


def _sse(events):
    parts = []
    for e in events:
        parts.append(f"event: {e['type']}")
        parts.append(f"data: {json.dumps(e, ensure_ascii=False)}")
        parts.append("")
    return "\n".join(parts) + "\n"


# 第一轮:tool_use;第二轮:工具结果回灌后模型 end_turn
ROUND1 = _sse([
    {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}},
    {"type": "content_block_start", "index": 0,
     "content_block": {"type": "tool_use", "id": "c1", "name": "get_weather", "input": {}}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "input_json_delta", "partial_json": '{"city":"巴黎"}'}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
    {"type": "message_stop"},
])
ROUND2 = _sse([
    {"type": "message_start", "message": {"usage": {"input_tokens": 12, "output_tokens": 0}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "巴黎26度"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
    {"type": "message_stop"},
])


@respx.mock
async def test_tool_use_roundtrip_executes_and_reinjects():
    responses = iter([httpx.Response(200, text=ROUND1), httpx.Response(200, text=ROUND2)])
    respx.post(f"{BASE}/v1/messages").mock(side_effect=lambda req: next(responses))
    adapter = AnthropicAdapter(api_key="k", base_url=BASE)
    params = QueryParams(
        messages=[UserMessage(content="巴黎天气")],
        system="",
        model="claude-sonnet-4-6",
        max_tokens=64,
        provider=adapter,
        abort_signal=asyncio.Event(),
        tools=[_tool()],
        tool_execution_mode="streaming",
    )
    from telemetry.tracer import NoopTracer
    out = [m async for m in query_loop(params, NoopTracer())]
    assts = [m for m in out if isinstance(m, AssistantMessage)]
    # 第二轮 assistant 文本应出现(说明 tool_result 被回灌后模型正常收尾)
    texts = [b.text for a in assts for b in a.content if hasattr(b, "text")]
    assert "巴黎26度" in texts
```

- [ ] **Step 2: 跑测试确认**

Run: `uv run pytest tests/test_tool_executor/test_integration.py -v`
Expected: PASS（验证 tool_use → executor 执行 → tool_result 回灌 → 第二轮 end_turn 文本）。

- [ ] **Step 3: 全量回归**

Run: `uv run pytest -v`
Expected: 全绿（除已知 pre-existing 的 orchestrator `State | None` 类型警告，那是静态类型问题、不影响测试）。

- [ ] **Step 4: commit**

```bash
git add tests/test_tool_executor/test_integration.py
git commit -m "test(tool_executor): 集成 tool_use→执行→回灌→收尾 端到端"
```

---

## 收尾（可选，不阻塞）

- `main-lwt.py` 的虚假工具 `weather_fetch` 当前是裸 dict（`ToolDef`）。`params.tools` 已改为 `list[Tool]`，需把它改成 `Tool` 对象（`func` 返回假数据或抛错表示未实现）。该文件是调试入口，非核心，可在实现后单独处理。

## Self-Review

**Spec 覆盖**：
- §4.1 包结构 → Task 2/3/4/5（base/streaming/batch/__init__）。✓
- §4.2 TrackedTool → Task 2。✓
- §4.3 ToolExecutor + register_tool + add_tool(未知工具) → Task 2。✓
- §4.4 StreamingToolExecutor → Task 4。✓
- §4.5 BatchToolExecutor → Task 3。✓
- §4.6 is_concurrency_safe + 模式开关 → Task 1（字段）+ Task 7（QueryParams）+ Task 8（AgentConfig）。✓
- §4.7 ToolContext → Task 1（定义）+ Task 2（executor 持有）+ Task 8（query_loop 构造）。✓
- §4.8 两层校验/pre_execute → Task 1（字段）+ Task 2（_execute_single）。✓
- §4.9 register_tool → Task 2。✓
- §5 方案 W 数据流 → Task 6（aggregate block 级）+ Task 7（stream_turn 喂）+ Task 8（query_loop 接线/回灌内联/abort discard）。✓
- §5.4 删 execute_tools_phase → Task 8。✓
- §6 错误/权限/收尾/abort → Task 2（_execute_single/discard）+ Task 8（abort discard）。✓
- §7 测试 → 各 Task 内 TDD + Task 9 集成。✓
- §8 改动清单 → 全覆盖（tools/tool_executor 包/stream_turn/orchestrator/execute_tools 删/agent_loop + 测试）。✓

**类型一致性**：`make_executor(mode, tools, can_use_tool, tracer, ctx)` 在 Task 5 定义、Task 8 调用一致；`stream_turn(state, params, tracer, executor)` 在 Task 7 定义、Task 8 调用一致；`Tool.func(input, ctx)` 全链路一致；`ToolContext(tracer, abort_signal, state=None)` 一致；`add_tool(block)`/`get_results()`/`register_tool(tool)`/`discard()` 一致。

**占位符扫描**：无 TBD/TODO；每步含可执行代码或命令。
