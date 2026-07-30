"""配置: pydantic-settings 从环境变量/.env 读取。

所有键统一加 `LOOP_ENGINEER_` 前缀,**刻意避免**与 Anthropic 官方 SDK 的
`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` 等约定变量冲突(装了官方 SDK 会自动
读那两个)。config 层 provider 中立,Phase 4 接 OpenAI 时同一套配置也适用。

环境变量(.env 同名键):
    LOOP_ENGINEER_API_KEY     API key(端到端验收需要)
    LOOP_ENGINEER_BASE_URL    默认官方 https://api.anthropic.com
    LOOP_ENGINEER_MODEL       模型 id
    LOOP_ENGINEER_MAX_TOKENS  单次生成上限
    LOOP_ENGINEER_MAX_TURNS   内层 query_loop 最大轮次守卫
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOOP_ENGINEER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # 忽略 ANTHROPIC_* 等无关变量,绝不串味
    )

    api_key: str = ""
    base_url: str = "https://api.anthropic.com"
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    max_turns: int = 20
    debug_sse: bool = False  # LOOP_ENGINEER_DEBUG_SSE=true 时打印原始 SSE 流
    run_log_enabled: bool = True  # ★ 结构化运行日志(FileTracer 写 JSONL, jq 可查)
    run_log_path: str | None = None  # None → FileTracer 默认 logs/{时间戳}.jsonl;设了则原样用(不拼接)
    mcp_config: list[str] = Field(default_factory=list)  # LOOP_ENGINEER_MCP_CONFIG='[".mcp.json", "{...}"]'
    mcp_config_path: str = ""  # LOOP_ENGINEER_MCP_CONFIG_PATH=.mcp.json,单文件便捷入口
    mcp_tool_wait_timeout: float = 5.0  # 首轮等 MCP 工具列表的最长时间;超时后主流程继续
    tda_enabled: bool = False  # LOOP_ENGINEER_TDA_ENABLED=true 时把真实 TDA MCP 接进 agent
    tda_jar_path: str = ""  # LOOP_ENGINEER_TDA_JAR_PATH=/path/to/tda-3.2.jar
    tda_timeout: float = 60.0  # TDA 启动和分析 thread dump 可能比普通 MCP 工具慢
    tda_tool_wait_timeout: float = 5.0  # 首轮等 TDA 工具列表的最长时间;超时后主流程继续


def get_settings() -> Settings:
    """每次调用读取最新环境(便于测试覆盖)。"""
    return Settings()
