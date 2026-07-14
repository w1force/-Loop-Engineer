"""parse_sse: SSE 字节流 → data 字符串序列(只 yield str)。

覆盖红线: 多行 data 用 \\n 拼接、注释行忽略、[DONE] 原样、流末无空行补 yield。
"""
import json

from core.providers._sse import parse_sse


class FakeResponse:
    """模拟 httpx.Response.aiter_lines():按行 yield(保留空行)。"""

    def __init__(self, text: str):
        self._lines = text.split("\n")

    async def aiter_lines(self):
        for line in self._lines:
            yield line


async def _collect(resp) -> list[str]:
    return [x async for x in parse_sse(resp)]


async def test_multiline_data_joined_with_newline():
    resp = FakeResponse('data: {"type":"message_start"}\ndata: {"extra":"more"}\n\n')
    out = await _collect(resp)
    assert out == ['{"type":"message_start"}\n{"extra":"more"}']


async def test_comment_lines_ignored():
    resp = FakeResponse(': keep-alive\ndata: {"type":"x"}\n\n')
    out = await _collect(resp)
    assert out == ['{"type":"x"}']


async def test_done_yielded_as_string():
    resp = FakeResponse("data: [DONE]\n\n")
    out = await _collect(resp)
    assert out == ["[DONE]"]


async def test_trailing_no_blank_line_still_yielded():
    # 流末无收尾空行 → 补 yield(常是带 stop_reason 的 message_delta)
    resp = FakeResponse('data: {"type":"message_stop"}')
    out = await _collect(resp)
    assert out == ['{"type":"message_stop"}']


async def test_data_prefix_without_space_stripped():
    # "data:" 后无空格也应正确剥离
    resp = FakeResponse('data:{"type":"x"}\n\n')
    out = await _collect(resp)
    assert out == ['{"type":"x"}']


async def test_realistic_sequence_in_order():
    text = (
        'data: {"type":"message_start"}\n\n'
        'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
        ': keep-alive\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        "data: [DONE]\n\n"
    )
    out = await _collect(FakeResponse(text))
    kinds = [json.loads(s)["type"] for s in out[:-1]]
    assert kinds == ["message_start", "content_block_delta", "message_delta"]
    assert out[-1] == "[DONE]"
