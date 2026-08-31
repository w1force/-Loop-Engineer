"""Small text-generation adapter over the project's streaming Provider protocol."""

from __future__ import annotations

import asyncio

from core.provider import Provider
from core.types import UserMessage
from telemetry.tracer import NoopTracer, Tracer


class ProviderTextGenerator:
    """Collect only final text blocks; thinking and tool calls are never parsed as JSON."""

    def __init__(self, provider: Provider, *, tracer: Tracer | None = None) -> None:
        self.provider = provider
        self.tracer = tracer or NoopTracer()

    async def generate(
        self, *, system: str, prompt: str, model: str, max_tokens: int
    ) -> str:
        blocks: dict[int, dict[str, str]] = {}
        async for event in self.provider.stream(
            messages=[UserMessage(content=prompt)],
            system=system,
            tools=[],
            model=model,
            max_tokens=max_tokens,
            abort_signal=asyncio.Event(),
            tracer=self.tracer,
            # Short summarization/distillation budgets can be lower than
            # Anthropic's minimum thinking budget. Only Repair trajectory capture
            # needs provider-visible reasoning; these helper calls need final text.
            thinking_budget_tokens=0,
        ):
            if event.index is None:
                continue
            if event.type == "content_block_start":
                block = event.block or {}
                blocks[event.index] = {
                    "type": str(block.get("type") or ""),
                    "text": str(block.get("text") or ""),
                }
            elif event.type == "content_block_delta":
                current = blocks.get(event.index)
                if current is None or current["type"] != "text":
                    continue
                delta = event.delta or {}
                if isinstance(delta.get("text"), str):
                    current["text"] += delta["text"]
        return "".join(
            blocks[index]["text"]
            for index in sorted(blocks)
            if blocks[index]["type"] == "text"
        ).strip()


__all__ = ["ProviderTextGenerator"]
