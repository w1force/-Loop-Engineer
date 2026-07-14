"""transcript: JSONL 持久化的写入/读回 round-trip。"""
import json

from core.transcript import load_transcript, record_transcript
from core.types import AssistantMessage, TextBlock, Usage, UserMessage


async def test_record_and_load_roundtrip(tmp_path):
    path = tmp_path / "t.jsonl"
    msgs = [
        UserMessage(content="你好"),
        AssistantMessage(
            content=[TextBlock(text="hi")],
            usage=Usage(input_tokens=5, output_tokens=3),
            stop_reason="end_turn",
        ),
    ]
    await record_transcript(msgs, path)

    # 每条消息一行 JSON,ensure_ascii=False(中文不转义)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["role"] == "user"
    assert "你好" in lines[0]  # 中文原样保留

    loaded = load_transcript(path)
    assert len(loaded) == 2
    assert isinstance(loaded[0], UserMessage)
    assert isinstance(loaded[1], AssistantMessage)
    assert loaded[1].usage.output_tokens == 3
    assert loaded[1].content[0].text == "hi"
