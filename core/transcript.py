"""JSONL 持久化 (P1 §9)。

每条消息一行 JSON。record_transcript 每次传累积的全量 messages,覆写式
重建完整 transcript(对齐 agent_loop 每轮持久化全量的语义)。
"""
import json

import aiofiles

from .types import AssistantMessage, Message, UserMessage


async def record_transcript(messages: list[Message], path) -> None:
    """覆写式写入全量 messages。"""
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        for m in messages:
            await f.write(json.dumps(m.model_dump(), ensure_ascii=False) + "\n")


def load_transcript(path) -> list[Message]:
    """读 JSONL 重建 messages(按 role 判别)。Phase 3 resume 用,Phase 1 先实现。"""
    out: list[Message] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("role") == "user":
                out.append(UserMessage.model_validate(obj))
            else:
                out.append(AssistantMessage.model_validate(obj))
    return out
