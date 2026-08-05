"""配置: pydantic-settings 从环境变量/.env 读取。

所有键统一加 `LOOP_ENGINEER_` 前缀,**刻意避免**与 Anthropic 官方 SDK 的
`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` 等约定变量冲突(装了官方 SDK 会自动
读那两个)。config 层 provider 中立,Phase 4 接 OpenAI 时同一套配置也适用。

环境变量(.env 同名键):
    LOOP_ENGINEER_API_KEY     API key(端到端验收需要)
    LOOP_ENGINEER_BASE_URL    默认官方 https://api.anthropic.com
    LOOP_ENGINEER_USE_HTTP_PROXY_ENV
                              是否让 httpx 读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 等环境
                              代理变量; 默认 false(本项目直连, 避免本机 SOCKS 代理触发
                              socksio 缺失报错)。需要走环境代理时设 true。
    LOOP_ENGINEER_MODEL       模型 id
    LOOP_ENGINEER_MAX_TOKENS  单次生成上限
    LOOP_ENGINEER_MAX_TURNS   内层 query_loop 最大轮次守卫
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOOP_ENGINEER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # 忽略 ANTHROPIC_* 等无关变量,绝不串味
    )

    api_key: str = ""
    base_url: str = "https://api.anthropic.com"
    # 是否让 httpx 读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 等环境代理。默认 False:
    # 本项目自带 base_url 直连即可, 避免本机 SOCKS 代理(httpx 处理 socks5 需 socksio)
    # 导致 ImportError。需要走环境代理时设 LOOP_ENGINEER_USE_HTTP_PROXY_ENV=true。
    # (对应 httpx.AsyncClient(trust_env=...); 此处用更直白的名字避免与 httpx 内部
    # 参数名 trust_env 混淆,USE_HTTP_PROXY_ENV 一眼看出语义 = "用环境变量里的代理"。)
    use_http_proxy_env: bool = False
    model: str = "claude-sonnet-4-6"
    review_model: str | None = None  # None 时 reviewer 复用 model
    diagnosis_max_rework_rounds: int = 1
    max_tokens: int = 4096
    max_turns: int = 20
    debug_sse: bool = False  # LOOP_ENGINEER_DEBUG_SSE=true 时打印原始 SSE 流
    run_log_enabled: bool = True  # ★ 结构化运行日志(FileTracer 写 JSONL, jq 可查)
    run_log_path: str | None = None  # None → FileTracer 默认 logs/{时间戳}.jsonl;设了则原样用(不拼接)


def get_settings() -> Settings:
    """返回进程级单例 Settings(懒加载)。

    首次调用从 .env / 环境变量构造一次并缓存,之后所有调用共享同一实例 ——
    配置语义上应进程内一致,且避免反复读 .env / 解析环境变量的开销。

    需要重新读取环境(测试覆盖、运行时热更)时调 reset_settings() 清缓存,
    下次 get_settings() 会重新构造。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


_settings: Settings | None = None


def reset_settings() -> None:
    """清空缓存的 Settings 单例。下次 get_settings() 重新从环境读取。

    主要供测试覆盖配置:在测试里用 monkeypatch 改环境变量后调用此函数,
    再 get_settings() 即可拿到反映新环境的实例。
    """
    global _settings
    _settings = None
