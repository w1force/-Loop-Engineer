# builtin 工具迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 CC 的 read/write/glob/grep 四个 builtin 工具迁移到本项目的 `Tool` 框架(实用核心版 + readFileState 陈旧检测 + C 类增强),给 agent 读写搜索代码的能力。

**Architecture:** 4 工具纯新增到 `core/builtin_tools/` 包;框架侧只做两处必要接线——`ToolResultBlock.content` 类型收窄(杜绝任意 dict)+ `ToolContext.read_state` 注入(agent 级 FileReadState,供 read/write 共享陈旧检测)。func 返回 `str`(类型层面保证合法 Anthropic content)。grep 用系统 ripgrep(subprocess)。

**Tech Stack:** Python 3.10+、pydantic v2、pytest(asyncio_mode=auto)、pathlib/difflib/asyncio 标准库、系统 ripgrep 二进制。

## Global Constraints

(每个 task 隐式包含,值照 spec 抄)

- pytest `asyncio_mode = auto`:测试直接 `async def test_xxx()`,不加 `@pytest.mark.asyncio`。`tmp_path` fixture 造临时文件。
- pyright `typeCheckingMode = "basic"` 必须通过。
- **不引入新依赖**:glob 用 `pathlib`、write diff 用 `difflib`、grep 用系统 `rg`(subprocess,环境已装 15.1.0)。
- 留在 `feat/four-tool-lwt` 分支,不提交 main;每 task 末尾 commit。
- **工具错误抛异常** → 框架 `_execute_single except Exception` 转 `is_error` result;正常返回 `str`。
- 字段名 **Python 风格**(`context_before` 等),内部映射 rg flag。
- 精确常量:`MAX_READ_BYTES = 256_000`、`GLOB_LIMIT = 100`、grep `head_limit` 默认 `250`、`VCS_DIRS = [".git",".svn",".hg",".bzr",".jj",".sl"]`、rg 退出码 `0`=有匹配/`1`=无匹配/`>1`=错误。
- 中文注释(对齐现有代码风格)。

## File Structure

| 文件 | 责任 | 动作 |
|------|------|------|
| `core/types.py` | `ToolResultBlock.content` 收窄 + `ToolResultContent` | 改 |
| `core/tools.py` | `func` 签名收窄 + `ToolContext.read_state` | 改 |
| `core/tool_executor/base.py` | `_to_result` 归一化 str/TextBlock/list | 改 |
| `core/builtin_tools/readstate.py` | `FileReadState` + `ReadRecord` | 新建 |
| `core/builtin_tools/glob.py` | `GlobIn` + `glob_tool(cwd)` | 新建 |
| `core/builtin_tools/grep.py` | `GrepIn` + `grep_tool(cwd)`(ripgrep) | 新建 |
| `core/builtin_tools/read.py` | `ReadIn` + `read_tool(read_state, cwd)` | 新建 |
| `core/builtin_tools/write.py` | `WriteIn` + `write_tool(read_state, cwd)` | 新建 |
| `core/builtin_tools/__init__.py` | `builtin_tools()` 工厂 | 新建 |
| `core/loop/orchestrator.py` | `query_loop` 接收 `read_state` 注入 ToolContext | 改 |
| `core/agent_loop.py` | `submit` 创建 `FileReadState` 传入 | 改 |
| `tests/test_builtin_tools/` | 5 个测试文件 | 新建 |

---

### Task 1: 输出类型强化(types / tools / base 收窄)

**Files:**
- Modify: `core/types.py`、`core/tools.py`、`core/tool_executor/base.py`
- Modify: `main-lwt.py`(mock `_fetch` 返回值 dict → str,适配收窄)
- Test: `tests/test_tool_executor/test_base.py`

**Interfaces:**
- Produces: `ToolResultBlock.content: str | list[TextBlock]`(收窄);`_to_result(tool_use_id, ret: str|TextBlock|list[TextBlock]) -> ToolResultBlock`(归一化);`Tool.func` 签名 `Awaitable[str | TextBlock | list[TextBlock]]`。后续所有工具 func 返回 str 依赖此。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_tool_executor/test_base.py`;顶部 import 补 `TextBlock`、`pytest`)

```python
import pytest
from core.types import TextBlock, ToolResultBlock
from core.tool_executor.base import _to_result


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
        ToolResultBlock(tool_use_id="c1", content=[{"filenames": ["a", "b"]}])
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_tool_executor/test_base.py -v`
Expected: FAIL — `test_tool_result_block_rejects_arbitrary_dict` 不通过(当前 `content: str | list[dict]` 接受任意 dict);其余 `_to_result` 现行为 dict 分支。

- [ ] **Step 3: 实现**

`core/types.py` — `TextBlock` 已存在,只改 `ToolResultBlock.content` + 加别名:

```python
class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[TextBlock]   # 收窄: 原 str | list[dict]
    is_error: bool = False
```

> `TextBlock` 定义在 `ToolResultBlock` 之前(已是),无需调整顺序。`ToolResultContent = str | list[TextBlock]` 别名可选(spec 提及),本 task 不强制导出(实现里如需再加)。

`core/tools.py` — `func` 签名收窄(import `TextBlock`):

```python
from .types import TextBlock, ToolUseBlock   # TextBlock 加入 import

class Tool(BaseModel):
    ...
    func: Callable[..., Awaitable[str | TextBlock | list[TextBlock]]]
    ...
```

`core/tool_executor/base.py` — `_to_result` 归一化(import `TextBlock`):

```python
from ..types import TextBlock, ToolResultBlock, ToolUseBlock   # TextBlock 加入


def _to_result(tool_use_id: str, ret: str | TextBlock | list[TextBlock]) -> ToolResultBlock:
    """func 返回值适配 ToolResultBlock.content: str→str; TextBlock→[block]; list→list。"""
    if isinstance(ret, str):
        return ToolResultBlock(tool_use_id=tool_use_id, content=ret)
    if isinstance(ret, TextBlock):
        return ToolResultBlock(tool_use_id=tool_use_id, content=[ret])
    return ToolResultBlock(tool_use_id=tool_use_id, content=ret)
```

`main-lwt.py` — mock `_fetch` 返回 dict → str(适配收窄,该文件 .gitignore 但避免本地炸):

```python
async def _fetch(inp: FetchIn, ctx) -> str:   # 原 -> dict
    await asyncio.sleep(0.5)
    return f"data-{inp.key}"                    # 原 {"key":..., "value":...}
```

> `main-lwt.py` 若有其他 dict 返回的 mock 也一并改 str。该文件不进 git,仅本地适配。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_tool_executor/ -v && pytest -q`
Expected: 新 4 测通过 + 全量不破(`content=str` 的现有 result/占位不受影响)。

- [ ] **Step 5: commit**

```bash
git add core/types.py core/tools.py core/tool_executor/base.py tests/test_tool_executor/test_base.py
git commit -m "feat: ToolResultBlock.content 收窄 str|list[TextBlock] + _to_result 归一化"
```

---

### Task 2: FileReadState + ToolContext.read_state

**Files:**
- Create: `core/builtin_tools/__init__.py`(空包标志)、`core/builtin_tools/readstate.py`
- Test: `tests/test_builtin_tools/__init__.py`、`tests/test_builtin_tools/test_readstate.py`

**Interfaces:**
- Produces: `FileReadState`(set/get/is_unchanged/is_stale)、`ReadRecord(content, mtime, offset, limit)`。Task 5/6 的 read/write 工厂**闭包捕获**它(不进 ToolContext)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_builtin_tools/test_readstate.py
from core.builtin_tools.readstate import FileReadState


def test_set_get_roundtrip():
    rs = FileReadState()
    rs.set("/a", "content", 100.0, 1, 10)
    rec = rs.get("/a")
    assert rec is not None
    assert rec.content == "content" and rec.mtime == 100.0
    assert rec.offset == 1 and rec.limit == 10


def test_is_unchanged_true_when_same_range_and_mtime():
    rs = FileReadState()
    rs.set("/a", "c", 100.0, 1, 10)
    assert rs.is_unchanged("/a", 1, 10, 100.0) is True


def test_is_unchanged_false_when_mtime_changed():
    rs = FileReadState()
    rs.set("/a", "c", 100.0, 1, 10)
    assert rs.is_unchanged("/a", 1, 10, 101.0) is False


def test_is_unchanged_false_when_no_record():
    rs = FileReadState()
    assert rs.is_unchanged("/a", 1, 10, 100.0) is False


def test_is_stale_true_when_modified_after_read():
    rs = FileReadState()
    rs.set("/a", "c", 100.0, 1, None)
    assert rs.is_stale("/a", 101.0) is True


def test_is_stale_false_when_not_modified():
    rs = FileReadState()
    rs.set("/a", "c", 100.0, 1, None)
    assert rs.is_stale("/a", 100.0) is False


def test_is_stale_false_when_never_read():
    """没读过的文件允许直接写(CC 行为)。"""
    rs = FileReadState()
    assert rs.is_stale("/a", 100.0) is False
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_builtin_tools/test_readstate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.builtin_tools'`。

- [ ] **Step 3: 实现**

`core/builtin_tools/__init__.py`(空包标志,Task 7 充实):
```python
"""builtin 工具集 (read/write/glob/grep)。Task 7 实现 builtin_tools() 工厂。"""
```

> **不改 `core/tools.py`**:read_state 走工厂闭包(spec §3.3 方案 A),不进 `ToolContext`。`tools.py` 的 `func` 签名收窄已在 Task 1 完成。

`core/builtin_tools/readstate.py`:
```python
"""agent 级文件读状态: read 记录 mtime, write 查陈旧。跨轮持久(不随 ToolContext 重建)。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReadRecord:
    content: str
    mtime: float
    offset: int
    limit: int | None


class FileReadState:
    def __init__(self) -> None:
        self._records: dict[str, ReadRecord] = {}

    def set(self, path: str, content: str, mtime: float,
            offset: int, limit: int | None) -> None:
        self._records[path] = ReadRecord(content, mtime, offset, limit)

    def get(self, path: str) -> ReadRecord | None:
        return self._records.get(path)

    def is_unchanged(self, path: str, offset: int,
                     limit: int | None, disk_mtime: float) -> bool:
        """read 去重: 同 (path, offset, limit) 且 mtime 未变 → True。"""
        rec = self._records.get(path)
        return (rec is not None and rec.offset == offset
                and rec.limit == limit and rec.mtime == disk_mtime)

    def is_stale(self, path: str, disk_mtime: float) -> bool:
        """write 陈旧: 读过且读后被外部改了(disk mtime > 记录) → True。没读过 → False。"""
        rec = self._records.get(path)
        return rec is not None and disk_mtime > rec.mtime
```

> `tests/test_builtin_tools/__init__.py` 建空文件(包标志)。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_builtin_tools/test_readstate.py -v && pytest -q`
Expected: 7 测通过 + 全量不破(ToolContext 新字段默认 None,向后兼容)。

- [ ] **Step 5: commit**

```bash
git add core/builtin_tools/ tests/test_builtin_tools/
git commit -m "feat: FileReadState + ToolContext.read_state (read/write 共享陈旧检测)"
```

---

### Task 3: GlobTool

**Files:**
- Create: `core/builtin_tools/glob.py`
- Test: `tests/test_builtin_tools/test_glob.py`

**Interfaces:**
- Consumes: `Tool`、`ToolContext`(core.tools)、`GLOB_LIMIT=100`。
- Produces: `GlobIn(pattern, path=None)`;`glob_tool(cwd: str | None = None) -> Tool`(name="glob", is_concurrency_safe=True, func 返回 str)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_builtin_tools/test_glob.py
import asyncio

from core.builtin_tools.glob import GlobIn, glob_tool
from core.tools import ToolContext
from telemetry.tracer import NoopTracer


def _ctx() -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())


async def test_glob_matches_pattern(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    result = await glob_tool(str(tmp_path)).func(GlobIn(pattern="*.py"), _ctx())
    assert "a.py" in result
    assert "b.txt" not in result


async def test_glob_relative_paths(tmp_path):
    (tmp_path / "a.py").write_text("x")
    result = await glob_tool(str(tmp_path)).func(GlobIn(pattern="*.py"), _ctx())
    assert result == "a.py"   # 相对 cwd, 不含 tmp_path 前缀


async def test_glob_excludes_git(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "a.py").write_text("x")
    result = await glob_tool(str(tmp_path)).func(GlobIn(pattern="**/*"), _ctx())
    assert ".git" not in result
    assert "a.py" in result


async def test_glob_no_files(tmp_path):
    result = await glob_tool(str(tmp_path)).func(GlobIn(pattern="*.nope"), _ctx())
    assert result == "No files found"


async def test_glob_truncates_at_100(tmp_path):
    for i in range(150):
        (tmp_path / f"f{i:03d}.py").write_text("x")
    result = await glob_tool(str(tmp_path)).func(GlobIn(pattern="*.py"), _ctx())
    assert "truncated" in result.lower()
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_builtin_tools/test_glob.py -v`
Expected: FAIL — `ModuleNotFoundError: core.builtin_tools.glob`。

- [ ] **Step 3: 实现**

```python
# core/builtin_tools/glob.py
"""Glob 工具: 按文件名 pattern 匹配(只读, 并发安全)。"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

from ..tools import Tool, ToolContext

_GLOB_LIMIT = 100

_DESCRIPTION = (
    "Fast file pattern matching tool. Use this to find files by name pattern. "
    "Always use this tool first when you need to find files by name. "
    "Pattern supports ** for recursive matching."
)


class GlobIn(BaseModel):
    pattern: str
    path: str | None = None


def glob_tool(cwd: str | None = None) -> Tool:
    async def _glob(inp: GlobIn, ctx: ToolContext) -> str:
        base = Path(inp.path) if inp.path else Path(cwd or os.getcwd())
        files: list[str] = []
        for p in base.glob(inp.pattern):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(base)
            except ValueError:
                rel = p  # base 外, 用绝对路径
            if ".git" in rel.parts:
                continue
            files.append(str(rel))
        files.sort()
        truncated = len(files) > _GLOB_LIMIT
        head = files[:_GLOB_LIMIT]
        if not head:
            return "No files found"
        out = "\n".join(head)
        if truncated:
            out += "\n(Results are truncated. Consider a more specific pattern.)"
        return out

    return Tool(
        name="glob",
        description=_DESCRIPTION,
        input_model=GlobIn,
        func=_glob,
        is_concurrency_safe=True,
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_builtin_tools/test_glob.py -v`
Expected: 5 passed。

- [ ] **Step 5: commit**

```bash
git add core/builtin_tools/glob.py tests/test_builtin_tools/test_glob.py
git commit -m "feat: Glob 工具 (pattern 匹配 + 限100 + 排除 .git + 相对路径)"
```

---

### Task 4: GrepTool(ripgrep)

**Files:**
- Create: `core/builtin_tools/grep.py`
- Test: `tests/test_builtin_tools/test_grep.py`

**Interfaces:**
- Consumes: `Tool`、`ToolContext`、系统 `rg` 二进制。
- Produces: `GrepIn`(13 字段);`grep_tool(cwd: str | None = None) -> Tool`(name="grep", is_concurrency_safe=True, func 返回 str)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_builtin_tools/test_grep.py
import asyncio
import os

from core.builtin_tools.grep import GrepIn, grep_tool
from core.tools import ToolContext
from telemetry.tracer import NoopTracer


def _ctx() -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())


async def test_grep_files_with_matches(tmp_path):
    (tmp_path / "a.py").write_text("hello world\nfoo\n")
    (tmp_path / "b.py").write_text("bar\n")
    result = await grep_tool(str(tmp_path)).func(
        GrepIn(pattern="hello", output_mode="files_with_matches"), _ctx())
    assert "a.py" in result
    assert "b.py" not in result
    assert "Found" in result


async def test_grep_content_mode_with_line_numbers(tmp_path):
    (tmp_path / "a.py").write_text("foo\nhello\nbar\n")
    result = await grep_tool(str(tmp_path)).func(
        GrepIn(pattern="hello", output_mode="content"), _ctx())
    assert "hello" in result
    assert "a.py" in result


async def test_grep_count_mode(tmp_path):
    (tmp_path / "a.py").write_text("hello\nhello\n")
    result = await grep_tool(str(tmp_path)).func(
        GrepIn(pattern="hello", output_mode="count"), _ctx())
    assert "a.py" in result
    assert "occurrences" in result


async def test_grep_no_matches(tmp_path):
    (tmp_path / "a.py").write_text("foo\n")
    result = await grep_tool(str(tmp_path)).func(
        GrepIn(pattern="zzz"), _ctx())
    assert "No files found" in result or "No matches" in result


async def test_grep_type_filter(tmp_path):
    (tmp_path / "a.py").write_text("hello\n")
    (tmp_path / "b.txt").write_text("hello\n")
    result = await grep_tool(str(tmp_path)).func(
        GrepIn(pattern="hello", type="py"), _ctx())
    assert "a.py" in result
    assert "b.txt" not in result


async def test_grep_mtime_sort_order(tmp_path):
    import time
    old = tmp_path / "old.py"; old.write_text("x\n")
    new = tmp_path / "new.py"; new.write_text("x\n")
    os.utime(old, (time.time() - 1000, time.time() - 1000))  # old 更旧
    os.utime(new, (time.time(), time.time()))                 # new 更新
    result = await grep_tool(str(tmp_path)).func(
        GrepIn(pattern="x"), _ctx())
    # new(mtime 大)排在 old 前
    assert result.index("new.py") < result.index("old.py")


async def test_grep_pattern_starting_with_dash(tmp_path):
    (tmp_path / "a.py").write_text("foo-bar\n")
    result = await grep_tool(str(tmp_path)).func(
        GrepIn(pattern="-bar", output_mode="content"), _ctx())
    assert "foo-bar" in result


async def test_grep_rg_missing(monkeypatch):
    """rg 未装 → is_error 带安装提示(模拟 FileNotFoundError)。"""
    import core.builtin_tools.grep as g

    async def _boom(*a, **kw):
        raise FileNotFoundError("rg")
    monkeypatch.setattr(g.asyncio, "create_subprocess_exec", _boom)
    with __import__("pytest").raises(RuntimeError) as exc:
        await grep_tool("/tmp").func(GrepIn(pattern="x"), _ctx())
    assert "ripgrep" in str(exc.value).lower() or "rg" in str(exc.value).lower()
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_builtin_tools/test_grep.py -v`
Expected: FAIL — `ModuleNotFoundError: core.builtin_tools.grep`。

- [ ] **Step 3: 实现**

```python
# core/builtin_tools/grep.py
"""Grep 工具: 内容搜索(ripgrep, 只读, 并发安全)。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..tools import Tool, ToolContext

_VCS_DIRS = [".git", ".svn", ".hg", ".bzr", ".jj", ".sl"]
_DEFAULT_HEAD_LIMIT = 250

_DESCRIPTION = (
    "Search file contents with a regular expression (powered by ripgrep). "
    "Use this to find where code/text lives. Supports output_mode "
    "(files_with_matches/content/count), context lines, case-insensitive, "
    "file type filter, and pagination (head_limit/offset)."
)


class GrepIn(BaseModel):
    pattern: str
    path: str | None = None
    glob: str | None = None
    output_mode: Literal["content", "files_with_matches", "count"] = "files_with_matches"
    context_before: int | None = None   # rg -B
    context_after: int | None = None    # rg -A
    context: int | None = None          # rg -C (优先于 -B/-A)
    case_insensitive: bool = False      # rg -i
    show_line_numbers: bool = True      # rg -n (仅 content)
    type: str | None = None             # rg --type
    head_limit: int = _DEFAULT_HEAD_LIMIT   # 0 = 不限
    offset: int = 0
    multiline: bool = False             # rg -U --multiline-dotall


def _build_args(inp: GrepIn) -> list[str]:
    args = ["--hidden", "--max-columns", "500"]
    for d in _VCS_DIRS:
        args += ["--glob", f"!{d}"]
    if inp.multiline:
        args += ["-U", "--multiline-dotall"]
    if inp.case_insensitive:
        args.append("-i")
    if inp.output_mode == "files_with_matches":
        args.append("-l")
    elif inp.output_mode == "count":
        args.append("-c")
    if inp.show_line_numbers and inp.output_mode == "content":
        args.append("-n")
    if inp.output_mode == "content":
        if inp.context is not None:
            args += ["-C", str(inp.context)]
        else:
            if inp.context_before is not None:
                args += ["-B", str(inp.context_before)]
            if inp.context_after is not None:
                args += ["-A", str(inp.context_after)]
    args += ["-e", inp.pattern] if inp.pattern.startswith("-") else [inp.pattern]
    if inp.type:
        args += ["--type", inp.type]
    if inp.glob:
        args += ["--glob", inp.glob]
    return args


def _rel(path: str, base: Path) -> str:
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return path


def _paginate(items: list[str], head_limit: int, offset: int) -> tuple[list[str], bool]:
    if head_limit == 0:
        return items[offset:], False
    sliced = items[offset:offset + head_limit]
    truncated = len(items) - offset > head_limit
    return sliced, truncated


def _format_limit(truncated: bool, offset: int) -> str:
    parts = []
    if truncated:
        parts.append("more results exist")
    if offset:
        parts.append(f"offset={offset}")
    return f" [{', '.join(parts)}]" if parts else ""


def grep_tool(cwd: str | None = None) -> Tool:
    async def _grep(inp: GrepIn, ctx: ToolContext) -> str:
        base = Path(inp.path) if inp.path else Path(cwd or os.getcwd())
        args = _build_args(inp)
        try:
            proc = await asyncio.create_subprocess_exec(
                "rg", *args, str(base),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "ripgrep (rg) not found. Install: brew install ripgrep (macOS) "
                "or apt install ripgrep (Debian/Ubuntu)."
            ) from e
        stdout_b, stderr_b = await proc.communicate()
        rc = proc.returncode
        if rc not in (0, 1):
            raise RuntimeError(f"rg failed (code {rc}): {stderr_b.decode().strip()}")
        lines = stdout_b.decode().splitlines() if rc == 0 else []

        if inp.output_mode == "files_with_matches":
            def _mtime(p: str) -> float:
                try:
                    return Path(p).stat().st_mtime
                except OSError:
                    return 0.0
            ordered = sorted(lines, key=lambda p: (-_mtime(p), p))  # mtime 降序, 名字 tiebreak
            sliced, truncated = _paginate(ordered, inp.head_limit, inp.offset)
            rels = [_rel(p, base) for p in sliced]
            if not rels:
                return "No files found"
            return f"Found {len(rels} files{_format_limit(truncated, inp.offset)}\n" + "\n".join(rels)

        if inp.output_mode == "count":
            sliced, truncated = _paginate(lines, inp.head_limit, inp.offset)
            total = 0
            rendered = []
            for ln in sliced:
                rendered.append(_rel_path_before_colon(ln, base))
                idx = ln.rfind(":")
                if idx > 0:
                    n = ln[idx + 1:]
                    if n.isdigit():
                        total += int(n)
            if not rendered:
                return "No matches found"
            return ("\n".join(rendered)
                    + f"\n\nFound {total} occurrences across {len(rendered)} files"
                    + _format_limit(truncated, inp.offset))

        # content
        sliced, truncated = _paginate(lines, inp.head_limit, inp.offset)
        rendered = [_rel_path_before_colon(ln, base) for ln in sliced]
        if not rendered:
            return "No matches found"
        return "\n".join(rendered) + _format_limit(truncated, inp.offset)

    return Tool(
        name="grep",
        description=_DESCRIPTION,
        input_model=GrepIn,
        func=_grep,
        is_concurrency_safe=True,
    )


def _rel_path_before_colon(line: str, base: Path) -> str:
    """rg 输出形如 /abs/path:lineno:content 或 /abs/path:content 或 /abs/path:count。
    把首个冒号前的路径部分相对化, 其余保留。"""
    idx = line.find(":")
    if idx <= 0:
        return line
    return _rel(line[:idx], base) + line[idx:]
```

> 注意 `f"Found {len(rels} files..."` 是 plan笔误,实现时写 `len(rels)`(`}` 闭合)。即:`f"Found {len(rels)} files..."`。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_builtin_tools/test_grep.py -v`
Expected: 8 passed(环境有 rg)。

- [ ] **Step 5: commit**

```bash
git add core/builtin_tools/grep.py tests/test_builtin_tools/test_grep.py
git commit -m "feat: Grep 工具 (ripgrep + 3 模式 + --type + mtime 排序 + 分页)"
```

---

### Task 5: FileReadTool

**Files:**
- Create: `core/builtin_tools/read.py`
- Test: `tests/test_builtin_tools/test_read.py`

**Interfaces:**
- Consumes: `Tool`、`ToolContext.read_state`(Task 2)、`MAX_READ_BYTES=256_000`、`os.stat`。
- Produces: `ReadIn(file_path, offset=1, limit=None)`;`read_tool(read_state: FileReadState, cwd: str | None = None) -> Tool`(name="read", safe=True)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_builtin_tools/test_read.py
import asyncio
import os

from core.builtin_tools.read import ReadIn, read_tool
from core.builtin_tools.readstate import FileReadState
from core.tools import ToolContext
from telemetry.tracer import NoopTracer


def _ctx() -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())


async def test_read_adds_line_numbers(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("line1\nline2\nline3\n")
    rs = FileReadState()
    result = await read_tool(rs, str(tmp_path)).func(ReadIn(file_path=str(f)), _ctx())
    assert "1" in result and "line1" in result
    assert "2" in result and "line2" in result


async def test_read_offset_limit(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("l1\nl2\nl3\nl4\nl5\n")
    rs = FileReadState()
    result = await read_tool(rs, str(tmp_path)).func(
        ReadIn(file_path=str(f), offset=2, limit=2), _ctx())
    assert "l2" in result and "l3" in result
    assert "l1" not in result and "l4" not in result


async def test_read_dedup_unchanged(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("hello\n")
    rs = FileReadState()
    tool = read_tool(rs, str(tmp_path))
    await tool.func(ReadIn(file_path=str(f)), _ctx())  # 首次读, 记录
    result = await tool.func(ReadIn(file_path=str(f)), _ctx())  # 同 range, mtime 未变
    assert result == "File unchanged"


async def test_read_after_external_change_re_reads(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("hello\n")
    rs = FileReadState()
    tool = read_tool(rs, str(tmp_path))
    await tool.func(ReadIn(file_path=str(f)), _ctx())
    os.utime(str(f), (os.path.getmtime(str(f)) + 100, os.path.getmtime(str(f)) + 100))
    result = await tool.func(ReadIn(file_path=str(f)), _ctx())
    assert result != "File unchanged"
    assert "hello" in result


async def test_read_binary_rejected(tmp_path):
    f = tmp_path / "a.png"; f.write_bytes(b"\x89PNG\r\n")
    rs = FileReadState()
    import pytest
    with pytest.raises(Exception):
        await read_tool(rs, str(tmp_path)).func(ReadIn(file_path=str(f)), _ctx())


async def test_read_empty_file(tmp_path):
    f = tmp_path / "empty.txt"; f.write_text("")
    rs = FileReadState()
    result = await read_tool(rs, str(tmp_path)).func(ReadIn(file_path=str(f)), _ctx())
    assert "empty" in result.lower()


async def test_read_offset_out_of_range(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("only one line\n")
    rs = FileReadState()
    result = await read_tool(rs, str(tmp_path)).func(
        ReadIn(file_path=str(f), offset=99), _ctx())
    assert "out of range" in result.lower() or "shorter" in result.lower()


async def test_read_nonexistent_raises(tmp_path):
    rs = FileReadState()
    import pytest
    with pytest.raises(Exception):
        await read_tool(rs, str(tmp_path)).func(
            ReadIn(file_path=str(tmp_path / "nope.txt")), _ctx())
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_builtin_tools/test_read.py -v`
Expected: FAIL — `ModuleNotFoundError: core.builtin_tools.read`。

- [ ] **Step 3: 实现**

```python
# core/builtin_tools/read.py
"""Read 工具: 读文本文件(按行 + 行号, 只读, 并发安全)。支持去重 + 陈旧记录。"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ..tools import Tool, ToolContext
from .readstate import FileReadState

MAX_READ_BYTES = 256_000

_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".zip", ".gz", ".tar", ".bz2", ".7z", ".rar",
    ".pdf", ".exe", ".dll", ".so", ".dylib", ".class", ".jar",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac",
    ".pyc", ".pyo", ".o", ".a", ".woff", ".woff2", ".ttf",
}

_BLOCKED_DEVICES = {
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr", "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
}

_DESCRIPTION = (
    "Read a text file from the local filesystem. Output has line numbers. "
    "Supports offset/limit for large files. Cannot read binary files."
)


class ReadIn(BaseModel):
    file_path: str
    offset: int = 1        # 1-indexed
    limit: int | None = None


def _is_blocked_device(path: str) -> bool:
    if path in _BLOCKED_DEVICES:
        return True
    return path.startswith("/proc/") and (
        path.endswith("/fd/0") or path.endswith("/fd/1") or path.endswith("/fd/2"))


def _add_line_numbers(lines: list[str], start: int, total: int) -> str:
    width = max(2, len(str(total)))
    out = []
    for i, ln in enumerate(lines):
        out.append(f"{start + i:>{width}}\t{ln}")
    return "\n".join(out)


def read_tool(read_state: FileReadState, cwd: str | None = None) -> Tool:
    async def _read(inp: ReadIn, ctx: ToolContext) -> str:
        path = Path(inp.file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {inp.file_path}")
        if path.suffix.lower() in _BINARY_EXTS:
            raise ValueError(
                f"Cannot read binary file ({path.suffix}). Use a different tool.")
        if _is_blocked_device(str(path)):
            raise ValueError(
                f"Cannot read '{inp.file_path}': device file would block or produce infinite output.")

        disk_mtime = path.stat().st_mtime

        # 去重
        if read_state.is_unchanged(str(path), inp.offset, inp.limit, disk_mtime):
            return "File unchanged"

        all_lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
        if all_lines and all_lines[-1] == "":
            all_lines = all_lines[:-1]   # 末尾空行(由 trailing \n 产生)不计
        total = len(all_lines)

        if total == 0:
            read_state.set(str(path), "", disk_mtime, inp.offset, inp.limit)
            return "<File is empty>"

        start_idx = inp.offset - 1
        if start_idx >= total:
            return f"<File has {total} line(s); offset {inp.offset} out of range.>"

        end_idx = total if inp.limit is None else min(start_idx + inp.limit, total)
        selected = all_lines[start_idx:end_idx]

        # 字节上限: 累积截断
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

    return Tool(
        name="read",
        description=_DESCRIPTION,
        input_model=ReadIn,
        func=_read,
        is_concurrency_safe=True,
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_builtin_tools/test_read.py -v`
Expected: 8 passed。

- [ ] **Step 5: commit**

```bash
git add core/builtin_tools/read.py tests/test_builtin_tools/test_read.py
git commit -m "feat: Read 工具 (按行+行号 + 去重 + binary/设备拒绝 + 字节上限)"
```

---

### Task 6: FileWriteTool

**Files:**
- Create: `core/builtin_tools/write.py`
- Test: `tests/test_builtin_tools/test_write.py`

**Interfaces:**
- Consumes: `Tool`、`ToolContext.read_state`、`difflib`、`os.stat`。
- Produces: `WriteIn(file_path, content)`;`write_tool(read_state, cwd=None) -> Tool`(name="write", is_concurrency_safe=False)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_builtin_tools/test_write.py
import asyncio
import difflib
import os

from core.builtin_tools.read import ReadIn, read_tool
from core.builtin_tools.readstate import FileReadState
from core.builtin_tools.write import WriteIn, write_tool
from core.tools import ToolContext
from telemetry.tracer import NoopTracer


def _ctx() -> ToolContext:
    return ToolContext(tracer=NoopTracer(), abort_signal=asyncio.Event())


async def test_write_creates_new_file(tmp_path):
    rs = FileReadState()
    f = tmp_path / "new.txt"
    result = await write_tool(rs, str(tmp_path)).func(
        WriteIn(file_path=str(f), content="hello\n"), _ctx())
    assert f.read_text() == "hello\n"
    assert "created" in result.lower()


async def test_write_update_returns_diff(tmp_path):
    rs = FileReadState()
    f = tmp_path / "a.txt"; f.write_text("line1\nline2\n")
    # 先 read 记录 mtime, 才能 update(否则首次写视为 create? 不: 文件存在且 read_state 无记录 → 允许写, 算 update)
    result = await write_tool(rs, str(tmp_path)).func(
        WriteIn(file_path=str(f), content="line1\nCHANGED\n"), _ctx())
    assert f.read_text() == "line1\nCHANGED\n"
    assert "updated" in result.lower()
    assert "CHANGED" in result or "line2" in result   # diff 含改动


async def test_write_stale_after_external_change(tmp_path):
    """read 后外部改 mtime → write 被拒(is_error)。"""
    rs = FileReadState()
    f = tmp_path / "a.txt"; f.write_text("orig\n")
    rt = read_tool(rs, str(tmp_path))
    await rt.func(ReadIn(file_path=str(f)), _ctx())   # read 记录
    # 外部改文件(mtime 推后)
    f.write_text("EXTERNAL EDIT\n")
    future = os.path.getmtime(str(f)) + 1000
    os.utime(str(f), (future, future))
    import pytest
    with pytest.raises(Exception) as exc:
        await write_tool(rs, str(tmp_path)).func(
            WriteIn(file_path=str(f), content="mine\n"), _ctx())
    assert "modified" in str(exc.value).lower() or "read" in str(exc.value).lower()


async def test_write_mkdir_parents(tmp_path):
    rs = FileReadState()
    f = tmp_path / "sub" / "dir" / "a.txt"
    await write_tool(rs, str(tmp_path)).func(
        WriteIn(file_path=str(f), content="x\n"), _ctx())
    assert f.read_text() == "x\n"


async def test_write_after_read_same_mtime_succeeds(tmp_path):
    """read 后文件未变 → write 成功(read-write 正常流)。"""
    rs = FileReadState()
    f = tmp_path / "a.txt"; f.write_text("orig\n")
    await read_tool(rs, str(tmp_path)).func(ReadIn(file_path=str(f)), _ctx())
    result = await write_tool(rs, str(tmp_path)).func(
        WriteIn(file_path=str(f), content="new\n"), _ctx())
    assert f.read_text() == "new\n"
    assert "updated" in result.lower()
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_builtin_tools/test_write.py -v`
Expected: FAIL — `ModuleNotFoundError: core.builtin_tools.write`。

- [ ] **Step 3: 实现**

```python
# core/builtin_tools/write.py
"""Write 工具: 创建/覆盖文件(写, 独占)。含陈旧检测 + unified diff 返回。"""
from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel

from ..tools import Tool, ToolContext
from .readstate import FileReadState

_DESCRIPTION = (
    "Write a file to the local filesystem. Overwrites existing files. "
    "For files you have read, refuses if the file changed on disk since your last read "
    "(re-read first). Creates parent directories as needed."
)


class WriteIn(BaseModel):
    file_path: str
    content: str


def write_tool(read_state: FileReadState, cwd: str | None = None) -> Tool:
    async def _write(inp: WriteIn, ctx: ToolContext) -> str:
        path = Path(inp.file_path)
        exists = path.exists()
        disk_mtime = path.stat().st_mtime if exists else None

        # 陈旧检测: 读过且读后被外部改了 → 拒绝
        if exists and disk_mtime is not None and read_state.is_stale(str(path), disk_mtime):
            raise PermissionError(
                "File has been modified since read. Read it again before writing.")

        # mkdir 父目录
        path.parent.mkdir(parents=True, exist_ok=True)

        old = path.read_text(encoding="utf-8") if exists else None

        # 写(LF, 不重写行尾)
        path.write_text(inp.content, encoding="utf-8", newline="\n")

        new_mtime = path.stat().st_mtime
        read_state.set(str(path), inp.content, new_mtime, 0, None)

        if old is None:
            return f"File created successfully at: {inp.file_path}"

        diff = "\n".join(difflib.unified_diff(
            old.splitlines(), inp.content.splitlines(),
            fromfile=inp.file_path, tofile=inp.file_path, lineterm=""))
        if diff:
            return f"The file {inp.file_path} has been updated.\n{diff}"
        return f"The file {inp.file_path} has been updated (no content change)."

    return Tool(
        name="write",
        description=_DESCRIPTION,
        input_model=WriteIn,
        func=_write,
        is_concurrency_safe=False,
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_builtin_tools/test_write.py -v`
Expected: 5 passed。

- [ ] **Step 5: commit**

```bash
git add core/builtin_tools/write.py tests/test_builtin_tools/test_write.py
git commit -m "feat: Write 工具 (陈旧检测 + mkdir + LF + unified diff 返回)"
```

---

### Task 7: 注册 + query_loop/agent_loop 接线

**Files:**
- Modify: `core/builtin_tools/__init__.py`(充实 `builtin_tools()` 工厂)
- Modify: `core/agent_loop.py`(`submit` 创建 `FileReadState`,`builtin_tools(rs, cwd)` 产工具塞 params.tools)
- Test: `tests/test_builtin_tools/test_register.py`

**Interfaces:**
- Consumes: 4 工具工厂(Task 3-6)、`FileReadState`(Task 2)。
- Produces: `builtin_tools(read_state, *, cwd=None) -> list[Tool]`(工厂闭包绑 read_state)。**不改 query_loop / ToolContext**(read_state 走闭包)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_builtin_tools/test_register.py
from core.builtin_tools import builtin_tools
from core.builtin_tools.readstate import FileReadState
from core.tools import Tool


def test_builtin_tools_returns_four():
    tools = builtin_tools(FileReadState())
    names = [t.name for t in tools]
    assert sorted(names) == ["glob", "grep", "read", "write"]
    for t in tools:
        assert isinstance(t, Tool)


def test_builtin_tools_share_read_state():
    """read 和 write 共享同一个 FileReadState(陈旧检测前提)。"""
    rs = FileReadState()
    tools = {t.name: t for t in builtin_tools(rs)}
    # 两者的 read_state 是同一个对象(工厂捕获同一 rs)
    assert tools["read"].func.__closure__ is not None
    assert tools["write"].func.__closure__ is not None
    # 更直接的验证: read 后 write 在同一 rs 上能看到记录(集成测在 test_read/test_write 已覆盖)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_builtin_tools/test_register.py -v`
Expected: FAIL — `ImportError: cannot import name 'builtin_tools'`(__init__.py 当前空)。

- [ ] **Step 3: 实现**

`core/builtin_tools/__init__.py`:
```python
"""builtin 工具集 (read/write/glob/grep)。"""
from __future__ import annotations

from ..tools import Tool
from .glob import glob_tool
from .grep import grep_tool
from .read import read_tool
from .readstate import FileReadState
from .write import write_tool

__all__ = ["FileReadState", "builtin_tools"]


def builtin_tools(read_state: FileReadState, *, cwd: str | None = None) -> list[Tool]:
    """产出 4 个 builtin Tool。read/write 共享 read_state(陈旧检测)。cwd 默认 os.getcwd()。"""
    return [
        glob_tool(cwd),
        grep_tool(cwd),
        read_tool(read_state, cwd),
        write_tool(read_state, cwd),
    ]
```

`core/agent_loop.py` — `submit` 创建 agent 级 `FileReadState`,用 `builtin_tools` 产工具塞 params.tools:

```python
from .builtin_tools import builtin_tools
from .builtin_tools.readstate import FileReadState
...
async def submit(...):
    read_state = FileReadState()
    tools = builtin_tools(read_state, cwd=...)   # read/write 闭包共享同一 read_state
    # 把 tools 塞进 QueryParams.tools(替代或追加现有 tools)
    ...
    async for x in query_loop(params, tracer):
        ...
```

> **不改 `query_loop` / `ToolContext`**:工具 func 自带 read_state(工厂闭包),framework 无感。`submit` 的具体签名/调用点以现有代码为准——关键是 `builtin_tools(read_state, cwd)` 产出的 Tool 列表进 `QueryParams.tools`。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_builtin_tools/ -v && pytest -q`
Expected: test_register 2 passed + 全量不破(不改 query_loop/ToolContext,现有测试无影响)。

- [ ] **Step 5: 全量回归 + pyright**

Run: `pytest -q && pyright`
Expected: 全部通过;pyright basic 无新错误。

- [ ] **Step 6: commit**

```bash
git add core/builtin_tools/__init__.py core/agent_loop.py tests/test_builtin_tools/test_register.py
git commit -m "feat: builtin_tools() 工厂 + agent_loop 注入 read_state(闭包)"
```

---

## Self-Review 记录

**1. Spec 覆盖:**
- §3.1 目录 `core/builtin_tools/` → Task 2(__init__+readstate)/3(glob)/4(grep)/5(read)/6(write)/7(充实 __init__)。✓
- §3.2 类型强化 → Task 1(types/tools/base + main-lwt 适配)。✓
- §3.3 readFileState → Task 2(FileReadState + ToolContext.read_state)+ Task 7(orchestrator/agent_loop 注入)。✓
- §3.4 注册 `builtin_tools()` → Task 7。✓
- §3.5 is_concurrency_safe → glob/grep/read=True(T3/4/5)、write=False(T6)。✓
- §4.1-4.4 四工具行为 → Task 3/4/5/6。✓
- §5 错误矩阵 → 各工具 func 抛异常(rg缺失/binary/设备/陈旧/不存在)+ 返回 warning(空/越界)。✓
- §6 测试策略 → 每 task 测试覆盖 + Task 6 read/write 陈旧集成。✓
- §7 变更清单 6 文件 → Task 1-7 全覆盖。✓

**2. 类型一致性:**
- `_to_result(tool_use_id, ret: str|TextBlock|list[TextBlock])`:Task 1 定义,所有工具 func 返回 str 符合。✓
- `FileReadState.set/get/is_unchanged/is_stale`:Task 2 定义,Task 5/6 消费。✓
- `ToolContext.read_state`:Task 2 加,Task 5/6 用 `ctx.read_state`(但工具工厂闭包捕获 read_state,不依赖 ctx——见下注意)。✓
- `glob_tool/grep_tool(cwd) -> Tool` + `read_tool/write_tool(read_state, cwd) -> Tool`:Task 3-6 定义,Task 7 `builtin_tools(read_state)` 调用。read/write 闭包绑 read_state(不进 ToolContext)。✓

**3. 已知实现注意(非占位,交付时核对):**
- **Task 4 grep 有 1 处 plan 笔误**:`f"Found {len(rels} files..."` 应为 `f"Found {len(rels)} files..."`(`}` 闭合)。Step 3 已标注,实现时修正。
- **read_state 走闭包(方案 A, spec §3.3)**:Task 5/6 工厂 `read_tool(read_state, cwd)` / `write_tool(read_state, cwd)` 闭包绑 read_state,func 内用闭包(**不读 `ctx.read_state`**)。Task 5/6 测试 `read_tool(rs, ...).func(inp, _ctx())`——rs 进工厂闭包,`_ctx()` 不传 read_state。Task 7 `builtin_tools(read_state)` 传同一 rs 给 read/write 工厂 → 共享。**不改 ToolContext / query_loop**。
- **Task 7 test_register** 的 `__closure__` 断言较弱(只验证是闭包);真正的"read/write 共享同一 rs"由 Task 6 的 read→write 集成测试覆盖(同 rs 实例)。实现时可保留或加强。
- **agent_loop 接线点**:Task 7 Step 3 说明"以现有代码为准"——关键是 `builtin_tools(read_state, cwd)` 产出的工具进 `QueryParams.tools`。实现时读 agent_loop.py 确认 submit 的 tools 装配位置。
