# loop-engineer

Python 双层 agentic loop 复现 —— **Phase 1**：双层 loop 骨架 + 全程埋点 + 可跑的 Anthropic 直通 + 定死的扩展点。

> 设计依据（项目根目录三份文档）：
> - `python-dual-loop-plan.md`(P1)：状态机真相 —— 数据模型 / provider 抽象 / query_loop / aggregate_stream
> - `p2.md`(P2)：代码组织(orchestrator+phases+recovery 责任链)+ 埋点(Tracer)+ Phase 1 桩约定
> - `p2-phase1-checklist.md`：逐文件施工手册

## 运行

```bash
# 1. 安装依赖(uv, Python 3.12)
uv sync

# 2. 配置 API(任选其一)。前缀 LOOP_ENGINEER_,刻意避开 Anthropic 官方 SDK 的 ANTHROPIC_*
cp .env.example .env       # 然后编辑 .env 填 LOOP_ENGINEER_API_KEY / LOOP_ENGINEER_MODEL
#   或直接用环境变量:
#   export LOOP_ENGINEER_API_KEY=sk-...
#   export LOOP_ENGINEER_BASE_URL=https://api.anthropic.com   # 默认官方
#   export LOOP_ENGINEER_MODEL=claude-sonnet-4-6

# 3. 跑一次纯文本对话
uv run python main.py
```

`main.py` 用 `LoggingTracer`,终端会先打印埋点日志,最后打印结果 dict,形如:

```
... turn_start {... 'turn': 1, 'seq': 1}
... provider_request {... 'model': '...', 'msg_count': 1, 'seq': 2}
... stream_end {... 'stop_reason': 'end_turn', 'usage': {...}, 'seq': 3}
... transition {... 'reason': 'completed', 'seq': 4}
{'type': 'result', 'subtype': 'success', 'text': '...你好...', 'usage': {...}}
```

埋点序列 `TURN_START → PROVIDER_REQUEST → STREAM_END → TRANSITION(completed)` 即 Phase 1 DoD。换 `NoopTracer()` 可静默埋点。对话记录落盘到 `run.transcript.jsonl`。

## 测试

```bash
uv run pytest           # 全套 42 个
uv run pytest -q        # 精简输出
```

覆盖:`parse_sse`(多行 data/注释/[DONE]/流末补 yield)、`aggregate_stream`(红线#4 先攒齐再 yield、TOOL_USE_DETECTED)、`AnthropicAdapter.stream`(respx mock SSE→StreamEvent)、`RecoveryChain`(→completed)、`orchestrator`(respx 端到端→completed+埋点序列)、`agent_loop`(is_result_successful 三路径 + submit success + transcript 落盘)、桩扩展点(tool_use→run_tools 桩、OpenAI×2 桩)。

## 常见排错

| 现象 | 原因 / 处理 |
|---|---|
| `401` / `403` | `LOOP_ENGINEER_API_KEY` 未设或无效 |
| `404` / model not found | `LOOP_ENGINEER_MODEL` 与 `LOOP_ENGINEER_BASE_URL` 不匹配 |
| 智谱端点 (`open.bigmodel.cn/api/anthropic`) | 设 `LOOP_ENGINEER_BASE_URL=https://open.bigmodel.cn/api/anthropic`,`LOOP_ENGINEER_MODEL` 改智谱模型名(如 `glm-4.6`),key 用智谱平台 key |
| 连接/超时 | 网络/代理;`LOOP_ENGINEER_BASE_URL` 是否可达 |

## Phase 1 状态(DoD)

- [x] 注入 `NoopTracer`/`LoggingTracer`,Anthropic 纯文本对话端到端跑通(orchestrator + stream_turn + CompletedRule → `completed`)(单测用 respx 验证;真实 smoke 见上「运行」)
- [x] `LoggingTracer` 日志见 `TURN_START / PROVIDER_REQUEST / STREAM_END / TRANSITION(completed)`
- [x] 触发 tool_use 路径 → `run_tools` 桩抛带清晰信息的 `NotImplementedError`
- [x] §3 全部单测通过(42 passed)
- [x] 所有桩(OpenAI×2、`run_tools`、`compact`、两条 recovery 规则、`RemoteTracer`)签名为最终形态,仅实现体抛错

## 桩清单(接口稳定,实现延后)

| 模块 / 函数 | Phase 1 | 落地 |
|---|---|---|
| `providers/anthropic.py` `.stream` | ✅ 实现 | 1 |
| `providers/openai_chat.py` / `openai_responses.py` `.stream` | 🔲 桩 | 4 |
| `tools.run_tools` | 🔲 桩 | 2 |
| `phases/compact.maybe_compact` | 🔲 直通 | 5 |
| `recovery.PromptTooLongRule` / `MaxOutputTokensRule` | 🔲 桩 | 5 |
| `recovery.CompletedRule` | ✅ 实现 | 1 |
| `telemetry.RemoteTracer` | 🔲 桩 | 待中间件确认 |

## 目录结构

```
core/
  types.py                 # P1 §4 数据模型 + 状态机枚举
  provider.py              # Provider 协议 + BaseAdapter
  providers/{anthropic,openai_chat,openai_responses,_sse}.py
  loop/
    orchestrator.py        # query_loop 主干 + QueryParams
    phases/{compact,stream_turn,execute_tools}.py
    recovery/{base,rules.py}  # 责任链引擎 + 规则
  agent_loop.py            # 外层:持久化+守卫+收尾
  tools.py                 # Tool / run_tools(桩) / can_use_tool
  transcript.py            # JSONL 持久化
telemetry/{events,tracer}.py   # Tracer 协议 + Noop/Logging/Remote
config.py  main.py  tests/
```

## 后续 Phase

Phase 2(工具执行)→ 3(agent_loop resume)→ 4(OpenAI)→ 5(recovery:两段式 max_tokens / prompt_too_long 压缩链 / 触发式 autocompact)→ 6(并发工具等可选)。桩的签名已定死,届时只填实现体。
