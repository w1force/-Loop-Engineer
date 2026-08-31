from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
from threading import Barrier

import pytest

from core.learning.archive import _atomic_json
from core.learning.catalog import LearnedSkillCatalog
from core.learning.models import (
    ExperienceSkill,
    HumanReviewDecision,
    ReviewStatus,
    ShareGPTTrajectory,
    SkillMatch,
    SkillProvenance,
)
from core.learning.trajectory import (
    CompressionConfig,
    TrajectoryCompressionError,
    TrajectoryCompressor,
    extract_reasoning_blocks,
    load_sharegpt_trajectory,
    messages_to_sharegpt,
    record_sharegpt_trajectory,
)
from core.stages.diagnosis import DiagnosisStage
from core.stages.repair import RepairStage
from core.types import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from telemetry.tracer import NoopTracer


class _RecordingGenerator:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def generate(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected generator call")
        return self.responses.pop(0)


def _review(run_id: str = "run-1") -> HumanReviewDecision:
    return HumanReviewDecision(
        run_id=run_id,
        repository="acme/service",
        pull_request_number=7,
        commit_sha="a" * 40,
        status=ReviewStatus.APPROVED,
        reviewer="owner",
        review_id=11,
    )


def _trajectory(
    conversations: tuple[dict, ...], *, run_id: str = "run-1"
) -> ShareGPTTrajectory:
    return ShareGPTTrajectory(
        run_id=run_id,
        incident_id="incident-1",
        cycle=1,
        conversations=conversations,
        model="model-a",
        completed=True,
        terminal_reason="completed",
        reasoning_blocks=(
            {"turn": 1, "type": "thinking", "thinking": "root cause"},
        ),
    )


def _skill(
    name: str,
    *,
    matched_rule: str = "checkout-timeout",
    signature_code: str = "checkout.timeout",
    error_type: str | None = "TimeoutError",
    event_code: str | None = None,
    message_pattern: str | None = "timed out",
    source_paths: tuple[str, ...] = ("service/checkout.py",),
    revision: int = 1,
) -> ExperienceSkill:
    return ExperienceSkill(
        name=name,
        description=f"Historical repair for {signature_code}.",
        match=SkillMatch(
            matched_rule=matched_rule,
            signature_code=signature_code,
            error_type=error_type,
            event_code=event_code,
            message_pattern=message_pattern,
            source_paths=source_paths,
        ),
        applicable_when=("the same failure signature is reproduced",),
        diagnosis_steps=("confirm the failing branch",),
        repair_steps=("apply the bounded fallback",),
        pitfalls=("do not weaken verification",),
        provenance=(
            SkillProvenance(
                run_id="run-1",
                incident_id="incident-1",
                candidate_digest="b" * 64,
                report_digest="c" * 64,
                review_digest="d" * 64,
                compressed_trajectory_path="/archive/run-1.json",
            ),
        ),
        revision=revision,
    )


def _search_query() -> dict:
    return {
        "matched_rule": "checkout-timeout",
        "signature_code": "checkout.timeout",
        "error_type": "TimeoutError",
        "message": "upstream checkout timed out",
        "source_paths": ["service/checkout.py"],
    }


def _race_atomic_writes(
    path: Path,
    payloads: list[dict],
    *,
    replace: bool,
) -> list[str]:
    barrier = Barrier(len(payloads))

    def write(payload: dict) -> str:
        barrier.wait(timeout=5)
        try:
            _atomic_json(
                path,
                payload,
                replace=replace,
            )
        except FileExistsError:
            return "conflict"
        return "success"

    with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
        return list(executor.map(write, payloads))


def test_atomic_json_concurrent_create_is_exclusive_and_idempotent(
    tmp_path: Path,
) -> None:
    create_path = tmp_path / "create.json"
    different = [{"writer": index} for index in range(12)]
    outcomes = _race_atomic_writes(
        create_path, different, replace=False
    )
    assert outcomes.count("success") == 1
    assert outcomes.count("conflict") == len(different) - 1
    assert json.loads(create_path.read_text(encoding="utf-8")) in different

    idempotent_path = tmp_path / "idempotent.json"
    same = {"stable": ["same", "content"]}
    outcomes = _race_atomic_writes(
        idempotent_path, [same] * 12, replace=False
    )
    assert outcomes == ["success"] * 12
    assert json.loads(idempotent_path.read_text(encoding="utf-8")) == same


def test_messages_to_sharegpt_preserves_roles_tools_and_redacts_secrets() -> None:
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        "private-key-material\n"
        "-----END PRIVATE KEY-----"
    )
    conversations = messages_to_sharegpt(
        system="repair system; Bearer system-token; sk-proj-ABC12345",
        messages=[
            UserMessage(
                content=(
                    "diagnosis result; Bearer user-token; "
                    "github=ghp_1234567890abcdef"
                )
            ),
            AssistantMessage(
                content=[
                    TextBlock(text="inspect and run the reproducer"),
                    ToolUseBlock(
                        id="tool-1",
                        name="Bash",
                        input={
                            "command": "curl https://example.invalid",
                            "authorization": "Bearer tool-token",
                            "nested": {"api_key": "private-key"},
                        },
                    ),
                ]
            ),
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tool-1",
                        content=(
                            "request failed; Bearer result-token; "
                            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/example; "
                            "access id AKIAIOSFODNN7EXAMPLE\n" + private_key
                        ),
                        is_error=True,
                    )
                ]
            ),
        ],
    )

    assert [item["from"] for item in conversations] == [
        "system",
        "human",
        "gpt",
        "tool",
    ]
    assert conversations[2]["value"] == "inspect and run the reproducer"
    assert conversations[2]["tool_calls"] == [
        {
            "type": "tool_use",
            "id": "tool-1",
            "name": "Bash",
            "input": {
                "command": "curl https://example.invalid",
                "authorization": "[REDACTED]",
                "nested": {"api_key": "[REDACTED]"},
            },
        }
    ]
    assert conversations[3]["from"] == "tool"
    assert conversations[3]["tool_use_id"] == "tool-1"
    assert conversations[3]["is_error"] is True
    serialized = json.dumps(conversations)
    assert "system-token" not in serialized
    assert "user-token" not in serialized
    assert "tool-token" not in serialized
    assert "private-key" not in serialized
    assert "result-token" not in serialized
    assert "sk-proj-ABC12345" not in serialized
    assert "ghp_1234567890abcdef" not in serialized
    assert "wJalrXUtnFEMI/K7MDENG/example" not in serialized
    assert "AKIAIOSFODNN7EXAMPLE" not in serialized
    assert "private-key-material" not in serialized


@pytest.mark.asyncio
async def test_noop_tracer_keeps_message_reasoning_eligible_for_learning(
    tmp_path: Path,
) -> None:
    path = await record_sharegpt_trajectory(
        transcript_path=tmp_path / "repair.transcript.jsonl",
        system="repair SOP",
        messages=[
            UserMessage(content="repair this incident"),
            AssistantMessage(
                content=[
                    ThinkingBlock(thinking="trace the failing fallback branch"),
                    TextBlock(text='{"implementation_summary":"fixed"}'),
                ]
            ),
        ],
        tools=[],
        model="model-a",
        completed=True,
        terminal_reason="completed",
        context={
            "run_id": "run-noop-tracer",
            "incident_id": "incident-1",
            "stage": "repair",
            "cycle": 1,
        },
        tracer=NoopTracer(),
    )

    trajectory = load_sharegpt_trajectory(path)
    compressed = await TrajectoryCompressor(
        generator=_RecordingGenerator(),
        config=CompressionConfig(target_max_tokens=100_000),
    ).compress(trajectory, review=_review("run-noop-tracer"))

    assert trajectory.trace_path is None
    assert trajectory.reasoning_blocks == (
        {
            "turn": 1,
            "type": "thinking",
            "thinking": "trace the failing fallback branch",
        },
    )
    assert compressed.usable_reasoning_count == 1
    assert compressed.eligible_for_skill is True


def test_extract_reasoning_blocks_filters_trace_context_and_redacts(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    records = [
        {
            "kind": "llm_response",
            "run_id": "run-1",
            "stage": "repair",
            "turn": 2,
            "payload": {
                "blocks": [
                    {
                        "type": "thinking",
                        "thinking": "inspect Bearer visible-token",
                    },
                    {
                        "type": "redacted_thinking",
                        "thinking": "provider marker Bearer redacted-token",
                    },
                    {"type": "text", "text": "ordinary response"},
                    {
                        "type": "thinking",
                        "thinking": {
                            "api_key": "nested-key",
                            "note": "retry Bearer nested-token",
                        },
                    },
                ]
            },
        },
        {
            "kind": "llm_response",
            "run_id": "another-run",
            "stage": "repair",
            "turn": 3,
            "payload": {
                "blocks": [
                    {"type": "thinking", "thinking": "must be ignored"}
                ]
            },
        },
        {
            "kind": "tool_result",
            "run_id": "run-1",
            "stage": "repair",
            "payload": {
                "blocks": [{"type": "thinking", "thinking": "also ignored"}]
            },
        },
    ]
    trace.write_text(
        "not-json\n" + "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    blocks = extract_reasoning_blocks(
        str(trace), context={"run_id": "run-1", "stage": "repair"}
    )

    assert blocks == (
        {
            "turn": 2,
            "type": "thinking",
            "thinking": "inspect Bearer [REDACTED]",
        },
        {
            "turn": 2,
            "type": "redacted_thinking",
            "thinking": "provider marker Bearer [REDACTED]",
        },
        {
            "turn": 2,
            "type": "thinking",
            "thinking": {
                "api_key": "[REDACTED]",
                "note": "retry Bearer [REDACTED]",
            },
        },
    )


@pytest.mark.asyncio
async def test_small_trajectory_is_returned_without_summarization() -> None:
    generator = _RecordingGenerator()
    conversations = (
        {"from": "system", "value": "repair SOP"},
        {"from": "human", "value": "diagnosis result"},
        {"from": "gpt", "value": "repair complete"},
    )
    compressor = TrajectoryCompressor(
        generator=generator,
        config=CompressionConfig(target_max_tokens=10),
        token_counter=lambda _: 10,
    )

    result = await compressor.compress(_trajectory(conversations), review=_review())

    assert result.compressed is False
    assert result.summary is None
    assert result.conversations == conversations
    assert result.reasoning_blocks[0]["thinking"] == "root cause"
    assert result.original_tokens == result.compressed_tokens == 10
    assert generator.calls == []


@pytest.mark.asyncio
async def test_large_trajectory_summarizes_only_middle_and_preserves_head_and_tail() -> None:
    generator = _RecordingGenerator("attempts one and two failed before the final fix")
    conversations: tuple[dict, ...] = (
        {"from": "system", "value": "SYSTEM_HEAD"},
        {"from": "human", "value": "LEARNED_SKILL_LISTING"},
        {"from": "human", "value": "INCIDENT_JSON:\nDIAGNOSIS_HEAD"},
        {
            "from": "gpt",
            "value": "assistant-1",
            "tool_calls": [
                {
                    "name": "Load_Skill",
                    "input": {"name": "learned-repair-timeout"},
                }
            ],
        },
        {"from": "tool", "value": "tool-1"},
        {"from": "human", "value": "feedback-1"},
        {
            "from": "gpt",
            "value": "assistant-2",
            "reasoning": [{"type": "thinking", "thinking": "middle reasoning"}],
        },
        {"from": "tool", "value": "tool-2"},
        {"from": "human", "value": "feedback-2"},
        {"from": "gpt", "value": "assistant-3"},
        {"from": "tool", "value": "tool-3"},
        {"from": "human", "value": "feedback-3"},
        {"from": "gpt", "value": "assistant-4"},
        {"from": "tool", "value": "tool-4"},
        {"from": "human", "value": "feedback-4"},
        {"from": "gpt", "value": "assistant-5"},
        {"from": "tool", "value": "tool-5"},
        {"from": "human", "value": "feedback-5"},
        {
            "from": "gpt",
            "value": "assistant-6",
            "reasoning": [{"type": "thinking", "thinking": "tail reasoning"}],
        },
        {"from": "tool", "value": "tool-6"},
    )
    compressor = TrajectoryCompressor(
        generator=generator,
        config=CompressionConfig(
            target_max_tokens=900,
            summary_target_tokens=37,
            protect_last_n_turns=4,
            summarization_model="summary-model",
        ),
        token_counter=len,
    )

    result = await compressor.compress(_trajectory(conversations), review=_review())

    assert result.compressed is True
    assert result.compressed_tokens <= 900
    assert result.conversations[:3] == conversations[:3]
    assert result.conversations[3] == {
        "from": "system",
        "value": (
            "[COMPRESSED_TRAJECTORY_MIDDLE]\n"
            "attempts one and two failed before the final fix"
        ),
    }
    tail_start = 9
    assert result.conversations[4:] == conversations[tail_start:]
    assert [
        item["value"]
        for item in result.conversations
        if item["from"] == "gpt"
    ] == ["assistant-3", "assistant-4", "assistant-5", "assistant-6"]

    assert len(generator.calls) == 1
    call = generator.calls[0]
    assert call["model"] == "summary-model"
    assert call["max_tokens"] == 37
    middle_json = json.loads(call["prompt"].split("\n\n", 1)[1])
    assert middle_json["conversations"] == list(conversations[3:tail_start])
    assert "middle reasoning" in generator.calls[0]["prompt"]
    assert middle_json["reasoning_blocks"][0]["thinking"] == "root cause"
    assert result.reasoning_blocks == ()
    assert result.conversations[-2]["reasoning"][0]["thinking"] == "tail reasoning"
    assert result.loaded_skill_names == ("learned-repair-timeout",)


@pytest.mark.asyncio
async def test_compressor_redacts_middle_before_external_summary() -> None:
    generator = _RecordingGenerator("reuse ghp_1234567890abcdef")
    secret = "OPENAI_API_KEY=sk-proj-ABC12345"
    conversations = (
        {"from": "system", "value": "repair SOP"},
        {"from": "human", "value": "diagnosis result"},
        {"from": "gpt", "value": (secret + " ") * 30},
        {"from": "tool", "value": "AWS_SECRET_ACCESS_KEY=aws-secret-value"},
        {"from": "gpt", "value": "final repair"},
    )
    compressor = TrajectoryCompressor(
        generator=generator,
        config=CompressionConfig(
            target_max_tokens=300,
            protect_last_n_turns=1,
        ),
        token_counter=len,
    )

    result = await compressor.compress(_trajectory(conversations), review=_review())

    assert len(generator.calls) == 1
    assert "sk-proj-ABC12345" not in generator.calls[0]["prompt"]
    assert "aws-secret-value" not in generator.calls[0]["prompt"]
    assert "ghp_1234567890abcdef" not in json.dumps(result.conversations)
    assert result.compressed_tokens <= 300


@pytest.mark.asyncio
async def test_compressor_fails_when_protected_regions_exceed_target() -> None:
    generator = _RecordingGenerator("must not be requested")
    conversations = (
        {"from": "system", "value": "S" * 200},
        {"from": "human", "value": "H" * 200},
        {"from": "gpt", "value": "compressible middle"},
        {"from": "tool", "value": "middle result"},
        {"from": "gpt", "value": "T" * 200},
    )
    compressor = TrajectoryCompressor(
        generator=generator,
        config=CompressionConfig(
            target_max_tokens=50,
            protect_last_n_turns=1,
        ),
        token_counter=len,
    )

    with pytest.raises(
        TrajectoryCompressionError,
        match="protected trajectory head/tail exceed target_max_tokens",
    ):
        await compressor.compress(_trajectory(conversations), review=_review())
    assert generator.calls == []


@pytest.mark.asyncio
async def test_compressor_rejects_an_oversized_generated_summary() -> None:
    generator = _RecordingGenerator("summary" * 100)
    conversations = (
        {"from": "system", "value": "repair SOP"},
        {"from": "human", "value": "diagnosis result"},
        {"from": "gpt", "value": "M" * 500},
        {"from": "gpt", "value": "final repair"},
    )
    compressor = TrajectoryCompressor(
        generator=generator,
        config=CompressionConfig(
            target_max_tokens=300,
            protect_last_n_turns=1,
        ),
        token_counter=len,
    )

    with pytest.raises(
        TrajectoryCompressionError,
        match="trajectory summarization did not meet target_max_tokens",
    ):
        await compressor.compress(_trajectory(conversations), review=_review())
    assert len(generator.calls) == 1


def test_catalog_enforces_prefix_and_rejects_symlink_paths(tmp_path: Path) -> None:
    catalog = LearnedSkillCatalog(tmp_path / "skills")

    assert catalog.deterministic_name(
        signature_code="Checkout.Timeout/HTTP",
        family_digest="e" * 64,
    ) == "learned-repair-checkout-timeout-http-eeeeeeee"
    assert len(
        catalog.deterministic_name(
            signature_code="very-long-signature-code-" * 8,
            family_digest="e" * 64,
        )
    ) < 64
    with pytest.raises(ValueError, match="allowed prefix"):
        catalog.get("verification-sop-timeout")
    with pytest.raises(ValueError, match="prefix"):
        LearnedSkillCatalog(tmp_path / "invalid", name_prefix="Learned-")

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ValueError, match="root cannot be a symlink"):
        LearnedSkillCatalog(linked_root)

    catalog.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_skill = catalog.root / "learned-repair-linked"
    linked_skill.symlink_to(outside, target_is_directory=True)
    assert catalog.list() == ()
    with pytest.raises(ValueError, match="escapes its root"):
        catalog.write(_skill("learned-repair-linked"), expected_revision=0)


def test_catalog_rejects_a_user_symlink_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="ancestor cannot be a symlink"):
        LearnedSkillCatalog(linked_parent / "skills")
    assert not (outside / "skills").exists()


def test_catalog_normalizes_the_macos_var_system_alias(tmp_path: Path) -> None:
    private_var = Path("/private/var")
    var_alias = Path("/var")
    if not var_alias.is_symlink() or var_alias.resolve() != private_var:
        pytest.skip("macOS /var system alias is not present")
    try:
        relative_tmp = tmp_path.resolve().relative_to(private_var)
    except ValueError:
        pytest.skip("pytest temp directory is not below /private/var")

    alias_root = var_alias / relative_tmp / "alias-skills"
    catalog = LearnedSkillCatalog(alias_root)

    assert catalog.root == tmp_path.resolve() / "alias-skills"
    skill = _skill("learned-repair-system-alias-a")
    catalog.write(skill, expected_revision=0)
    assert catalog.get(skill.name) == skill


def test_catalog_returns_frozen_snapshot_and_fails_closed_on_tamper(
    tmp_path: Path,
) -> None:
    catalog = LearnedSkillCatalog(tmp_path / "skills")
    skill = _skill("learned-repair-checkout-timeout-a")
    catalog.write(skill, expected_revision=0)

    metas = catalog.skill_metas(query=_search_query())
    assert len(metas) == 1
    meta = metas[0]
    assert meta.snapshot_text is not None
    assert meta.digest == sha256(meta.snapshot_text.encode("utf-8")).hexdigest()
    frozen_text = meta.snapshot_text

    meta.skill_md.write_text(frozen_text + "\nTAMPERED\n", encoding="utf-8")
    assert meta.snapshot_text == frozen_text
    assert catalog.get(skill.name) is None
    assert catalog.search(query=_search_query()) == ()
    assert catalog.skill_metas(query=_search_query()) == []

    catalog.write(skill, expected_revision=skill.revision)
    metadata = json.loads((meta.skill_dir / "metadata.json").read_text(encoding="utf-8"))
    metadata["description"] = "tampered metadata"
    (meta.skill_dir / "metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    assert catalog.list() == ()

    catalog.write(skill, expected_revision=skill.revision)
    outside = tmp_path / "outside-skill.md"
    outside.write_text(meta.skill_md.read_text(encoding="utf-8"), encoding="utf-8")
    meta.skill_md.unlink()
    meta.skill_md.symlink_to(outside)
    assert catalog.list() == ()
    assert catalog.skill_metas(query=_search_query()) == []


def test_catalog_write_uses_revision_compare_and_swap(tmp_path: Path) -> None:
    catalog = LearnedSkillCatalog(tmp_path / "skills")
    first = _skill("learned-repair-checkout-timeout-a", revision=1)
    first_digest = catalog.write(first, expected_revision=0)
    assert len(first_digest) == 64

    second = first.model_copy(
        update={
            "revision": 2,
            "repair_steps": ("apply the bounded fallback", "add a regression test"),
        }
    )
    second_digest = catalog.write(second, expected_revision=1)
    assert second_digest != first_digest
    assert catalog.get(first.name) == second

    stale = second.model_copy(update={"revision": 3})
    with pytest.raises(RuntimeError, match=r"expected=1, actual=2"):
        catalog.write(stale, expected_revision=1)
    assert catalog.get(first.name) == second


def test_catalog_rejects_skill_creator_invalid_generated_content(tmp_path: Path) -> None:
    catalog = LearnedSkillCatalog(tmp_path / "skills")
    valid = _skill("learned-repair-checkout-timeout-a")

    with pytest.raises(ValueError, match="angle brackets"):
        catalog.write(
            valid.model_copy(update={"description": "Repair <service> timeouts"}),
            expected_revision=0,
        )
    with pytest.raises(ValueError, match="unfinished TODO"):
        catalog.write(
            valid.model_copy(update={"repair_steps": ("[TODO: add repair]",)}),
            expected_revision=0,
        )

    generated = catalog.deterministic_name(
        signature_code="x" * 500,
        family_digest="a" * 64,
    )
    assert len(generated) <= 64

def test_catalog_search_requires_strong_match_and_ranks_exact_signature(
    tmp_path: Path,
) -> None:
    catalog = LearnedSkillCatalog(tmp_path / "skills")
    exact = _skill("learned-repair-checkout-timeout-a")
    same_rule = _skill(
        "learned-repair-checkout-timeout-b",
        signature_code="checkout.connection-reset",
        error_type="ConnectionError",
        message_pattern="connection reset",
        source_paths=("service/network.py",),
    )
    weak_only = _skill(
        "learned-repair-unrelated-c",
        matched_rule="inventory-failure",
        signature_code="inventory.unavailable",
        error_type=None,
        message_pattern="timed out",
    )
    for skill in (exact, same_rule, weak_only):
        catalog.write(skill, expected_revision=0)

    matches = catalog.search(query=_search_query())
    assert [skill.name for skill in matches] == [exact.name, same_rule.name]
    assert catalog.search(query=_search_query(), limit=1) == (exact,)

    weak_query = {
        "message": "upstream checkout timed out",
        "source_paths": ["service/checkout.py"],
    }
    assert catalog.search(query=weak_query) == ()


def test_diagnosis_and_repair_share_the_dynamic_default_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "learned"
    monkeypatch.setenv("LOOP_ENGINEER_LEARNED_SKILLS_ROOT", str(root))

    diagnosis = DiagnosisStage()
    repair = RepairStage(workspace_ignore=())

    assert diagnosis.learned_skill_catalog.root == root
    assert repair.learned_skill_catalog.root == root


def test_sharegpt_answer_text_does_not_duplicate_reasoning() -> None:
    conversations = messages_to_sharegpt(
        system="repair SOP",
        messages=[
            UserMessage(content="repair this"),
            AssistantMessage(
                content=[
                    ThinkingBlock(thinking="private provider-visible reasoning"),
                    TextBlock(text='{"implementation_summary":"done"}'),
                ]
            ),
        ],
    )

    assert conversations[-1]["value"] == '{"implementation_summary":"done"}'
    assert "private provider-visible reasoning" not in conversations[-1]["value"]
    assert conversations[-1]["reasoning"] == [
        {"type": "thinking", "thinking": "private provider-visible reasoning"}
    ]
