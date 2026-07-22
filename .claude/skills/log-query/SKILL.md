---
name: log-query
description: 排查 agent loop 运行问题时使用——用 jq 从运行日志(默认 logs/{时间戳}.jsonl)按需提取 LLM 调用/工具执行/错误等关键部分,而非全量读取文件。
---

# log-query:查询结构化运行日志

运行日志(默认 `logs/{时间戳}.jsonl`,或 FileTracer 传入的自定义 path)是 agent loop 的结构化执行轨迹(每次运行一个独立文件),**每个事件一行 JSON**。
**绝不全量读取**(单次运行可能数千行、含完整 LLM 内容 + 工具入参/返回,会撑爆上下文)。用 `jq` 按需过滤关键部分。

## 行 schema

```json
{"ts","seq","chain_id","turn","kind","payload"}
```
- `seq`:全局递增(时间序,跨轮保序)
- `turn`:agent loop 轮次(tracer.child 注入,**同轮事件可按 turn join**)
- `kind`:事件类型(见下表)

## 事件 kind 速查

| kind | 含义 | payload 关键字段 |
|---|---|---|
| `turn_start` / `turn_end` | 每轮起止 | turn_end:`stop_reason` |
| `transition` | 状态转换 | `reason`(next_turn / max_output_tokens_escalate / network_retry / completed / ...) |
| `provider_request` | LLM 请求 | `model`,`msg_count`,**`req_body`**(messages/system/tools/max_tokens) |
| `llm_response` | LLM 响应 | **`stop_reason`,`usage`,`blocks`,`raw_events`**,`error` |
| `provider_error` | provider 调用失败 | `status`/`body` 或 `transport` 或 `event` |
| `tool_use_detected` | 流式检测到工具调用 | `tool_name`,`tool_use_id` |
| `tool_exec_start` / `tool_exec_end` | 工具执行 | start:`tool_name`,`input`;end:`is_error`,`result`,`error` |
| `recovery_attempt` | 兜底规则命中 | `rule`,`withheld` / `error` |
| `tool_input_malformed` | LLM 的 tool_use input 非合法 object,被兜底成 {} | `tool_use_id`,`tool_name`,`reason`,`parsed_type`,**`raw_input_buf`** |
| `run_error` | 未捕获异常(崩溃) | `type`,`message`,**`traceback`** |

## 定位日志文件

FileTracer 未传 path 时默认写 `logs/{时间戳}.jsonl`(如 `logs/20260722-085639.jsonl`);传了 path 则按该 path 原样写。取 logs/ 下最新一个:

```bash
F=$(ls -t logs/*.jsonl | head -1) && echo $F
```

## 常用 jq 模式

先 `head -1 $F` 确认是 JSONL。

### 总览:发生了什么
```bash
jq -c '{seq,turn,kind}' $F              # 事件时间线
jq -c '{seq,turn,kind}' $F | tail -40   # 末尾(看运行怎么收尾)
```

### 某一轮的完整轨迹(请求→工具→响应 串起来)
```bash
jq 'select(.turn==3)' $F
```

### LLM 调用
```bash
jq -c 'select(.kind=="provider_request")|{turn,msg_count:.payload.msg_count,max_tokens:.payload.req_body.max_tokens}' $F
jq -c 'select(.kind=="llm_response")|{turn,stop:.payload.stop_reason,usage:.payload.usage,error:.payload.error}' $F
jq -c 'select(.kind=="llm_response")|.payload.stop_reason' $F | sort | uniq -c   # stop_reason 分布
```

### 核对 provider 原始返回(诊断 max_tokens/stop_reason 真实取值)
```bash
jq -c 'select(.kind=="llm_response")|.payload.raw_events[]|select(.type=="message_delta")' $F
```
对比 `llm_response.stop_reason` 与 raw `message_delta` 里的 stop_reason —— 若 provider 返回的是 `"length"` 而代码预期 `"max_tokens"`,即兜底判定的兼容性 bug。

### 工具执行(入参/返回/异常)
```bash
jq -c 'select(.kind=="tool_exec_end")|{is_error:.payload.is_error,result:.payload.result.content}' $F
jq -c 'select(.kind=="tool_exec_end" and .payload.is_error)|.payload.error' $F   # 失败的工具(含 traceback)
```

### tool_use input 损坏(GLM 偶发返回 list/残缺 json,被兜底成 {})
```bash
jq -c 'select(.kind=="tool_input_malformed")|{turn,tool:.payload.tool_name,reason:.payload.reason,parsed_type:.payload.parsed_type}' $F
jq -r 'select(.kind=="tool_input_malformed")|.payload.raw_input_buf' $F | head -1   # 看原始 input(GLM 到底返回了啥)
```

### 所有报错(一刀切)
```bash
jq -c 'select(.kind|test("error"))' $F              # provider_error / tool_exec_end(is_error) / run_error
jq -c 'select(.kind=="run_error")|.payload' $F      # 未捕获异常(崩溃,含 traceback)
```

### 兜底/recovery 是否触发
```bash
jq -c 'select(.kind=="recovery_attempt" or .kind=="transition")|{seq,turn,kind,payload}' $F
```

## 排查思路

1. `{seq,turn,kind}` 总览 → 定位异常区间(哪一轮、哪个事件)
2. `select(.turn==N)` 看那一轮全程(请求/工具/响应/转换)
3. 针对问题 kind 看详情:
   - LLM 行为异常 → `llm_response.stop_reason` + `raw_events` 的 message_delta
   - 工具失败 → `tool_exec_end.error`(type/message/traceback)
   - 崩溃 → `run_error.traceback`
   - 兜底没触发 → 看 `llm_response.stop_reason` 是否等于代码预期的 `"max_tokens"`(stream_turn.py:176)

## 取某次请求/响应的完整内容(确认要全量看时)

```bash
jq 'select(.kind=="provider_request" and .turn==3)|.payload.req_body' $F    # 完整请求体
jq 'select(.kind=="llm_response" and .turn==3)|.payload.blocks' $F          # 完整响应内容(blocks)
```
只在定位到具体某轮后再取该轮全量,不要一次性 dump 整个文件。
