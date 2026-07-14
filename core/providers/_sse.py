"""SSE 解析(共享,~30 行)(P1 §5.5 改进版)。

契约: parse_sse **只 yield str**(每个 SSE event 的 data 字段;[DONE] 原样 yield 成
字符串)。调用方自行 json.loads(除 [DONE])。三 provider 共用这一个函数。
"""
from collections.abc import AsyncIterator

import httpx


async def parse_sse(resp: httpx.Response) -> AsyncIterator[str]:
    """yield 每个 SSE event 的 data 字符串(可能为 '[DONE]')。"""
    data_lines: list[str] = []
    async for line in resp.aiter_lines():
        if line == "":  # 空行 = 一个 event 结束
            if data_lines:
                yield "\n".join(data_lines)  # 多个 data: 行按 SSE 规范用 \n 拼接
                data_lines = []
            continue
        if line.startswith(":"):  # 注释行(如 ": keep-alive"),忽略
            continue
        if line.startswith("data:"):
            # 剥 "data:" 前缀,再剥一个可选空格(SSE 规范)
            data_lines.append(line[5:].lstrip(" ") if len(line) > 5 else "")
        # 其它字段(event: / id: / retry:)本计划用不到,忽略
    if data_lines:  # 流结束但无收尾空行 → 补 yield(常是带 stop_reason 的 message_delta)
        yield "\n".join(data_lines)
