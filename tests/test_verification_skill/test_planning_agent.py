from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.agents.verification_planning import (
    FreshContextVerificationPlanner,
    VerificationPlanningAgentError,
)
from core.agents.verification_workflow import (
    AgentWorkflowError,
    FreshContextLightweightVerifier,
    FreshContextRepairAgent,
)
from core.builtin_tools import (
    BASH_TOOL,
    EDIT_TOOL,
    GLOB_TOOL,
    GREP_TOOL,
    LOAD_SKILL_TOOL,
    READ_TOOL,
    WRITE_TOOL,
)
from core.loop.orchestrator import QueryParams
from core.types import AgentState, AssistantMessage, StreamEvent, TextBlock, UserMessage
from core.verification.workflow import (
    ArtifactReference,
    AvailableVerificationSkill,
    CandidateFileChange,
    CandidateSnapshot,
    FailureSignature,
    FileChangeKind,
    IncidentBundle,
    LightweightVerificationRequest,
    LightweightVerdict,
    RepairCycleRequest,
    ReproductionSpec,
    SourceLocation,
    VerificationPlanProposal,
    VerificationPlanningRequest,
    canonical_json_digest,
)
from core.verification.models import (
    BehaviorGateSpec,
    BehaviorScenarioSpec,
    CommandSpec,
    ScenarioSpec,
    VerificationPolicy,
    VerificationSkillSpec,
)
from core.verification.generation_skill import (
    GenerationSkillChoice,
    GenerationSkillSelection,
    VerificationGenerationSkillCatalog,
)
from telemetry.tracer import NoopTracer


def _text_events(text: str):
    async def events():
        for event in (
            StreamEvent(type="message_start"),
            StreamEvent(
                type="content_block_start",
                index=0,
                block={"type": "text", "text": ""},
            ),
            StreamEvent(
                type="content_block_delta", index=0, delta={"text": text}
            ),
            StreamEvent(type="content_block_stop", index=0),
            StreamEvent(
                type="message_delta",
                delta={"stop_reason": "end_turn"},
                message={"usage": {"input_tokens": 7, "output_tokens": 5}},
            ),
            StreamEvent(type="message_stop"),
        ):
            yield event

    return events()


class _Provider:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(
            {
                **kwargs,
                "messages": [item.model_copy(deep=True) for item in kwargs["messages"]],
                "tools": list(kwargs["tools"]),
            }
        )
        return _text_events(self.response)

    def count_tokens(self, messages):
        return 0


class _SequenceProvider(_Provider):
    def __init__(self, responses: list[str]):
        super().__init__("")
        self.responses = iter(responses)

    def stream(self, **kwargs):
        self.calls.append(
            {
                **kwargs,
                "messages": [item.model_copy(deep=True) for item in kwargs["messages"]],
                "tools": list(kwargs["tools"]),
            }
        )
        return _text_events(next(self.responses))


def _generation_catalog(tmp_path: Path) -> VerificationGenerationSkillCatalog:
    root = tmp_path / "generation-skills"
    skill = root / "verification-api-contract"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: verification-api-contract\n"
        "description: Generate deterministic API contract tests.\n---\n"
        "# API SOP\nSELECTED_SECRET_SOP_BODY\n",
        encoding="utf-8",
    )
    (skill / "selection.yaml").write_text(
        "name: verification-api-contract\n"
        "scenarios:\n"
        "  - id: openapi-contract-change\n"
        "    when:\n"
        "      matched_rules: [checkout-timeout]\n"
        "      changed_paths: ['service.py']\n"
        "      risk_tags: [api]\n"
        "    selection_prompt: Select when an HTTP contract may have changed.\n"
        "    exclusions: [Do not select for browser-only behavior.]\n",
        encoding="utf-8",
    )
    (skill / "provenance.yaml").write_text(
        "repository: https://example.invalid/api.git\n"
        "commit: '0000000000000000000000000000000000000000'\n"
        "source_path: skill\n"
        "license: Apache-2.0\n",
        encoding="utf-8",
    )
    return VerificationGenerationSkillCatalog([root])


def _request(tmp_path: Path) -> VerificationPlanningRequest:
    control = tmp_path / "control"
    candidate = tmp_path / "candidate"
    control.mkdir()
    candidate.mkdir()
    (control / "service.py").write_text("status = 'timeout'\n", encoding="utf-8")
    (candidate / "service.py").write_text("status = 'ok'\n", encoding="utf-8")
    artifact = ArtifactReference(uri="file:///evidence/item.json", sha256="a" * 64)
    signature = FailureSignature(code="checkout.timeout", error_type="TimeoutError")
    incident = IncidentBundle(
        incident_id="incident-1",
        requirement="Recover the checkout request.",
        matched_rule="checkout-timeout",
        error_logs=(artifact,),
        original_trace=artifact,
        source_locations=(
            SourceLocation(path="service.py", start_line=1, revision="control-sha"),
        ),
        root_cause="The fallback path was skipped.",
        control_ref="control-sha",
        original_input={"prompt": "checkout"},
        failure_signature=signature,
    )
    policy = VerificationPolicy(
        behavior=BehaviorGateSpec(
            scenarios=(
                BehaviorScenarioSpec(
                    scenario_id="checkout:case",
                    expected_control_outcome="failure",
                    allowed_changed_paths=("$.status",),
                    required_changed_paths=("$.status",),
                    forbidden_changed_paths=("@model",),
                    reproducer=True,
                ),
            )
        )
    )
    skill_spec = VerificationSkillSpec(
        name="checkout",
        version="1",
        description="Checkout verification",
        integration=(
            ScenarioSpec(
                id="case",
                description="Exercise the checkout repair",
                steps=(CommandSpec(id="focused", argv=("pytest",)),),
            ),
        ),
    )
    return VerificationPlanningRequest(
        run_id="run-1",
        cycle=1,
        incident=incident,
        control_workspace=str(control),
        candidate=CandidateSnapshot(
            workspace=str(candidate),
            candidate_ref="candidate-sha",
            candidate_digest="b" * 64,
            changed_files=(
                CandidateFileChange(
                    path="service.py",
                    kind=FileChangeKind.MODIFIED,
                    before_sha256="c" * 64,
                    after_sha256="d" * 64,
                ),
            ),
            unified_diff="-status = 'timeout'\n+status = 'ok'\n",
            implementation_summary="Use the fallback path.",
            test_entrypoints=("pytest tests/test_checkout.py",),
        ),
        policy=policy,
        policy_digest=policy.digest,
        available_skills=(
            AvailableVerificationSkill(
                name="checkout",
                description="Checkout verification",
                digest="f" * 64,
                scenario_ids=("checkout:case",),
                spec=skill_spec,
            ),
        ),
    )


def _proposal(request: VerificationPlanningRequest) -> VerificationPlanProposal:
    return VerificationPlanProposal(
        skill_names=("checkout",),
        reproductions=(
            ReproductionSpec(
                scenario_id="checkout:case",
                skill_name="checkout",
                input_payload=request.incident.original_input,
                input_digest=canonical_json_digest(request.incident.original_input),
                reproducer=True,
                failure_signature=request.incident.failure_signature,
                expected_control_outcome="failure",
                expected_candidate_outcome="success",
                allowed_changed_paths=("$.status",),
                required_changed_paths=("$.status",),
                forbidden_changed_paths=("@model",),
                regression_assertions=("focused",),
                boundary_assertions=("focused",),
                side_effect_assertions=("focused",),
            ),
        ),
    )


def _params(provider: _Provider) -> QueryParams:
    return QueryParams(
        system="repair system",
        model="test-model",
        max_tokens=4096,
        provider=provider,
        abort_signal=asyncio.Event(),
        tools=[
            READ_TOOL,
            GLOB_TOOL,
            GREP_TOOL,
            BASH_TOOL,
            WRITE_TOOL,
            EDIT_TOOL,
            LOAD_SKILL_TOOL,
        ],
    )


def test_planning_request_rejects_policy_digest_mismatch(tmp_path: Path) -> None:
    request = _request(tmp_path)
    payload = request.model_dump(mode="python")
    payload["policy_digest"] = "0" * 64

    with pytest.raises(ValueError, match="policy_digest"):
        VerificationPlanningRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_planner_uses_fresh_context_read_only_tools_and_strict_json(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    proposal = _proposal(request)
    provider = _Provider(proposal.model_dump_json())
    parent = AgentState(
        cwd=str(tmp_path),
        messages=[
            UserMessage(content="repair conversation secret"),
            AssistantMessage(content=[TextBlock(text="trust my repair")]),
        ],
    )
    original_messages = list(parent.messages)
    planner = FreshContextVerificationPlanner(
        parent_agent_state=parent,
        parent_params=_params(provider),
        tracer=NoopTracer(),
    )

    actual = await planner.propose(request)

    assert actual == proposal
    call = provider.calls[0]
    assert [tool.name for tool in call["tools"]] == ["Read", "Glob", "Grep"]
    assert len(call["messages"]) == 1
    assert "repair conversation secret" not in str(call["messages"])
    assert "REQUEST_JSON" in str(call["messages"][0].content)
    assert '"focused"' in str(call["messages"][0].content)
    assert '"forbidden_changed_paths":["@model"]' in str(
        call["messages"][0].content
    )
    assert call["system"] != "repair system"
    assert parent.messages == original_messages
    assert parent.total_input_tokens == 7
    assert parent.total_output_tokens == 5


@pytest.mark.asyncio
async def test_planner_selects_from_metadata_before_loading_full_generation_skill(
    tmp_path: Path,
) -> None:
    catalog = _generation_catalog(tmp_path)
    request = _request(tmp_path).model_copy(
        update={"available_generation_skills": catalog.discover()}
    )
    selection = GenerationSkillSelection(
        choices=(
            GenerationSkillChoice(
                skill_name="verification-api-contract",
                scenario_ids=("openapi-contract-change",),
                reason="The changed service path may alter its HTTP contract.",
            ),
        )
    )
    resolved = catalog.load_selected(selection.skill_names)
    proposal = _proposal(request).model_copy(
        update={
            "generation_skill_names": selection.skill_names,
            "generation_skill_digests": {
                item.name: item.digest for item in resolved
            },
            "generation_skill_choices": selection.choices,
        }
    )
    provider = _SequenceProvider(
        [selection.model_dump_json(), proposal.model_dump_json()]
    )
    parent = AgentState(cwd=str(tmp_path))
    planner = FreshContextVerificationPlanner(
        parent_agent_state=parent,
        parent_params=_params(provider),
        tracer=NoopTracer(),
        generation_skill_catalog=catalog,
    )

    actual = await planner.propose(request)

    assert actual == proposal
    assert len(provider.calls) == 2
    selection_call, planning_call = provider.calls
    assert selection_call["tools"] == []
    assert "Select when an HTTP contract may have changed" in str(
        selection_call["messages"]
    )
    assert "SELECTED_SECRET_SOP_BODY" not in str(selection_call["messages"])
    assert [tool.name for tool in planning_call["tools"]] == [
        "Read",
        "Glob",
        "Grep",
    ]
    assert "SELECTED_SECRET_SOP_BODY" in str(planning_call["messages"])
    assert resolved[0].digest in str(planning_call["messages"])
    assert selection.choices[0].reason in str(planning_call["messages"])
    assert parent.total_input_tokens == 14
    assert parent.total_output_tokens == 10


@pytest.mark.asyncio
async def test_planner_fails_closed_for_unadvertised_generation_scenario(
    tmp_path: Path,
) -> None:
    catalog = _generation_catalog(tmp_path)
    request = _request(tmp_path).model_copy(
        update={"available_generation_skills": catalog.discover()}
    )
    selection = GenerationSkillSelection(
        choices=(
            GenerationSkillChoice(
                skill_name="verification-api-contract",
                scenario_ids=("unknown-scenario",),
                reason="Untrusted selection.",
            ),
        )
    )
    provider = _SequenceProvider([selection.model_dump_json()])
    planner = FreshContextVerificationPlanner(
        parent_agent_state=AgentState(cwd=str(tmp_path)),
        parent_params=_params(provider),
        tracer=NoopTracer(),
        generation_skill_catalog=catalog,
    )

    with pytest.raises(VerificationPlanningAgentError, match="unknown scenarios"):
        await planner.propose(request)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_planner_rejects_markdown_wrapped_or_duplicate_json(tmp_path: Path) -> None:
    request = _request(tmp_path)
    for output in (
        f"```json\n{_proposal(request).model_dump_json()}\n```",
        '{"skill_names":["checkout"],"skill_names":["checkout"],"reproductions":[]}',
    ):
        planner = FreshContextVerificationPlanner(
            parent_agent_state=AgentState(cwd=str(tmp_path)),
            parent_params=_params(_Provider(output)),
            tracer=NoopTracer(),
        )
        with pytest.raises(VerificationPlanningAgentError, match="strict JSON"):
            await planner.propose(request)


@pytest.mark.asyncio
async def test_planner_fails_closed_without_complete_read_tool_set(tmp_path: Path) -> None:
    request = _request(tmp_path)
    provider = _Provider(_proposal(request).model_dump_json())
    params = _params(provider)
    params.tools = [READ_TOOL]
    planner = FreshContextVerificationPlanner(
        parent_agent_state=AgentState(cwd=str(tmp_path)),
        parent_params=params,
        tracer=NoopTracer(),
    )

    with pytest.raises(VerificationPlanningAgentError, match="missing read-only tools"):
        await planner.propose(request)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_repair_adapter_is_fresh_and_derives_candidate_ref(tmp_path: Path) -> None:
    planning_request = _request(tmp_path)
    provider = _Provider(
        '{"implementation_summary":"use fallback",'
        '"test_entrypoints":["pytest tests/test_checkout.py"]}'
    )
    parent = AgentState(
        cwd=str(tmp_path), messages=[UserMessage(content="untrusted prior conclusion")]
    )
    repair = FreshContextRepairAgent(
        parent_agent_state=parent,
        parent_params=_params(provider),
        tracer=NoopTracer(),
        workspace_ignore=(),
    )

    result = await repair.repair(
        RepairCycleRequest(
            run_id="run-1",
            cycle=1,
            incident=planning_request.incident,
            candidate_workspace=planning_request.candidate.workspace,
        )
    )

    assert result.workspace == str(Path(planning_request.candidate.workspace).resolve())
    assert result.candidate_ref.startswith("candidate:run-1:1:")
    assert result.implementation_summary == "use fallback"
    call = provider.calls[0]
    assert "untrusted prior conclusion" not in str(call["messages"])
    assert "Agent" not in {tool.name for tool in call["tools"]}
    assert "LSP" not in {tool.name for tool in call["tools"]}


@pytest.mark.asyncio
async def test_lightweight_adapter_parses_single_final_verdict(tmp_path: Path) -> None:
    planning_request = _request(tmp_path)
    provider = _Provider("focused evidence\nVERDICT: PARTIAL")
    verifier = FreshContextLightweightVerifier(
        parent_agent_state=AgentState(cwd=str(tmp_path)),
        parent_params=_params(provider),
        tracer=NoopTracer(),
    )

    result = await verifier.verify(
        LightweightVerificationRequest(
            run_id="run-1",
            cycle=1,
            incident=planning_request.incident,
            candidate=planning_request.candidate,
        )
    )

    assert result.verdict is LightweightVerdict.PARTIAL
    assert [tool.name for tool in provider.calls[0]["tools"]] == [
        "Read",
        "Glob",
        "Grep",
        "Bash",
    ]


@pytest.mark.asyncio
async def test_lightweight_adapter_rejects_ambiguous_verdict(tmp_path: Path) -> None:
    planning_request = _request(tmp_path)
    provider = _Provider("VERDICT: FAIL\nVERDICT: PASS")
    verifier = FreshContextLightweightVerifier(
        parent_agent_state=AgentState(cwd=str(tmp_path)),
        parent_params=_params(provider),
        tracer=NoopTracer(),
    )

    with pytest.raises(AgentWorkflowError, match="exactly one VERDICT"):
        await verifier.verify(
            LightweightVerificationRequest(
                run_id="run-1",
                cycle=1,
                incident=planning_request.incident,
                candidate=planning_request.candidate,
            )
        )
