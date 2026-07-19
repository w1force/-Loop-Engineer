# skill 能力设计 (Claude Code 式 skill 包)

> 日期: 2026-07-20 · 分支: feat/skill
> 来源: 对标 Claude Code 的 skill 系统,让 agent 具备可扩展的 skill 能力。
> 基底: feat/streaming-turn(ea212fe,含 streaming + builtin_tools + recovery)。

## 1. 背景与范围

让 agent 具备 CC 式 skill 能力:目录化 skill 包(`SKILL.md` + 只读资源),模型自主决策何时激活,按需加载 skill 指令。对标 CC 的 progressive disclosure(system 放轻量目录,模型决策后调工具拉全文)。

**范围(MVP):**
- **skill 发现**:扫描 `skill_dirs`,解析每个 `SKILL.md` 的 frontmatter。
- **目录注入**:把所有 skill 的 `name + description` 作为 `<skills>` 目录段拼到 system prompt。
- **模型自主决策**:模型看目录决定用哪个 skill(无硬编码匹配)。
- **`load_skill` 工具**:按需加载指定 skill 的 `SKILL.md` 全文(对标 CC progressive disclosure)。
- **只读资源**:skill 目录可带资源文件,模型用现有 `read/glob/grep` 访问。
- **内置 + 外部来源**:固定 `skills/` + `AgentConfig.skill_dirs` 追加外部目录。

**明确不做(YAGNI / 二期):**
- `/name` 显式触发(无命令菜单)。
- skill 专属工具声明(skill 不带新工具,只用现有 builtin)。
- bash / 脚本执行(只读资源,不加 bash 工具)。
- frontmatter 字段:`when_to_use` / `disable-model-invocation` / `user-invocable` / `argument-*` / `allowed-tools` / `disallowed-tools` / `model` / `effort` / `context` / `hooks` / `paths` / `shell`(二期)。
- `description` 字符截断(CC 的 1,536 预算,MVP 不截断)。
- skill 扫描缓存(每次 submit 扫描,未来优化)。

## 2. 设计原则

1. **零改动 loop 内层**:skill 注入是 `submit` 的参数准备,`query_loop` / `tool_executor` / recovery 完全不动。
2. **`load_skill` 是普通 `Tool`**:和 `builtin_tools`(read/write/glob/grep)同级,复用 Tool 机制(并发 / 权限 / transcript / Tombstone)。
3. **模型自主决策**:无硬编码匹配逻辑,模型看目录自己决定。
4. **工厂闭包捕获 metas**:对标 `builtin_tools(read_state)` 模式。
5. **skill 是增强,失败降级**:任何 skill 层面失败 → 无注入 + warn,绝不中断主流程。
6. **frontmatter 宽松解析**:容忍未知字段(向前兼容 CC 新字段)+ 只认 `description`。

## 3. 架构与数据流

### 3.1 集成方案:轻量集成

skill 注入在 `agent_loop.submit` 完成,`query_loop` 内层零改动。

```
submit(prompt, config)
  │
  ├─ read_state = FileReadState()
  ├─ tools = [*config.tools, *builtin_tools(read_state)]
  │
  ├─ metas = SkillLoader.scan(config.skill_dirs)         # 启动扫描
  ├─ if metas:
  │    system = append_catalog(config.system, render_catalog(metas))
  │    tools  = [*tools, load_skill_tool(metas)]
  │
  ├─ QueryParams(system, tools, ...) → query_loop          # 内层不变
  │
  └─ query_loop 执行:
       模型看 system 目录 + tools 里 load_skill
         → 决定用 skill "foo"
         → tool_call: load_skill(name="foo")
         → 返回 skills/foo/SKILL.md 全文
         → 模型按指令工作(可能调 read/grep 访问 skill 资源)
       streaming/recovery 路径完全不变
```

### 3.2 新增包 `core/skills/`

```
core/skills/
├── __init__.py        # 导出 load_skill_tool / SkillLoader / render_catalog / append_catalog / SkillMeta
├── loader.py          # SkillMeta + SkillLoader.scan + render_catalog + append_catalog
└── load_skill.py      # LoadSkillInput + load_skill_tool(metas) -> Tool
```

## 4. 组件详述

### 4.1 `SkillMeta` + `SkillLoader`(`loader.py`)

```python
@dataclass(frozen=True)
class SkillMeta:
    name: str           # = 目录名,skill 标识
    description: str    # frontmatter.description,进 system 目录段
    skill_dir: Path     # skill 目录绝对路径
    skill_md: Path      # SKILL.md 绝对路径(= skill_dir / "SKILL.md")


class SkillLoader:
    @staticmethod
    def scan(skill_dirs: list[str | Path]) -> list[SkillMeta]:
        """扫描所有 skill_dirs 的直接子目录,解析 SKILL.md frontmatter。
        返回按 name 排序的 list[SkillMeta]。空输入/路径不存在 → 返回 [](不抛)。"""
```

扫描规则:
- 每个 `skill_dir` 下的**直接子目录** = skill 候选。
- 子目录有 `SKILL.md` → 解析;无 → 跳过(当普通子目录)。
- frontmatter 缺 `description` → 跳过 + warn(严格)。
- YAML 损坏 → 跳过该 skill + warn。
- 未知 frontmatter 字段 → 宽松容忍(忽略)。
- 目录名(`name`)重复 → 后者覆盖前者 + warn。
- `skill_dirs` 路径不存在 → 跳过 + warn。
- 结果按 `name` 排序(保证 system 目录段稳定、可测试)。

frontmatter 解析依赖:
- **推荐 pyyaml**(加依赖,稳健,frontmatter 本就是 YAML)。
- 备选手写极简(零依赖,只提取 `description`,容忍其他字段)——若选此需处理 `description: foo` 单行与 `description: |` 多行块两种形态。

### 4.2 `render_catalog` + `append_catalog`(`loader.py`)

```python
def render_catalog(metas: list[SkillMeta]) -> str:
    """metas → <skills>...</skills> + 调用指引;metas 为空返回 ''。"""


def append_catalog(system: str | list[dict], catalog: str) -> str | list[dict]:
    """把 catalog 拼到 system 末尾,兼容 str 与 list[dict] 两种形态。"""
    if isinstance(system, str):
        return system + catalog
    return [*system, {"type": "text", "text": catalog}]
```

`render_catalog` 输出格式:
```
<skills>
- name: bullet-summarize
  description: 当用户要求总结文本/文章/对话要点时使用。输出 3-5 个简洁 bullet 要点。
- name: code-review
  description: 审查 git diff,按正确性/简洁性/性能维度找 bug。
</skills>

当用户请求匹配某个 skill 时,调用 load_skill(name) 加载完整指令后再执行。
```

### 4.3 `load_skill` Tool(`load_skill.py`)

```python
class LoadSkillInput(BaseModel):
    name: str


def load_skill_tool(metas: list[SkillMeta]) -> Tool:
    """工厂闭包:捕获 metas,返回 load_skill Tool。对标 builtin_tools(read_state) 模式。"""
    index = {m.name: m for m in metas}

    async def _load(inp: LoadSkillInput, ctx) -> str:
        meta = index.get(inp.name)
        if meta is None:
            return f"Error: skill '{inp.name}' not found. Available: {sorted(index)}"
        try:
            return meta.skill_md.read_text(encoding="utf-8")   # 返回 SKILL.md 全文
        except OSError as e:
            return f"Error: cannot read skill '{inp.name}': {e}"

    return Tool(
        name="load_skill",
        description="加载指定 skill 的完整指令。先看 <skills> 目录决定用哪个 skill,再调用此工具。",
        input_model=LoadSkillInput,
        func=_load,
        is_concurrency_safe=True,   # 只读 → 可并发
    )
```

- 返回 `SKILL.md` 全文(frontmatter + body)。
- `name` 找不到 → Error 文本 + 可用列表(走 tool_result,**不抛**)。
- 读失败 → Error 文本(走 tool_result,**不抛**)。
- `is_concurrency_safe=True`(只读,可并发,和 read/glob/grep 同等)。

### 4.4 `agent_loop` 集成

`AgentConfig` 新字段:
```python
@dataclass
class AgentConfig:
    ...现有字段...
    skill_dirs: list[str] = field(default_factory=lambda: ["skills/"])   # 默认项目根 skills/
```

`submit` 改动(开头组装段,`record_transcript` 之后、`QueryParams` 之前):
```python
read_state = FileReadState()
tools = [*config.tools, *builtin_tools(read_state)]

system = config.system
try:
    metas = SkillLoader.scan(config.skill_dirs)
except Exception as e:                       # 未预期 → 降级
    logger.warning(f"skill scan failed: {e}")
    metas = []
if metas:
    system = append_catalog(system, render_catalog(metas))
    tools = [*tools, load_skill_tool(metas)]

params = QueryParams(messages=..., system=system, ..., tools=tools, ...)
```

禁用 skill 的方式:调用方传 `skill_dirs=[]`(或删 `skills/` 目录),`scan` 返回 `[]` → 不注入。无需额外开关。

## 5. SKILL.md 格式

目录结构:
```
skills/                          # 默认根(skill_dirs 可追加外部目录)
├── bullet-summarize/            # 一个 skill = 一个目录
│   └── SKILL.md                 # 必须(YAML frontmatter + markdown body)
├── code-review/
│   ├── SKILL.md
│   └── resources/               # 可选(只读资源,模型用 read/glob/grep 访问)
│       └── checklist.md
```

- skill = 一个目录,**目录名 = skill 标识**(也作 `load_skill(name)` 入参)。
- `SKILL.md` 必须,在目录根。
- 资源(任意子目录/文件)可选,模型通过现有 read/glob/grep 读取。

`SKILL.md` schema(对标 CC 的 frontmatter + body):
```markdown
---
description: |
  当用户要求总结文本、文章或对话要点时使用。
  输出 3-5 个简洁 bullet 要点,每个不超过 20 字。
---

# body: load_skill 加载后注入上下文的指令
...
```

frontmatter 字段:
- `description`(**必须**)— 给模型的"何时用",进 system 目录段。
- 其余字段 YAGNI(二期)。

宽松解析原则:
- **容忍未知字段**(向前兼容 CC 新字段如 `when_to_use`/`allowed-tools` 等,解析时忽略)。
- **YAML 损坏** → 跳过该 skill(不崩)。

## 6. 错误处理矩阵

| 场景 | 处理 |
|---|---|
| `skill_dirs` 路径不存在 | 跳过 + warn |
| 子目录无 `SKILL.md` | 跳过 + warn(当普通子目录) |
| frontmatter 缺 `description` | 跳过 + warn(严格) |
| YAML 损坏 | 跳过该 skill + warn |
| 未知 frontmatter 字段 | 宽松容忍(忽略) |
| 目录名重复 | 后者覆盖 + warn |
| `load_skill(name)` 找不到 | 返回 Error 文本 + 可用列表(走 tool_result,不抛) |
| `load_skill` 读 `SKILL.md` 失败 | 返回 Error 文本(走 tool_result,不抛) |
| `scan` 整体异常(未预期) | submit 捕获 → 降级(无 skill 注入)+ warn |

**核心原则**:skill 是增强,任何失败 → 降级 + warn,绝不中断 `query_loop`。`load_skill` 的错误以文本返回(让模型自行修正),绝不抛异常。

## 7. 测试策略(TDD)

复用项目 `pytest asyncio_mode=auto` + `tmp_path` + `respx`。

**单元:**
- `test_skill_loader.py`:
  - scan 正常:多 skill 目录、多 `skill_dirs`、按 `name` 排序。
  - scan 容错:无 `SKILL.md` 跳过、缺 `description` 跳过、YAML 损坏跳过、路径不存在跳过、`name` 重复后者覆盖。
  - 宽松解析:未知字段容忍。
  - `SkillMeta` 字段正确(name/description/skill_dir/skill_md)。
  - `render_catalog`:格式正确、空 metas 返回 `""`、多行 `description`。
  - `append_catalog`:system 为 str / list[dict] 两种都正确拼接。
- `test_load_skill.py`:
  - 正常加载:返回 `SKILL.md` 全文。
  - `name` 不存在:返回 Error + 可用列表。
  - 读失败(模拟):返回 Error。
  - `is_concurrency_safe=True`。
  - (资源访问测试用 `tmp_path` 构造临时 skill + resources,不依赖示范 skill。)

**集成(`test_agent_loop_skill.py`):**
- `skill_dirs` 有 skill → system 拼了目录段 + tools 含 `load_skill`。
- `skill_dirs` 空或不存在 → system/tools 不变。
- system 为 `str` / `list[dict]` 两种拼接都正确。
- `scan` 异常 → 降级(无注入)。

**端到端(mock provider,可选):**
- 模型调 `load_skill` → `SKILL.md` 全文进 tool_result。
- 模型按 skill 指令调 read 访问 resources。

**示范 skill(冒烟):**
- `skills/bullet-summarize/SKILL.md`(实现阶段创建)。
- 真实跑一次,验证完整链路(目录注入 → load_skill → body 生效 → 模型输出 bullet 格式)。

## 8. 示范 skill:`bullet-summarize`

`skills/bullet-summarize/SKILL.md`(实现阶段创建):
```markdown
---
description: |
  当用户要求总结文本、文章或对话要点时使用。
  输出 3-5 个简洁 bullet 要点,每个不超过 20 字。
---

# bullet-summarize

总结时遵循:
1. 提取 3-5 个核心要点
2. 每个要点以 "- " 开头,不超过 20 字
3. 按重要性排序
4. 末尾不加客套话
```

用途:spec 真实示例 + 实现阶段冒烟测试对象。选它因为简单(纯指令)、能明显看出 skill 生效(模型加载后总结变 bullet 格式,观察即验证)。

## 9. 变更清单(文件级)

| 文件 | 动作 | 内容 |
|---|---|---|
| `core/skills/__init__.py` | 新建 | 导出 `load_skill_tool` / `SkillLoader` / `render_catalog` / `append_catalog` / `SkillMeta` |
| `core/skills/loader.py` | 新建 | `SkillMeta` + `SkillLoader.scan` + `render_catalog` + `append_catalog` |
| `core/skills/load_skill.py` | 新建 | `LoadSkillInput` + `load_skill_tool(metas)` |
| `core/agent_loop.py` | 改 | `AgentConfig` 加 `skill_dirs`;`submit` 扫描 + 注入 system/tools |
| `skills/bullet-summarize/SKILL.md` | 新建 | 示范 skill |
| `pyproject.toml` | 改 | 加 `pyyaml` 依赖(若选 pyyaml) |
| `tests/test_skill_loader.py` | 新建 | loader 单元测试 |
| `tests/test_load_skill.py` | 新建 | `load_skill` 工具测试 |
| `tests/test_agent_loop_skill.py` | 新建 | 集成测试 |

## 10. 权衡与未做

- **模型自主决策(对标 CC)**:灵活、语义匹配强,但每次带 skill 目录 token + 依赖模型判断。MVP skill 少,可接受。
- **目录 + 按需加载(对标 CC progressive disclosure)**:省 token(未激活 skill 全文不占位),代价是多一次 `load_skill` 工具调用往返。
- **目录名 = skill 标识(不要 frontmatter.name)**:简单、无一致性校验;代价是和 CC 略不同(CC 允许 `name` 覆盖目录名)。二期可加。
- **frontmatter 只要 `description`**:本项目唯一必需字段;CC 的其他字段(`when_to_use`/`allowed-tools`/`disable-model-invocation` 等)二期按需加。
- **`description` 缺失严格跳过**:本项目 `description` 是唯一字段,缺了无意义;CC 宽松是因为它有 `name` 等兜底。
- **不加 `enable_skills` 开关**:`skill_dirs=[]` 或 `skills/` 不存在 → `scan` 返回 `[]` → 不注入,已零开销,开关冗余。
- **每次 submit 扫描**:一次会话通常调一次,可接受;缓存为未来优化。
- **pyyaml 依赖**:推荐(稳健);备选手写极简(零依赖但脆弱,且要自己处理多行 `description` 块)。

**二期扩展点(标注不实现):**
- `/name` 显式触发 + `disable-model-invocation` / `user-invocable`。
- skill 专属工具声明。
- `allowed-tools` / `disallowed-tools`(权限收敛)。
- `frontmatter.name` 覆盖目录名。
- `description` 字符截断(1,536 预算)。
- skill 扫描缓存。
- bash / 脚本执行。
