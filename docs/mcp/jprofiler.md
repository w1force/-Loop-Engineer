# JProfiler MCP 接入说明

JProfiler MCP 是 ej-technologies 官方提供的 MCP server,用于让 agent 通过 MCP 协议调用 JProfiler 的 Java 性能分析能力。这里不实现 JProfiler 私有解析器,也不 mock JProfiler 输出。

## 两种接入方式

默认推荐使用官方 npm wrapper:

```json
{
  "mcpServers": {
    "JProfiler": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@ej-technologies/jprofiler-mcp@latest"],
      "timeout": 300
    }
  }
}
```

使用步骤:

```bash
cp .mcp.jprofiler.example.json .mcp.json
export LOOP_ENGINEER_MCP_CONFIG_PATH=.mcp.json
.venv/bin/python scripts/check_jprofiler_mcp.py --config .mcp.json
```

这种方式需要本机有 `node`、`npm`、`npx`。首次运行可能联网下载 JProfiler wrapper 和 JProfiler 本体,也可能进入 license/free evaluation 流程。

如果本机已经安装 JProfiler,可以改用自带的 `bin/jpmcp`:

```bash
cp .mcp.jprofiler.local.example.json .mcp.json
```

然后把 `.mcp.json` 里的占位路径:

```text
/path/to/JProfiler/bin/jpmcp
```

改成自己电脑上的真实路径。个人 `.mcp.json` 不要提交到仓库。

## 和 Debug Loop 的关系

JProfiler MCP 不应该在 `query_loop` 里写特例。它的流动应该是:

```text
.mcp.json
  -> core.mcp.config_loader.load_mcp_configs
  -> MCPServerConfig
  -> MCPManager
  -> StdioMCPClient
  -> tools/list
  -> AgentConfig.resolve_tools / refresh_tools
  -> query_loop 统一工具池
```

这样未来完整 debug agent 补上后,可以把 JProfiler 当成 runtime evidence 工具来源复用,而不是为 JProfiler 单独开一条流程。

## 常见失败

- `npx not found`: 本机没有 Node/npm/npx,或者 PATH 不包含 npx。
- `npm download failed`: 公司网络或 registry 阻止首次下载。
- `license activation failed`: JProfiler license/free evaluation 没处理完成。
- `stdio framing error`: MCP stdio 消息格式不兼容,需要检查 Content-Length framing 支持。
- `timeout`: 首次下载、启动或分析时间超过配置的 `timeout`。

## 真实验证

默认测试不会启动真实 JProfiler。真实验证需要显式打开:

```bash
LOOP_ENGINEER_JPROFILER_REAL=1 \
LOOP_ENGINEER_JPROFILER_CONFIG=.mcp.jprofiler.example.json \
.venv/bin/python -m pytest -q tests/test_mcp_jprofiler_real_optional.py -s
```

如果要带运行证据包一起观察输出,额外设置:

```bash
LOOP_ENGINEER_RUNTIME_EVIDENCE_ZIP=/absolute/path/to/runtime-evidence-demo.zip
```

真实测试包含两层:

```text
1. 启动官方 JProfiler MCP,打印 health、工具名和真实 schema。
2. 如果设置 LOOP_ENGINEER_RUNTIME_EVIDENCE_ZIP,从 zip 提取唯一的真实 .hprof,
   执行 load_snapshot -> check_status -> get_heap_data(biggest_objects/classes)。
```

完整分析输出可以用 `LOOP_ENGINEER_JPROFILER_OUTPUT` 指定到仓库外文件。真实分析还可以用 `LOOP_ENGINEER_JPROFILER_OBSERVATION_TIMEOUT` 设置验收观测窗口;它只限制这次人工验收,不改变生产 MCP 工具的统一长任务超时。

示例:

```bash
LOOP_ENGINEER_JPROFILER_REAL=1 \
LOOP_ENGINEER_JPROFILER_CONFIG=.mcp.jprofiler.example.json \
LOOP_ENGINEER_RUNTIME_EVIDENCE_ZIP=/absolute/path/runtime-evidence-demo.zip \
LOOP_ENGINEER_JPROFILER_OUTPUT=/absolute/path/jprofiler-analysis.json \
.venv/bin/python -m pytest -q tests/test_mcp_jprofiler_real_optional.py -s
```

只有第二层也通过,才能说 JProfiler 对这份真实 heap dump 的基础分析链路已经可用。

如果只想手动启动并观察工具列表,使用:

```bash
.venv/bin/python scripts/check_jprofiler_mcp.py --config .mcp.json --start
```

不加 `--start` 时脚本只做依赖和配置检查,不会启动 `npx`。
