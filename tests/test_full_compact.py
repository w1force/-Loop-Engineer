import asyncio

import pytest

from core import full_compact as fc
from core.file_state import FileState
from core.loop.orchestrator import QueryParams, query_loop
from core.loop.phases import compact as compact_phase
from core.provider_errors import PromptTooLongError
from core.types import (
    AgentState,
    AssistantMessage,
    CompactBoundaryMessage,
    QueryState,
    SkillMeta,
    StreamEvent,
    TextBlock,
    Tombstone,
    UserMessage,
)
from telemetry.tracer import NoopTracer


class CountingProvider:
    def __init__(self, tokens: int):
        self.tokens = tokens

    def count_tokens(self, _messages):
        return self.tokens


def _params(provider, transcript_path: str | None = None) -> QueryParams:
    return QueryParams(
        system="parent system",
        model="parent-model",
        max_tokens=4096,
        provider=provider,
        abort_signal=asyncio.Event(),
        tools=[],
        transcript_path=transcript_path,
    )


def _text_events(text: str = "ok"):
    async def _gen():
        yield StreamEvent(type="message_start")
        yield StreamEvent(
            type="content_block_start",
            index=0,
            block={"type": "text", "text": ""},
        )
        yield StreamEvent(
            type="content_block_delta", index=0, delta={"text": text}
        )
        yield StreamEvent(type="content_block_stop", index=0)
        yield StreamEvent(
            type="message_delta",
            delta={"stop_reason": "end_turn"},
            message={"usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        yield StreamEvent(type="message_stop")

    return _gen()


def test_full_compact_prompt_and_format():
    prompt = fc.build_full_compact_prompt()
    assert "Do NOT call any tools" in prompt
    assert "All User Messages" in prompt
    assert "<summary>" in prompt
    assert (
        fc.format_compact_summary(
            "<analysis>draft</analysis><summary>kept details</summary>"
        )
        == "Summary:\nkept details"
    )


@pytest.mark.asyncio
async def test_full_compact_uses_cache_safe_fork_and_rebuilds_context(
    monkeypatch, tmp_path
):
    source = tmp_path / "source.py"
    source.write_text("print('fresh')\n", encoding="utf-8")
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("skill body", encoding="utf-8")
    skill = SkillMeta(
        name="audit",
        description="Audit source",
        skill_dir=tmp_path,
        skill_md=skill_file,
    )
    original = [
        UserMessage(content="implement it"),
        AssistantMessage(content=[TextBlock(text="working")]),
    ]
    state = AgentState(
        messages=original,
        skills=[skill],
        sent_skill_names={"audit"},
        cwd=str(tmp_path),
    )
    state.file_read_state.set(
        str(source),
        FileState(content="old", timestamp=1, offset=1, limit=None),
    )
    list_identity = state.messages
    captured = {}

    async def fake_fork(**kwargs):
        captured.update(kwargs)
        context = kwargs["fork_context_messages"]
        return AgentState(
            messages=[
                *context,
                UserMessage(content=kwargs["task_prompt"]),
                AssistantMessage(
                    content=[
                        TextBlock(
                            text="<analysis>draft</analysis><summary>all important state</summary>"
                        )
                    ]
                ),
            ]
        )

    monkeypatch.setattr(fc, "run_forked_agent", fake_fork)

    did = await fc.full_compact(
        state,
        _params(CountingProvider(200_000), str(tmp_path / "session.jsonl")),
        NoopTracer(),
        pre_compact_tokens=200_000,
    )

    assert did is True
    assert state.messages is list_identity
    assert isinstance(state.messages[0], CompactBoundaryMessage)
    assert state.messages[0].pre_tokens == 200_000
    assert "all important state" in state.messages[1].content
    assert "<analysis>" not in state.messages[1].content
    archives = list(tmp_path.glob("session.jsonl.precompact.*.jsonl"))
    assert len(archives) == 1
    assert "implement it" in archives[0].read_text(encoding="utf-8")
    assert str(archives[0]) in state.messages[1].content
    assert any("print('fresh')" in str(message.content) for message in state.messages)
    assert any("The following skills are available" in str(message.content) for message in state.messages)
    assert state.file_read_state.has(str(source))
    assert state.sm.generation == 1
    assert captured["parent_params"].system == "parent system"
    assert captured["parent_params"].model == "parent-model"
    assert captured["max_turns"] == 1
    assert captured["abort_signal"] is captured["parent_params"].abort_signal
    assert captured["propagate_errors"] is True


@pytest.mark.asyncio
async def test_auto_compact_falls_back_to_full_once(monkeypatch):
    monkeypatch.setenv("LOOP_ENGINEER_AUTOCOMPACT_TOKENS", "100")
    monkeypatch.setattr(compact_phase, "run_microcompact", lambda *_: None)

    async def no_session_memory(*_args):
        return False

    calls = []

    async def fake_full(agent_state, params, tracer, **kwargs):
        calls.append(kwargs)
        agent_state.messages[:] = [UserMessage(content="full summary")]
        return True

    monkeypatch.setattr(
        compact_phase, "maybe_session_memory_compact", no_session_memory
    )
    monkeypatch.setattr(compact_phase, "full_compact", fake_full)
    agent_state = AgentState(messages=[UserMessage(content="large")])
    state = QueryState.model_construct(messages=agent_state.messages, turn_count=1)

    next_state = await compact_phase.maybe_compact(
        agent_state, state, _params(CountingProvider(200)), NoopTracer()
    )

    assert len(calls) == 1
    assert calls[0]["pre_compact_tokens"] == 200
    assert agent_state.messages[0].content == "full summary"
    assert next_state.autocompact_consecutive_failures == 0


@pytest.mark.asyncio
async def test_auto_compact_circuit_breaker_stops_after_three_failures(monkeypatch):
    monkeypatch.setenv("LOOP_ENGINEER_AUTOCOMPACT_TOKENS", "100")
    monkeypatch.setattr(compact_phase, "run_microcompact", lambda *_: None)

    async def no_session_memory(*_args):
        return False

    calls = 0

    async def failed_full(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(
        compact_phase, "maybe_session_memory_compact", no_session_memory
    )
    monkeypatch.setattr(compact_phase, "full_compact", failed_full)
    agent_state = AgentState(messages=[UserMessage(content="large")])
    state = QueryState.model_construct(messages=agent_state.messages, turn_count=1)
    params = _params(CountingProvider(200))

    for _ in range(4):
        state = await compact_phase.maybe_compact(
            agent_state, state, params, NoopTracer()
        )

    assert calls == 3
    assert state.autocompact_consecutive_failures == 3


@pytest.mark.asyncio
async def test_prompt_too_long_runs_single_reactive_full_compact(monkeypatch):
    class ScriptedProvider:
        def __init__(self):
            self.calls = 0

        def count_tokens(self, _messages):
            return 0

        def stream(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise PromptTooLongError("too long", status=400)
            return _text_events("continued")

    compact_calls = 0

    async def fake_full(agent_state, params, tracer, **_kwargs):
        nonlocal compact_calls
        compact_calls += 1
        agent_state.messages[:] = [
            CompactBoundaryMessage(pre_tokens=200_000),
            UserMessage(content="summary"),
        ]
        return True

    monkeypatch.setattr(fc, "full_compact", fake_full)
    provider = ScriptedProvider()
    agent_state = AgentState(messages=[UserMessage(content="huge")])

    out = [
        item
        async for item in query_loop(
            agent_state, _params(provider), NoopTracer()
        )
    ]

    assert compact_calls == 1
    assert provider.calls == 2
    assert any(isinstance(item, Tombstone) for item in out)
    assert any(
        isinstance(item, AssistantMessage)
        and item.content
        and item.content[0].text == "continued"
        for item in out
    )


@pytest.mark.asyncio
async def test_prompt_too_long_reactive_path_uses_full_compact_fork():
    class ScriptedProvider:
        def __init__(self):
            self.scripts = [
                PromptTooLongError("too long", status=400),
                _text_events(
                    "<analysis>draft</analysis><summary>preserved state</summary>"
                ),
                _text_events("continued after compact"),
            ]
            self.calls = 0

        def count_tokens(self, _messages):
            return 0

        def stream(self, **_kwargs):
            script = self.scripts[self.calls]
            self.calls += 1
            if isinstance(script, Exception):
                raise script
            return script

    provider = ScriptedProvider()
    agent_state = AgentState(
        messages=[
            UserMessage(content="large request"),
            AssistantMessage(content=[TextBlock(text="old work")]),
            UserMessage(content="continue"),
        ]
    )
    out = [
        item
        async for item in query_loop(
            agent_state, _params(provider), NoopTracer()
        )
    ]

    assert provider.calls == 3
    assert isinstance(agent_state.messages[0], CompactBoundaryMessage)
    assert "preserved state" in agent_state.messages[1].content
    assert any(
        isinstance(item, AssistantMessage)
        and item.content[0].text == "continued after compact"
        for item in out
    )


@pytest.mark.asyncio
async def test_full_compact_retries_when_compact_request_is_too_long():
    class ScriptedProvider:
        def __init__(self):
            self.scripts = [
                PromptTooLongError("compact too long", status=400),
                _text_events("<summary>summary after truncation</summary>"),
            ]
            self.calls = 0

        def count_tokens(self, _messages):
            return 200_000

        def stream(self, **_kwargs):
            script = self.scripts[self.calls]
            self.calls += 1
            if isinstance(script, Exception):
                raise script
            return script

    messages = []
    for index in range(6):
        messages.extend(
            [
                UserMessage(content=f"user-{index}"),
                AssistantMessage(content=[TextBlock(text=f"assistant-{index}")]),
            ]
        )
    provider = ScriptedProvider()
    agent_state = AgentState(messages=messages)

    did = await fc.full_compact(
        agent_state, _params(provider), NoopTracer(), pre_compact_tokens=200_000
    )

    assert did is True
    assert provider.calls == 2
    assert "summary after truncation" in agent_state.messages[1].content


def test_truncate_head_for_compact_retry_keeps_a_complete_recent_round():
    messages = [
        UserMessage(content=f"user-{i}")
        if i % 2 == 0
        else AssistantMessage(content=[TextBlock(text=f"assistant-{i}")])
        for i in range(10)
    ]
    truncated = fc.truncate_head_for_compact_retry(messages)
    assert truncated is not None
    assert len(truncated) < len(messages)
    assert isinstance(truncated[0], UserMessage)
