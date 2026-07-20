# builtin 工具迁移设计 (read / write / glob / grep)

> 日期: 2026-07-17 · 分支: dev/lwt
> 来源: 从 `Claude-Code-best` 的 TS 实现(`packages/builtin-tools/src/tools/*`)迁移 4 个工具到本项目 Python。

## 1. 背景与范围

把 CC 的 4 个 builtin 工具(read/write/glob/grep)迁移到本项目的 `Tool` 框架。CC 原版极度复杂(FileRead 1176 行、依赖 analytics/LSP/skills/fileHistory/图像/PDF 等),本项目是轻量 agent loop,必须大幅取舍。

**范围(实用核心版 + readFileState):**
- 4 工具的**文本核心能力**:glob 文件名匹配、grep 内容搜索(ripgrep)、read 纯文本按行读、write 创建/覆盖。
- 继承 **readFileState**:read 记录读过的文件 mtime,write 写前做**陈旧检测**(读-改-写安全)。
- C 类增强:**read 去重**(file_unchanged stub)、**grep `--type`**、**grep files_with_matches mtime 排序**、**write 返回 diff**。

**明确不做(YAGNI / 无基础设施):**
- read 的 image / PDF / notebook 输出(要 PIL/poppler/ipynb 解析)。
- CC 专有:analytics、LSP 通知、skills 发现、VSCode 通知、fileHistory 备份、team memory secret 检测、permission 系统(文件粒度)、plugin orphaned 排除、memoryFileFreshnessNote、macOS 截图 thin-space 变体。
- grep fallback(ripgrep-only,见 §4.2)。

## 2. 设计原则

1. **不碰工具执行框架**(除两处必要接线:readFileState 注入 + content 类型收窄)。4 工具是纯新增。
2. **func 返回 `str | TextBlock | list[TextBlock]`**——类型层面保证合法 Anthropic content,杜绝任意 dict(顺手修掉 `_to_result` 对 dict 的 broken 处理)。
3. **错误即抛异常**:工具对错误情况(binary/设备/陈旧/rg 失败/文件不存在)抛异常,框架 `_execute_single` 的 `except Exception` 统一转 `is_error` result;正常返回 str。
4. **字段名 Python 风格**(如 `context_before` 而非 `-B`),内部映射到 rg flag。

## 3. 架构

### 3.1 目录:新建 `core/builtin_tools/`(纯新增,不动 `core/tools.py`)

```
core/builtin_tools/
├── __init__.py        # builtin_tools(read_state, *, cwd=None) -> list[Tool]
├── readstate.py       # FileReadState + ReadRecord(跨轮持久)
├── glob.py            # glob_tool(cwd) -> Tool
├── grep.py            # grep_tool(cwd) -> Tool
├── read.py            # read_tool(read_state, cwd) -> Tool
└── write.py           # write_tool(read_state, cwd) -> Tool
```

每个工具一个**工厂函数**(绑 `read_state`/`cwd` 闭包),返回 `Tool`。`__init__.py` 一键产出 4 个。

### 3.2 输出类型强化(框架小改,types/tools/base 三处)

**`core/types.py`** — 收窄 `ToolResultBlock.content`,杜绝任意 dict:

```python
class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str

# tool_result 发给 Anthropic 的合法 content
ToolResultContent = str | list[TextBlock]

class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[TextBlock]   # 收窄: 原 str | list[dict]
    is_error: bool = False
```

> ImageBlock 暂不加(builtin 无图像工具);未来 read 支持图像时扩进联合。

**`core/tools.py`** — func 返回类型收窄:

```python
func: Callable[..., Awaitable[str | TextBlock | list[TextBlock]]]
```

**`core/tool_executor/base.py`** — `_to_result` 归一化三种形态:

```python
def _to_result(tool_use_id: str, ret: str | TextBlock | list[TextBlock]) -> ToolResultBlock:
    if isinstance(ret, str):
        return ToolResultBlock(tool_use_id=tool_use_id, content=ret)
    if isinstance(ret, TextBlock):
        return ToolResultBlock(tool_use_id=tool_use_id, content=[ret])
    return ToolResultBlock(tool_use_id=tool_use_id, content=ret)  # list[TextBlock]
```

**收益**:pyright 拒任意 dict + pydantic 构造兜底 + `to_anthropic` 的 model_dump 产出合法 text block。现有 `main-lwt.py` 的 mock `_fetch`(返回 dict)会类型不合规——它是 `.gitignore` 调试文件,改成返回 str。

### 3.3 readFileState 跨轮持久(唯一的框架接线)

问题:`ToolContext` 在 `query_loop` **每轮新建**,read 记的 mtime 下轮就丢。要让 read/write 共享且跨轮存活。

**`core/builtin_tools/readstate.py`:**

```python
@dataclass
class ReadRecord:
    content: str
    mtime: float
    offset: int
    limit: int | None

class FileReadState:
    """agent 级共享: read 记录, write 查陈旧。跨轮持久(不随 ToolContext 重建)。"""
    def __init__(self) -> None:
        self._records: dict[str, ReadRecord] = {}

    def set(self, path: str, content: str, mtime: float, offset: int, limit: int | None) -> None:
        self._records[path] = ReadRecord(content, mtime, offset, limit)

    def get(self, path: str) -> ReadRecord | None:
        return self._records.get(path)

    def is_unchanged(self, path: str, offset: int, limit: int | None, disk_mtime: float) -> bool:
        """read 去重: 同 (path, offset, limit) 且 mtime 未变 → True。"""
        rec = self._records.get(path)
        return rec is not None and rec.offset == offset and rec.limit == limit and rec.mtime == disk_mtime

    def is_stale(self, path: str, disk_mtime: float) -> bool:
        """write 陈旧: 读过且读后被外部改了(disk mtime > 记录) → True。没读过 → False(允许首次写)。"""
        rec = self._records.get(path)
        return rec is not None and disk_mtime > rec.mtime
```

**接线(工厂闭包,最简——不进 ToolContext):** read_state 由工具**工厂闭包捕获**:`read_tool(read_state, cwd)` / `write_tool(read_state, cwd)` 把 `read_state` 绑进 func 闭包。read 和 write 工厂接收**同一个** `FileReadState`(由 `builtin_tools(read_state, ...)` 传入),从而共享陈旧状态。

- `agent_loop.py`/`submit`:`read_state = FileReadState()`(agent 级,一个),`tools = builtin_tools(read_state, cwd=...)`,塞 `QueryParams.tools`。
- `query_loop` **不改**:工具 func 自带 read_state(闭包),framework 无感。
- `ToolContext` **不改**:read_state 不进 ctx(避免冗余闲置字段;未来若有非 builtin 工具需要,再加)。

### 3.4 工具注册

```python
# core/builtin_tools/__init__.py
from .readstate import FileReadState
from .glob import glob_tool
from .grep import grep_tool
from .read import read_tool
from .write import write_tool

def builtin_tools(read_state: FileReadState, *, cwd: str | None = None) -> list[Tool]:
    """产出 4 个 builtin Tool。cwd 默认 os.getcwd()(可注入, 测试友好)。"""
    return [
        glob_tool(cwd),
        grep_tool(cwd),
        read_tool(read_state, cwd),
        write_tool(read_state, cwd),
    ]
```

`agent_loop`/demo 用 `builtin_tools(read_state)` 拿列表,塞 `QueryParams.tools`。

### 3.5 is_concurrency_safe
glob / read / grep = **True**(只读,可并发);write = **False**(写,独占)。

## 4. 四个工具

### 4.1 GlobTool

- **input**(`GlobIn`):`pattern: str`、`path: str | None = None`(默认 cwd)
- **func**:
  1. `base = Path(path or cwd)`
  2. `matches = sorted(base.glob(pattern))`,排除任何路径段含 `.git` 的
  3. 限 100;超出标 `truncated`
  4. 相对化(转相对 cwd 的路径)
- **返回 str**:`"\n".join(rel_paths)`;空 → `"No files found"`;truncated → 末尾追加 `"(Results are truncated. Consider a more specific pattern.)"`
- **safe=True**

### 4.2 GrepTool(ripgrep-only)

- **input**(`GrepIn`):
  - `pattern: str`
  - `path: str | None = None`
  - `glob: str | None = None`
  - `output_mode: Literal["content","files_with_matches","count"] = "files_with_matches"`
  - `context_before: int | None = None`(→ `-B`)、`context_after: int | None = None`(→ `-A`)、`context: int | None = None`(→ `-C`,优先于前两者)
  - `case_insensitive: bool = False`(→ `-i`)
  - `show_line_numbers: bool = True`(→ `-n`,仅 content 模式)
  - `type: str | None = None`(→ `--type`)
  - `head_limit: int = 250`(`0` = 不限)
  - `offset: int = 0`
  - `multiline: bool = False`(→ `-U --multiline-dotall`)
- **func**:
  1. 拼 rg args:`--hidden`;每个 VCS 目录(`.git/.svn/.hg/.bzr/.jj/.sl`)`--glob '!DIR'`;`--max-columns 500`;模式 flag(`-l`/`-c`/`-n`/`-i`/`-U`);context(`-C` 优先,否则 `-B`/`-A`);`--type`;`--glob`(用户 glob);pattern 开头是 `-` 时用 `-e`。
  2. `await asyncio.create_subprocess_exec("rg", *args, cwd=base, stdout=PIPE, stderr=PIPE)`;`await proc.communicate()`。
  3. rg 退出码:`0`=有匹配,`1`=无匹配,`>1`=错误(抛 `RuntimeError(stderr)` → is_error)。`FileNotFoundError`(rg 未装)→ 抛 `"ripgrep (rg) not found. Install: brew install ripgrep / apt install ripgrep"`(is_error)。
  4. 按 `output_mode` 分发:
     - **files_with_matches**:解析文件列表 → `stat` 每文件 mtime → **按 mtime 降序**(最近改的在前,文件名 tiebreak)→ `head_limit/offset` 分页 → 相对化。
     - **content**:`head_limit/offset` 先分页(避免无谓相对化)→ 每行 `abs/path:rest` 转相对路径。
     - **count**:`head_limit/offset` 分页 → 相对化;累计 `totalMatches`/`fileCount`。
- **返回 str**:
  - files_with_matches:`"Found N files\np1\np2..."` 或 `"No files found"`;分页追加 `[limit: N, offset: M]`。
  - content:匹配行文本 或 `"No matches found"`;分页追加。
  - count:`"path:count\n..."` + `"\n\nFound N occurrences across M files"`。
- **safe=True**

### 4.3 FileReadTool

- **input**(`ReadIn`):`file_path: str`、`offset: int = 1`(1-indexed)、`limit: int | None = None`
- **常量**:`MAX_READ_BYTES = 256_000`
- **func**:
  1. `path = file_path`(工具内用绝对路径;不做 `~` 展开,保持简单)。
  2. **去重**(read_state 存在时):`disk_mtime = path.stat().st_mtime`;`read_state.is_unchanged(path, offset, limit, disk_mtime)` → True 则返回 `"File unchanged"`(C 类)。
  3. **binary 扩展名检测**:扩展名在二进制集合(.png/.jpg/.zip/.exe/.so/...) → 抛 `"binary file, cannot read"`。
  4. **设备文件屏蔽**:`/dev/zero|/dev/random|...` 等路径 → 抛 `"device file would block"`。
  5. **按行读**:读全文 `splitlines(keepends=False)`;取 `offset-1` 起 `limit` 行;累计字节不超过 `MAX_READ_BYTES`(超出截断 + 提示用 offset/limit)。
  6. `read_state.set(path, content, disk_mtime, offset, limit)`。
  7. 返回带行号文本(右对齐,宽度按 `total_lines` 位数,`"\t"` 分隔)。
- **返回 str**:
  - 正常:`"%*d\t%s" % (width, lineno, line)` 逐行。
  - 去重命中:`"File unchanged"`。
  - 空文件:`"<File is empty>"`。
  - offset 越界:`"<File has N lines, offset M out of range>"`。
  - 文件不存在:抛 → is_error `"File not found: ..."`。
- **safe=True**

### 4.4 FileWriteTool

- **input**(`WriteIn`):`file_path: str`、`content: str`
- **func**:
  1. `path = file_path`。
  2. **陈旧检测**:文件存在时 `disk_mtime = path.stat().st_mtime`;`read_state.is_stale(path, disk_mtime)` → True 抛 `"File has been modified since read. Read it again before writing."`(is_error)。
  3. `Path(path).parent.mkdir(parents=True, exist_ok=True)`。
  4. 读旧内容:`old = path.read_text() if path.exists() else None`(注意:mkdir 后、读旧前,文件可能不存在)。
  5. 写:`path.write_text(content, newline="\n")`(LF,不重写行尾,对齐 CC)。
  6. `read_state.set(path, content, path.stat().st_mtime, None, None)`。
- **返回 str**:
  - create(old is None):`"File created successfully at: {file_path}"`。
  - update:**`difflib.unified_diff(old.splitlines(), content.splitlines(), fromfile=file_path, tofile=file_path)`** 渲染(C 类),前缀 `"The file {file_path} has been updated.\n"` + diff 文本。
- **safe=False**(写,独占)

## 5. 错误处理矩阵

| 情况 | 工具行为 | 结果 |
|------|---------|------|
| rg 未装 | `create_subprocess_exec` 抛 `FileNotFoundError` → 抛带安装提示的 RuntimeError | is_error result |
| rg 失败(退出码>1) | 抛 `RuntimeError(stderr)` | is_error result |
| 文件不存在(read) | 抛 `FileNotFoundError`/ValueError | is_error result `"File not found"` |
| binary 扩展名(read) | 抛 ValueError | is_error result |
| 设备文件(read) | 抛 ValueError | is_error result |
| offset 越界 / 空文件(read) | 返回 warning str(不抛) | 正常 result(文本提示) |
| 陈旧(write) | 抛 PermissionError | is_error result `"modified since read"` |
| grep 无匹配(rg 退出码1) | 正常,返回 `"No matches found"`/`"No files found"` | 正常 result |
| 字节超限(read) | 截断 + 文本提示 | 正常 result |

> 工具"抛"→ 框架 `_execute_single except Exception` 转 `is_error=True` 的 `ToolResultBlock`;工具"返回 str"→ 正常 result。模型据此区分工具成败。

## 6. 测试策略(TDD)

每工具独立可测 + read/write 集成。用 `tmp_path` fixture 造临时文件,grep 用真实 `rg`(环境已装)。

- **类型层**:`_to_result` 三形态(str/TextBlock/list);`ToolResultBlock(content=任意dict)` pydantic 校验拒绝;func 返回类型 pyright 合规。
- **glob**:pattern 匹配 + 限100截断 + 排除 .git + 相对化 + "No files found"。
- **grep**:三模式各一例;`--type` 过滤;files_with_matches mtime 排序(造两个不同 mtime 文件验证顺序);head_limit/offset 分页;pattern 开头 `-` 用 `-e`;rg 未装( monkeypatch 抛 FileNotFoundError)→ is_error 带安装提示。
- **read**:行号格式正确;offset/limit 切片;**去重**(同 range + 未变 mtime → "File unchanged";改了 mtime → 重新读);binary 拒绝;设备路径拒绝;空文件/越界 warning;字节上限截断;文件不存在 is_error。
- **write**:create(返回 "created");update(返回含 diff);**陈旧检测**(read 后外部改 mtime → 写被拒 is_error;没读过 → 允许写);mkdir 父目录自动创建。
- **集成**:read 文件 → write 同文件成功(读后 mtime 未变);read 文件 → 外部 touch 改 mtime → write 被拒。
- **readFileState 跨轮**:模拟两轮(两个 ToolContext 共享同一 FileReadState)→ 第二轮 write 能看到第一轮 read 的记录。

## 7. 变更清单(文件级)

| 文件 | 动作 | 内容 |
|------|------|------|
| `core/builtin_tools/` | 新建包 | `__init__.py` + `readstate.py` + `glob.py` + `grep.py` + `read.py` + `write.py` |
| `core/types.py` | 改 | `ToolResultBlock.content` 收窄 `str \| list[TextBlock]`;加 `ToolResultContent` 别名(TextBlock 已有) |
| `core/tools.py` | 改 | `func` 签名收窄 `Awaitable[str \| TextBlock \| list[TextBlock]]` |
| `core/tool_executor/base.py` | 改 | `_to_result` 归一化 str/TextBlock/list |
| `core/agent_loop.py` | 改 | `submit` 创建 `FileReadState`,`builtin_tools(rs, cwd)` 产工具塞 params.tools |

## 8. 权衡与未做

- **ripgrep-only**:rg 未装报错(带安装提示),不做 fallback(避免两套参数映射 + 行为不一致)。环境已装 rg 15.1.0。
- **content 类型收窄**:用类型系统彻底解决"任意 dict 非法 Anthropic content"隐患(比"约定返回 str"更硬),顺手为未来合法 block(text+image)留口子。代价:框架三处小改 + main-lwt mock 适配。
- **readFileState 注入 ToolContext**:唯一的框架接线,read/write 共享必需。非 builtin 工具 `read_state=None` 向后兼容。
- **defer(未来可扩)**:image/PDF/notebook read;permission 系统;read 真 token 估算(现用字节近似);grep fallback;write fileHistory 备份。
- **不修复的已知项**:`main-lwt.py` mock 若仍返回 dict 会类型不合规(它是 .gitignore 调试文件,迁移时一并改成返回 str,但不视为本次 spec 的交付)。
- **write 陈旧检测有 TOCTOU 窗口**: stat() 读 mtime 与 write_text() 之间文件可被外部改, 陈旧检测不会重跑。这是 mtime-based 方案的固有局限(非原子); 真正原子需文件锁(fcntl), 超出本次范围。
