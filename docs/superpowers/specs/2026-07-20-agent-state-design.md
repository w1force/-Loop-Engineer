# agent_state 架构重构设计

> 日期: 2026-07-20 · 分支: feat/skill
> 背景: 现有 `State` 在 query_loop 内初始化 + 每轮 `model_copy` 重建,本质是 **query_state**(单轮循环/恢复计数器)。整个系统缺一个跨 submit、跨轮次的 **agent_state**。`FileReadState`/skills 靠工厂闭包扛"跨轮持久"是 symptom,`messages` 每次 submit 从 `initial_messages` 重建(不累积)是多输入的瓶颈。本次引入 `AgentState` 收编散落数据,为多输入铺路。

## 1. 背景与范围

**问题**:
- `State`(query_loop 内)每轮重建 = 单轮 query_state,无跨 submit 记忆。
- 跨轮/跨 submit 数据散落:`FileReadState`/skills 靠工厂闭包;`messages` 每次 submit 从 `initial_messages` 重建(不累积)。
- 闭包是补丁,非设计;且不支持多输入。

**范围**:
- 引入 `AgentState`(跨 submit 会话状态,caller 持有)。
- 收编散落数据:`messages`/`skills`/`file_read_state`/`cwd`/预算计数 进 AgentState。
- `ToolContext` 持双 state(`agent_state` + `query_state`)。
- 工具改造:`read`/`write`/`glob`/`grep`/`load_skill` 从 `ctx.agent_state` 取,闭包退场。
- `State`→`QueryState` 改名 + 瘦身(保留 `messages` 字段引用 `agent_state.messages`)。
- `submit`/`build_agent_state`/`build_system_prompt` 流程。
- `query_loop`/`stream_turn`/`maybe_compact`/recovery 改造。

**明确不做(YAGNI/二期)**:
- 多输入的完整 UI/接口(本次只奠基:agent_state 跨 submit 持久 + messages 累积)。
- 动态 skill 发现(运行时增减 skill;本次 skills 首次 build 固定,但 load_skill 从 state 取已为此留空间)。
- `Agent` 类封装(本次仍 submit 函数 + 工厂)。

## 2. 设计原则

1. **三层状态分离**:`agent_state`(跨 submit 会话)/ `query_state`(单 query_loop 内循环计数)/ `params`(单次调用配置)。
2. **messages 单一来源**:`agent_state.messages`;`query_state.messages` 是引用别名(query_loop 内便利,减小改动)。
3. **闭包退场**:工具从 `ctx.agent_state` 取运行时数据,工厂不再捕获。
4. **妥协减小改动**:`query_state` 保留 `messages` 字段(引用 `agent_state.messages`),`query_loop`/`stream_turn`/`maybe_compact` 内部代码基本不动。
5. **跨 submit 持久**:caller 持有 agent_state,多次 submit 传同一对象。

## 3. 数据模型

### AgentState(新增,`core/types.py`,dataclass)

```python
@dataclass
class AgentState:
    """跨 submit 的 agent 会话状态(caller 持有)。"""
    messages: list[Message] = field(default_factory=list)          # 跨 submit 累积(唯一权威)
    skills: list[SkillMeta] = field(default_factory=list)          # 首次 build scan
    file_read_state: FileReadState = field(default_factory=FileReadState)  # 跨 submit 持久
    cwd: str = ""                                                   # build 时设(default os.getcwd())
    total_input_tokens: int = 0                                     # 预算:跨 submit 累积
    total_output_tokens: int = 0
```

- 用 **dataclass**(非 pydantic):内部状态容器,不需校验/序列化/不 `model_copy`;含非 pydantic 类型(`FileReadState`/`SkillMeta`)自然。
- **无 `tools` 字段**:tools 走 `QueryParams`(executor 注册 + stream_turn 发 API)。

### QueryState(`State` 改名 + 保留 messages 引用)

```python
class QueryState(BaseModel):
    messages: list[Message]  # 保留(引用 agent_state.messages,query_loop 初始化时设)
    turn_count: int = 1
    max_output_tokens_recovery_count: int = 0
    max_output_tokens_override: int | None = None
    has_attempted_autocompact: bool = False
    network_retry_count: int = 0
    transition: Continue | Terminal | None = None
```

仍是 pydantic(recovery `model_copy`)。`messages` 字段保留但**引用** `agent_state.messages`(不每轮重建)。

### SkillMeta 移入 `core/types.py`

从 `core/skills/loader.py` 移到 `types.py`(避免底层 types 反向依赖上层 skills;loader 从 types import)。纯数据 dataclass,和 State/Message 同层。

### FileReadState 留 `core/builtin_tools/readstate.py`

`AgentState` 字段含 `FileReadState`,但后者是 builtin 概念(含 set/get/is_stale/is_unchanged 方法),不移。`types.py` 单向 `from .builtin_tools.readstate import FileReadState`——`readstate.py` 不依赖 `types`(无循环)。若实现时遇循环风险,改 `TYPE_CHECKING` forward ref + `arbitrary_types_allowed`(但 AgentState 是 dataclass 不需 pydantic 配置,直接类型注解即可)。

## 4. ToolContext 双 state

```python
@dataclass
class ToolContext:
    tracer: Tracer
    abort_signal: asyncio.Event
    agent_state: AgentState                     # 必需(跨 submit;工具取 file_read_state/skills/cwd)
    query_state: QueryState | None = None       # 可选(原 state 改名;query_loop 内填)
```

## 5. 工具改造(闭包退场)

| 工具 | 从 `ctx.agent_state` 取 | 现状(闭包) |
|------|------------------------|-------------|
| `read` | `file_read_state` + `cwd` | `read_tool(read_state, cwd)` |
| `write` | `file_read_state` + `cwd` | `write_tool(read_state, cwd)` |
| `glob` | `cwd` | `glob_tool(cwd)` |
| `grep` | `cwd` | `grep_tool(cwd)` |
| `load_skill` | `skills` | `load_skill_tool(metas)` |

- `builtin_tools()` **无参**,返回 `[glob, grep, read, write, load_skill]`(func 从 ctx 取,不闭包)。
- `load_skill` 移 `core/builtin_tools/load_skill.py`(和其他 4 个 builtin 统一位置)。
- 工具定义可模块级常量或无参工厂(func 不依赖闭包)。

## 6. submit 流程 + 工厂 + system 生成

### `build_agent_state(config)` 工厂(`core/agent_loop.py`)

```python
def build_agent_state(config: AgentConfig) -> AgentState:
    """调用者初始化 agent_state(组装 skills/file_read_state/cwd)。"""
    try:
        skills = SkillLoader.scan(config.skill_dirs)   # 降级
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

`AgentConfig` 加 `cwd: str = field(default_factory=os.getcwd)`。

### `build_system_prompt(agent_state, config)` 单独函数(`core/agent_loop.py`)

```python
def build_system_prompt(agent_state: AgentState, config: AgentConfig) -> str | list[dict]:
    """生成最终 system:config.system + skill 目录(从 agent_state.skills)。"""
    skills = agent_state.skills
    if not skills:
        return config.system
    # 渲染 <skills> 目录(原 render_catalog 逻辑)+ 拼接(原 append_catalog 逻辑),内联
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

(原 `render_catalog`/`append_catalog` 删除,逻辑内联到此)

### `submit` 流程(签名变)

```python
async def submit(prompt, agent_state, config, tracer):
    agent_state.messages.append(UserMessage(content=prompt))   # 跨 submit 累积
    await record_transcript(agent_state.messages, config.transcript_path)
    system = build_system_prompt(agent_state, config)
    tools = [*config.tools, *builtin_tools()]                  # builtin_tools() 无参含 load_skill
    params = QueryParams(model=..., max_tokens=..., provider=..., abort_signal=...,
                         tools=tools, max_turns=..., can_use_tool=..., tool_execution_mode=...)
    async for msg in query_loop(agent_state, params, tracer):  # ★ agent_state 传入
        if isinstance(msg, AssistantMessage):
            # query_loop 内已 append agent_state.messages(query_state.messages 引用)
            await record_transcript(agent_state.messages, config.transcript_path)
            last_stop_reason = msg.stop_reason
            if msg.usage:
                agent_state.total_input_tokens += msg.usage.input_tokens
                agent_state.total_output_tokens += msg.usage.output_tokens
        elif isinstance(msg, Tombstone): continue
        elif isinstance(msg, StreamEvent): continue
        if config.max_budget_usd is not None and _rough_cost(
            agent_state.total_input_tokens, agent_state.total_output_tokens) >= config.max_budget_usd:
            yield {"type": "result", "subtype": "error_budget", "error": "budget exceeded"}
            return
    # 收尾判定(agent_state.messages 的 last)
```

**QueryParams 保留 `tools`**(瘦身去 `messages`/`system`;tools 走 params,executor 注册 + stream_turn 发 API)。

## 7. query_loop · stream_turn · maybe_compact · recovery 改造

### query_loop(签名加 agent_state)

```python
async def query_loop(agent_state, params, tracer):
    state = QueryState(messages=agent_state.messages, turn_count=1)  # ★ messages 引用同一 list
    chain = build_recovery_chain()
    turn_id = 0
    while True:
        turn_id += 1
        state = await maybe_compact(agent_state, state, params, tracer)  # 加 agent_state
        ctx = ToolContext(tracer=tracer, abort_signal=params.abort_signal,
                          agent_state=agent_state, query_state=state)
        executor = make_executor(params.tool_execution_mode, params.tools,
                                 params.can_use_tool, tracer, ctx)       # params.tools
        try:
            outcome = None
            async for m in stream_turn(agent_state, state, params, tracer, executor):  # 加 agent_state
                ...
            assert outcome is not None
        except ProviderError as e:
            ...
        state.network_retry_count = 0
        state.messages.extend(outcome.assistant_msgs)    # = agent_state.messages(同一 list)
        yield outcome.assistant_msgs[0]
        if outcome.needs_follow_up:
            tool_results = await executor.get_results()
            state.messages.append(UserMessage(content=tool_results))   # 回灌
            state = state.model_copy(update={"turn_count": state.turn_count + 1,
                                             "transition": Continue(reason=ContinueReason.NEXT_TURN)})
            ...
```

### stream_turn / maybe_compact

签名加 `agent_state` 参数。**内部继续用 `state.messages`**(引用 `agent_state.messages`,代码基本不动)。

### recovery(`rules.py`)

`QueryState.model_copy` 只改 `turn_count`/recovery 计数/`transition`(**不重建 messages**)。messages 引用保持 `agent_state.messages`,不会脱节。

## 8. 错误处理

| 场景 | 处理 |
|------|------|
| `ctx.agent_state` | ToolContext 必需字段,query_loop 注入保证非 None;工具 func 直接取(不防御) |
| skill scan 失败 | `build_agent_state` try/except → `skills=[]` + warn(不中断) |
| load_skill 找不到 name | 文本 Error + Available(走 tool_result,不抛) |
| load_skill 读 SKILL.md 失败 | 文本 Error(走 tool_result,不抛) |
| read/write/glob/grep | 不变(异常 → is_error 机制) |

## 9. 测试策略

| 层 | 测试 |
|----|------|
| 工具(read/write/glob/grep/load_skill) | 构造 `agent_state`(file_read_state/cwd/skills)+ ctx,调 func(**不再传闭包参数**) |
| `build_agent_state` | config → agent_state,验证 skills(scan)/file_read_state/cwd/messages=[] |
| `build_system_prompt` | agent_state.skills + config.system → 最终 system(str\|list 两种 + 空 skills 原样返回) |
| `submit` | **多 submit 累积**(核心新测):同一 agent_state 两次 submit,messages 跨 submit 累积 + budget 累积 |
| query_loop/stream_turn/recovery | agent_state 传入;query_state.messages 引用 agent_state.messages;recovery 不重建 messages |

## 10. 变更清单(文件级)

| 文件 | 改动 |
|------|------|
| `core/types.py` | 加 `AgentState`(dataclass);`State`→`QueryState` 改名;`SkillMeta` 移入 |
| `core/tools.py` | `ToolContext` 加 `agent_state`(必需),`state`→`query_state` |
| `core/agent_loop.py` | `build_agent_state` 工厂 + `build_system_prompt` 函数 + `submit` 改签名;`AgentConfig` 加 `cwd`;删 prepare_skills/render_catalog/append_catalog 引用 |
| `core/builtin_tools/*.py` | read/write/glob/grep 改从 ctx 取(无闭包参数);新增 `load_skill.py`(移入,从 ctx 取);`builtin_tools()` 无参,返回 5 个 |
| `core/skills/loader.py` | `SkillMeta` 移走(从 types import);删 `render_catalog`/`append_catalog`;留 `SkillLoader.scan`/`_parse_frontmatter` |
| `core/loop/orchestrator.py` | `query_loop(agent_state, params, tracer)`;`QueryState(messages=agent_state.messages, ...)`;executor 用 `params.tools` |
| `core/loop/phases/stream_turn.py` | 签名加 `agent_state` |
| `core/loop/phases/compact.py` | 签名加 `agent_state` |
| `core/loop/recovery/rules.py` | `model_copy` 不重建 messages(只改计数/transition) |
| `tests/*` | 全面适配(agent_state 构造、多 submit 累积、工具 ctx 改造) |

## 11. 权衡与未做

- **query_state 保留 messages(引用)**:妥协,减小 query_loop/stream_turn/maybe_compact 改动。messages 单一来源仍是 `agent_state.messages`。
- **agent_state 不存 tools**:executor 注册自带 `_tools` + stream_turn 用 `params.tools` 发 API,tools 走 `QueryParams`(单一来源 params.tools)。
- **AgentState 用 dataclass(非 pydantic)**:内部状态容器,不需校验/序列化/不 `model_copy`;含非 pydantic 类型自然。
- **cwd 进 agent_state**:和 file_read_state 同层(会话级运行时数据),工具统一从 `ctx.agent_state` 取。
- **预算累积进 agent_state**:`max_budget_usd` 变整个会话预算(跨 submit 累计)。
- **build_system_prompt 单独函数(非 config 方法)**:职责分离(config 是数据,system 生成是逻辑)。
- **load_skill 移 `core/builtin_tools/`**:和其他 builtin 统一位置(都是无状态工具定义)。

**二期扩展点(标注不实现)**:
- 多输入完整接口(UI/会话管理)。
- 动态 skill 发现(运行时增减 agent_state.skills;load_skill 从 state 取已为此留空间)。
- `Agent` 类封装(agent_state + submit 方法)。
