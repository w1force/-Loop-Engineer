# 分级日志设计（通用应用日志 · JSON 结构化）

日期: 2026-07-14
状态: 待评审

## 1. 背景与目标

### 现状
- 标准库 `logging` 已在用,但配置简陋:仅 `main.py:18` 一句 `logging.basicConfig(level=INFO, format="%(asctime)s %(message)s")`,无统一封装、无级别标签、无结构化字段。
- `telemetry/` 是另一套**独立的业务埋点**(`TraceEvent`/`TraceKind`/`Tracer`),用于追踪 turn/stream/tool/provider 等业务事件,其 `LoggingTracer` 把所有事件都打到 INFO 级别、本身不分通用日志级别。它不是通用应用日志的替代品。
- 散落处:`main.py` 用 `print` 输出对话结果;`LoggingTracer` 用 `logging.getLogger("telemetry").info(...)`。

### 目标
提供项目级**通用分级应用日志**,满足:
- JSON 结构化输出(单行 JSON,便于采集/grep/jq)
- 分级:`DEBUG < INFO < WARNING < ERROR < CRITICAL`,外加自定义 `TRACE`(低于 DEBUG),用于流式细节
- 统一初始化入口,环境变量控制级别
- 在 stream event 级别支持细粒度 trace(每个返回的 StreamEvent 打一条)
- 输出目标可插拔(Handler),未来加文件/上云不改格式层

### 非目标(本次不做)
- 全量替换各模块现有 `print`/`log` 调用(仅搭基础设施 + 替换 `main.py` + 接入 stream trace)
- `telemetry` TraceKind → 日志级别的映射(留作 telemetry 那条线)
- 文件 handler / 云上报 handler(留作未来演进,见 §7)

## 2. 方案选型

**标准库 `logging` + 自写 `JsonFormatter` + `setup_logging()`。零新依赖。**

理由:
- `logging` 与 Java `java.util.logging` 同源,架构 `Logger → Handler(输出目标) → Formatter(格式)` 一一对应,输出目标可插拔是原生能力(Appender 模型)。
- 与现有 `telemetry/LoggingTracer` 无缝共存(它已用 `logging`)。
- Handler 与 Formatter 正交:未来加 `RotatingFileHandler`(文件)或自写 `CloudLogHandler`/`QueueHandler`(云)时,同一份 JSON Formatter 复用,不改业务代码。
- 自写 Formatter ≈ 20 行,且字段完全可控(未来加 `trace_id`/`service` 随意);不值得为此引入 `python-json-logger`。
- `structlog` 仅在"结构化处理逻辑(批量/采样/脱敏)变复杂"时才有明显优势,当前 YAGNI;届时 API 基本不变,迁移成本低。

## 3. 级别

| 级别 | 数值 | 说明 |
|------|------|------|
| `TRACE` | 5 | 自定义。stream event 等极细粒度,默认关闭 |
| `DEBUG` | 10 | 诊断信息 |
| `INFO` | 20 | 常规运行(默认级别) |
| `WARNING` | 30 | 需注意(如预算告警) |
| `ERROR` | 40 | 错误(如 provider 5xx) |
| `CRITICAL` | 50 | 致命 |

控制:
- 环境变量 `LOG_LEVEL`(大小写不敏感)设全局阈值,**优先级最高**(便于不改代码临时 `LOG_LEVEL=DEBUG`/`TRACE` 调试)。
- 默认 `INFO`(trace/debug 默认零开销短路)。
- 按模块放行仍可用:`logging.getLogger("providers").setLevel(DEBUG)`。

## 4. 组件(`core/logging_setup.py`)

### 4.1 TRACE 级别注册
```python
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

def _trace(self, msg, *args, **kwargs):
    if self.isEnabledFor(TRACE):        # 低于阈值时短路,零开销
        self._log(TRACE, msg, args, **kwargs)
logging.Logger.trace = _trace
```
注册后 `getLevelNamesMapping()` 含 `TRACE`,`LOG_LEVEL=TRACE` 可解析。

### 4.2 `JsonFormatter(logging.Formatter)`
输出单行 JSON:
```json
{"ts":"2026-07-14T12:00:01+08:00","level":"INFO","logger":"core.agent_loop","msg":"turn start","model":"claude","msg_count":3}
```
- 字段:`ts`(ISO8601,含本地时区)、`level`(名称)、`logger`(`record.name`)、`msg`(`record.getMessage()`)
- `extra={...}` 传入的字段平铺到 JSON 顶层(过滤掉 logging 内部保留键)
- 异常:`record.exc_info` → `exc` 字段(完整格式化堆栈),不丢栈
- 容错:`format` 自身抛错时回退到 `record.getMessage()` 纯文本,绝不向上抛(日志不能影响主流程)

### 4.3 `setup_logging(level: str | int = "INFO", *, stream=None) -> None`
程序入口调用一次:
- 读环境变量 `LOG_LEVEL` 覆盖 `level`(env 优先)
- 清空 root 旧 handler → 加 `StreamHandler(stream or sys.stdout)` → 挂 `JsonFormatter`
- `root.setLevel(resolved_level)`;非法级别字符串回退 `INFO`(不抛)
- 压制噪声 logger:`logging.getLogger("httpx").setLevel(WARNING)`(免得 SSE 流刷屏)
- 幂等:重复调用先清空再装,不叠加 handler

## 5. 调用约定(各模块)
```python
import logging
logger = logging.getLogger(__name__)                       # Java 风格;logger 字段来源
logger.info("provider request", extra={"model": m, "msg_count": n})   # extra 平铺进 JSON
logger.debug("...")        # LOG_LEVEL=DEBUG 才出
logger.trace("stream event", extra={"event": evt.model_dump()})       # LOG_LEVEL=TRACE 才出
```

## 6. 集成点

| 位置 | 改动 |
|------|------|
| `core/logging_setup.py` | 新建:TRACE 注册 + `JsonFormatter` + `setup_logging` |
| `main.py` | `logging.basicConfig(...)` → `setup_logging()` 一行 |
| `config.py`(`Settings`) | 加字段 `log_level: str = "INFO"`(配置驱动;env `LOG_LEVEL` 仍可覆盖) |
| `core/providers/anthropic.py` | `stream` 在 `yield` 前加 `logger.trace("stream event", extra={"event": <StreamEvent>.model_dump()})`,记录每个返回的 StreamEvent |
| `telemetry/LoggingTracer` | **不改**——已用 `logging.getLogger("telemetry")`,自动继承 JSON 输出 |

stream trace 落点(`anthropic.py` 现有 `yield self._to_stream_event(evt)`,约 line 121):
```python
se = self._to_stream_event(evt)
logger.trace("stream event", extra={"event": se.model_dump()})
yield se
```
`LOG_LEVEL >= DEBUG` 时 `trace` 被 `isEnabledFor` 短路,对正常运行的流式吞吐无可观测开销。

## 7. 错误处理
- `setup_logging` 永不抛(非法级别回退 `INFO`)。
- `JsonFormatter.format` 异常回退纯文本。
- Handler 内部异常走 `logging` 自带 `Handler.handleError`(默认打 stderr 不抛)。

## 8. 测试(`tests/test_logging_setup.py`)
- `JsonFormatter`:输出为合法 JSON;含 `ts`/`level`/`logger`/`msg`;`extra` 字段透传到顶层;`exc_info` 序列化为 `exc` 字段且含栈。
- 级别过滤:`LOG_LEVEL=WARNING` 时 `info` 不出、`warning` 出。
- TRACE:`LOG_LEVEL=TRACE` 时 `logger.trace(...)` 出、默认 `INFO` 时不出。
- `setup_logging`:非法级别字符串不抛且回退 `INFO`;重复调用不叠加 handler(幂等);`httpx` logger 被压到 `WARNING`。
- 集成(可选):`anthropic.stream` 在 `LOG_LEVEL=TRACE` 下每个 StreamEvent 产一条 trace 日志。

## 9. 数据流
```
logger.info/trace("msg", extra={...})
   →  LogRecord
   →  root Logger [级别阈值过滤: 低于 setLevel 的整条丢弃]
   →  StreamHandler(stdout)
   →  JsonFormatter.format → 单行 JSON
   →  stdout
```

## 10. 未来演进(非本次,记录决策依据)
- **加文件**:`root.addHandler(RotatingFileHandler("app.log", maxBytes=10MB, backupCount=5))`,`JsonFormatter` 复用。
- **上云**:自写 `CloudLogHandler(logging.Handler)`,`emit()` 喂云 SDK;或标准库 `QueueHandler`+`QueueListener` 后台批量异步上报(fire-and-forget,不阻塞)——与 `telemetry/RemoteTracer` 占位规划的 `asyncio.Queue + 后台 flush` 同构,可合流。
- **结构化处理变复杂**(批量/采样/脱敏/多目标 pipeline):再评估引入 `structlog`,届时调用 API 基本不变。
