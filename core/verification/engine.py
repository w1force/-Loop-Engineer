"""可由后续 Coordinator 调用的 Verification Skill 硬门禁内核。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

from .gates import (
    aggregate_verdict,
    blocked,
    error,
    evaluate_behavior_gate,
    evaluate_command_gate,
    evaluate_log_gate,
    evaluate_trace_gate,
    validate_assertion_contract_definitions,
    validate_assertion_contracts,
)
from .models import (
    BehaviorEvidence,
    CommandEvidence,
    CommandSpec,
    FrozenVerificationSkill,
    GateKind,
    GateResult,
    GateStatus,
    LogEvidence,
    ResolvedVerificationSkill,
    ScenarioSpec,
    TraceEvidence,
    TraceObservation,
    Variant,
    VerificationPolicy,
    VerificationReport,
    VerificationRunRequest,
    command_contract_digest,
)
from .providers import (
    BehaviorEvidenceProvider,
    EvidenceCollectionContext,
    LogEvidenceProvider,
    TraceEvidenceProvider,
)
from .runner import CommandRunner, workspace_digest
from .skill import VerificationSkillError, VerificationSkillLoader


_EvidenceT = TypeVar("_EvidenceT")


def _consume_background_result(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


async def _collect_before_deadline(
    awaitable: Awaitable[_EvidenceT], timeout_ms: int
) -> _EvidenceT:
    """Enforce a wall-clock deadline even if a faulty provider swallows cancel."""

    task = asyncio.create_task(awaitable)
    done, _ = await asyncio.wait({task}, timeout=timeout_ms / 1000)
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_background_result)
        # Give a cooperative coroutine one turn to process cancellation without
        # waiting for a provider that intentionally suppresses it.
        await asyncio.sleep(0)
        raise TimeoutError(f"provider exceeded {timeout_ms}ms deadline")
    return task.result()


class VerificationEngine:
    """执行七类 gate；任何缺配置、缺证据或绑定不一致都不能 VERIFIED。"""

    def __init__(
        self,
        *,
        policy: VerificationPolicy,
        skill_loader: VerificationSkillLoader,
        trace_provider: TraceEvidenceProvider | None = None,
        log_provider: LogEvidenceProvider | None = None,
        behavior_provider: BehaviorEvidenceProvider | None = None,
        command_runner: CommandRunner | None = None,
    ):
        self.policy = VerificationPolicy.model_validate(
            policy.model_dump(mode="python")
        )
        self.skill_loader = skill_loader
        self.trace_provider = trace_provider
        self.log_provider = log_provider
        self.behavior_provider = behavior_provider
        if command_runner is not None and type(command_runner) is not CommandRunner:
            raise TypeError(
                "command_runner 是硬门禁信任边界，只允许内置 CommandRunner"
            )
        self.command_runner = command_runner or CommandRunner(
            workspace_ignore=self.policy.workspace_ignore,
            sandbox_mode=self.policy.sandbox_mode,
        )

    async def verify(self, request: VerificationRunRequest) -> VerificationReport:
        # Pydantic's frozen model does not recursively freeze dict fields.  Run
        # against a serialization round-trip so later mutation of the engine's
        # configuration cannot alter an in-flight gate contract.
        policy_snapshot = VerificationPolicy.model_validate_json(
            self.policy.model_dump_json()
        )
        snapshot = VerificationEngine(
            policy=policy_snapshot,
            skill_loader=self.skill_loader,
            trace_provider=self.trace_provider,
            log_provider=self.log_provider,
            behavior_provider=self.behavior_provider,
            command_runner=self.command_runner,
        )
        return await snapshot._verify(request)

    async def _verify(self, request: VerificationRunRequest) -> VerificationReport:
        request = VerificationRunRequest.model_validate(
            request.model_dump(mode="python")
        )
        workspace = Path(request.workspace).resolve()
        if request.replay_digest is None or request.replay_manifest is None:
            reason = "缺少与 VerificationPlan 绑定的 replay receipt digest/manifest"
            results = tuple(blocked(kind, reason) for kind in GateKind)
            return VerificationReport(
                run_id=request.run_id,
                cycle=request.cycle,
                incident_id=request.incident_id,
                incident_digest=request.incident_digest,
                plan_digest=request.plan_digest,
                replay_digest=None,
                replay_manifest=request.replay_manifest,
                scenario_input_digests=request.scenario_input_digests,
                assertion_contracts=request.assertion_contracts,
                control_ref=request.control_ref,
                control_digest=request.control_digest,
                candidate_ref=request.candidate_ref,
                skill_names=request.skill_names,
                policy=self.policy,
                policy_digest=self.policy.digest,
                gate_results=results,
                verdict=aggregate_verdict(results),
            )
        if request.expected_policy_digest != self.policy.digest:
            reason = "VerificationPolicy 与修复前冻结摘要不一致"
            results = tuple(blocked(kind, reason) for kind in GateKind)
            return VerificationReport(
                run_id=request.run_id,
                cycle=request.cycle,
                incident_id=request.incident_id,
                incident_digest=request.incident_digest,
                plan_digest=request.plan_digest,
                replay_digest=request.replay_digest,
                replay_manifest=request.replay_manifest,
                scenario_input_digests=request.scenario_input_digests,
                assertion_contracts=request.assertion_contracts,
                control_ref=request.control_ref,
                control_digest=request.control_digest,
                candidate_ref=request.candidate_ref,
                skill_names=request.skill_names,
                policy=self.policy,
                policy_digest=self.policy.digest,
                gate_results=results,
                verdict=aggregate_verdict(results),
            )
        try:
            initial_digest = await asyncio.to_thread(
                workspace_digest, workspace, self.policy.workspace_ignore
            )
        except Exception as exc:
            results = tuple(
                error(kind, f"无法冻结 candidate: {exc}") for kind in GateKind
            )
            return VerificationReport(
                run_id=request.run_id,
                cycle=request.cycle,
                incident_id=request.incident_id,
                incident_digest=request.incident_digest,
                plan_digest=request.plan_digest,
                replay_digest=request.replay_digest,
                replay_manifest=request.replay_manifest,
                scenario_input_digests=request.scenario_input_digests,
                assertion_contracts=request.assertion_contracts,
                control_ref=request.control_ref,
                control_digest=request.control_digest,
                candidate_ref=request.candidate_ref,
                skill_names=request.skill_names,
                policy=self.policy,
                policy_digest=self.policy.digest,
                gate_results=results,
                verdict=aggregate_verdict(results),
            )
        if initial_digest != request.expected_candidate_digest:
            reason = "candidate 内容与验证开始前冻结摘要不一致"
            results = tuple(blocked(kind, reason) for kind in GateKind)
            return VerificationReport(
                run_id=request.run_id,
                cycle=request.cycle,
                incident_id=request.incident_id,
                incident_digest=request.incident_digest,
                plan_digest=request.plan_digest,
                replay_digest=request.replay_digest,
                replay_manifest=request.replay_manifest,
                scenario_input_digests=request.scenario_input_digests,
                assertion_contracts=request.assertion_contracts,
                control_ref=request.control_ref,
                control_digest=request.control_digest,
                candidate_ref=request.candidate_ref,
                candidate_digest=initial_digest,
                candidate_digest_after=initial_digest,
                skill_names=request.skill_names,
                policy=self.policy,
                policy_digest=self.policy.digest,
                gate_results=results,
                verdict=aggregate_verdict(results),
            )

        skills: tuple[ResolvedVerificationSkill, ...] = ()
        skill_error: str | None = None
        try:
            skills = self.skill_loader.load_many(request.skill_names)
        except VerificationSkillError as exc:
            skill_error = str(exc)
        actual_skill_digests = {item.name: item.digest for item in skills}
        skill_contracts = tuple(
            FrozenVerificationSkill(name=item.name, spec=item.spec, digest=item.digest)
            for item in skills
        )
        if skill_error is None and actual_skill_digests != request.expected_skill_digests:
            mismatches = sorted(
                name
                for name in request.skill_names
                if actual_skill_digests.get(name)
                != request.expected_skill_digests.get(name)
            )
            skill_error = (
                "Verification Skill 与修复前冻结摘要不一致: "
                + ", ".join(mismatches)
            )

        contract_failures = (
            validate_assertion_contract_definitions(
                request.assertion_contracts,
                skills=skills,
                policy=self.policy,
            )
            if skill_error is None
            else ()
        )
        if contract_failures:
            reason = "冻结 assertion contract 无效: " + "; ".join(
                contract_failures
            )
            blocked_results = tuple(blocked(kind, reason) for kind in GateKind)
            return VerificationReport(
                run_id=request.run_id,
                cycle=request.cycle,
                incident_id=request.incident_id,
                incident_digest=request.incident_digest,
                plan_digest=request.plan_digest,
                replay_digest=request.replay_digest,
                replay_manifest=request.replay_manifest,
                scenario_input_digests=request.scenario_input_digests,
                assertion_contracts=request.assertion_contracts,
                control_ref=request.control_ref,
                control_digest=request.control_digest,
                candidate_ref=request.candidate_ref,
                candidate_digest=initial_digest,
                candidate_digest_after=initial_digest,
                skill_names=request.skill_names,
                skill_digests=actual_skill_digests,
                skill_contracts=skill_contracts,
                policy=self.policy,
                policy_digest=self.policy.digest,
                gate_results=blocked_results,
                verdict=aggregate_verdict(blocked_results),
            )

        evidence: list[CommandEvidence] = []
        results: dict[GateKind, GateResult] = {}

        results[GateKind.LINT] = await self._run_global_commands(
            request=request,
            workspace=workspace,
            candidate_digest=initial_digest,
            gate=GateKind.LINT,
            checks=(self.policy.lint.checks if self.policy.lint else ()),
            evidence=evidence,
            missing_reason="lint 配置缺失",
            additional_forbidden_patterns=(
                self.policy.lint.warning_patterns if self.policy.lint else ()
            ),
        )
        results[GateKind.UNIT] = await self._run_global_commands(
            request=request,
            workspace=workspace,
            candidate_digest=initial_digest,
            gate=GateKind.UNIT,
            checks=(self.policy.unit.checks if self.policy.unit else ()),
            evidence=evidence,
            missing_reason="全量单测配置缺失",
        )

        integration_scenarios = self._skill_scenarios(skills, ui=False)
        if skill_error:
            results[GateKind.INTEGRATION] = blocked(
                GateKind.INTEGRATION, f"Verification Skill 解析失败: {skill_error}"
            )
        else:
            results[GateKind.INTEGRATION] = await self._run_scenarios(
                request=request,
                workspace=workspace,
                candidate_digest=initial_digest,
                gate=GateKind.INTEGRATION,
                scenarios=integration_scenarios,
                evidence=evidence,
                missing_reason="选中的 Verification Skill 没有聚焦集成场景",
            )

        ui_scenarios: tuple[tuple[str | None, str, ScenarioSpec], ...] = ()
        if self.policy.ui is None:
            results[GateKind.UI] = blocked(GateKind.UI, "UI 适用性与验证配置缺失")
        elif self.policy.ui.mode == "not_applicable" and any(
            skill.spec.ui for skill in skills
        ):
            declared = sorted(skill.name for skill in skills if skill.spec.ui)
            results[GateKind.UI] = blocked(
                GateKind.UI,
                "UI 被声明不适用，但选中 Skill 包含 UI 场景: "
                + ", ".join(declared),
            )
        elif self.policy.ui.mode == "not_applicable":
            results[GateKind.UI] = GateResult(
                gate=GateKind.UI,
                status=GateStatus.NOT_APPLICABLE,
                summary=(
                    "配置明确声明当前服务无 UI: "
                    + (self.policy.ui.not_applicable_reason or "")
                ),
            )
        elif skill_error:
            results[GateKind.UI] = blocked(
                GateKind.UI, f"Verification Skill 解析失败: {skill_error}"
            )
        else:
            ui_scenarios = (
                *self._skill_scenarios(skills, ui=True),
                *tuple(
                    (None, f"global:{scenario.id}", scenario)
                    for scenario in self.policy.ui.global_scenarios
                ),
            )
            scenario_ids = [scenario_id for _, scenario_id, _ in ui_scenarios]
            if len(scenario_ids) != len(set(scenario_ids)):
                results[GateKind.UI] = blocked(
                    GateKind.UI,
                    "UI scenario id 在 Skill 与全局配置之间发生冲突",
                )
                ui_scenarios = ()
            else:
                results[GateKind.UI] = await self._run_scenarios(
                    request=request,
                    workspace=workspace,
                    candidate_digest=initial_digest,
                    gate=GateKind.UI,
                    scenarios=ui_scenarios,
                    evidence=evidence,
                    missing_reason="UI 为 required，但没有配置 UI 场景",
                )

        digest_error: str | None = None
        try:
            after_commands = await asyncio.to_thread(
                workspace_digest, workspace, self.policy.workspace_ignore
            )
        except Exception as exc:
            after_commands = initial_digest
            digest_error = str(exc)
        candidate_changed = after_commands != initial_digest or digest_error is not None
        integration_ids = {scenario_id for _, scenario_id, _ in integration_scenarios}
        behavior_ids = {
            item.scenario_id for item in self.policy.behavior.scenarios
        } if self.policy.behavior else set()
        trace_log_scenarios = tuple(
            sorted(
                integration_ids
                | behavior_ids
                | {scenario_id for _, scenario_id, _ in ui_scenarios}
            )
        )

        external_block_reason: str | None = None
        if candidate_changed:
            external_block_reason = (
                "命令执行后 candidate digest 变化，现有证据全部失效"
                if digest_error is None
                else f"无法复核 candidate digest: {digest_error}"
            )
        elif skill_error:
            external_block_reason = (
                f"Verification Skill 解析失败，外部证据契约无法冻结: {skill_error}"
            )
        elif set(request.scenario_input_digests) != set(trace_log_scenarios):
            external_block_reason = (
                "VerificationRunRequest 场景输入摘要与冻结验证场景不一致"
            )

        if external_block_reason is not None:
            results[GateKind.TRACE] = blocked(GateKind.TRACE, external_block_reason)
            results[GateKind.STAGING_LOG] = blocked(
                GateKind.STAGING_LOG, external_block_reason
            )
            results[GateKind.BEHAVIOR_COMPARE] = blocked(
                GateKind.BEHAVIOR_COMPARE, external_block_reason
            )
            trace_evidence = None
            log_evidence = None
            behavior_evidence = None
        else:
            trace_log_context = EvidenceCollectionContext(
                run_id=request.run_id,
                cycle=request.cycle,
                control_ref=request.control_ref,
                control_digest=request.control_digest,
                candidate_ref=request.candidate_ref,
                candidate_digest=initial_digest,
                policy_digest=self.policy.digest,
                skill_names=request.skill_names,
                skill_digests=actual_skill_digests,
                scenario_ids=trace_log_scenarios or ("verification-run",),
                replay_manifest=request.replay_manifest,
            )
            results[GateKind.TRACE], trace_evidence = await self._trace_result(
                request, trace_log_context, set(trace_log_scenarios)
            )
            candidate_traces = (
                {item.trace_id: item for item in trace_evidence.observations}
                if trace_evidence is not None
                else {}
            )
            results[GateKind.STAGING_LOG], log_evidence = await self._log_result(
                request,
                trace_log_context,
                set(trace_log_scenarios),
                request.scenario_input_digests,
            )
            behavior_context = EvidenceCollectionContext(
                run_id=request.run_id,
                cycle=request.cycle,
                control_ref=request.control_ref,
                control_digest=request.control_digest,
                candidate_ref=request.candidate_ref,
                candidate_digest=initial_digest,
                policy_digest=self.policy.digest,
                skill_names=request.skill_names,
                skill_digests=actual_skill_digests,
                scenario_ids=tuple(sorted(behavior_ids)) or ("verification-run",),
                replay_manifest=request.replay_manifest,
            )
            results[GateKind.BEHAVIOR_COMPARE], behavior_evidence = await self._behavior_result(
                request,
                behavior_context,
                candidate_traces,
                request.scenario_input_digests,
            )

        try:
            final_digest = await asyncio.to_thread(
                workspace_digest, workspace, self.policy.workspace_ignore
            )
        except Exception:
            final_digest = None
        if final_digest != initial_digest:
            reason = "candidate 在证据生成期间发生变化，禁止复用旧证据"
            for kind, result in tuple(results.items()):
                if result.status in {GateStatus.PASS, GateStatus.NOT_APPLICABLE}:
                    results[kind] = blocked(kind, reason)

        ordered = tuple(results[kind] for kind in GateKind)
        return VerificationReport(
            run_id=request.run_id,
            cycle=request.cycle,
            incident_id=request.incident_id,
            incident_digest=request.incident_digest,
            plan_digest=request.plan_digest,
            replay_digest=request.replay_digest,
            replay_manifest=request.replay_manifest,
            scenario_input_digests=request.scenario_input_digests,
            assertion_contracts=request.assertion_contracts,
            control_ref=request.control_ref,
            control_digest=request.control_digest,
            candidate_ref=request.candidate_ref,
            candidate_digest=initial_digest,
            candidate_digest_after=final_digest,
            skill_names=request.skill_names,
            skill_digests=actual_skill_digests,
            skill_contracts=skill_contracts,
            policy=self.policy,
            policy_digest=self.policy.digest,
            gate_results=ordered,
            command_evidence=tuple(evidence),
            trace_evidence=trace_evidence,
            log_evidence=log_evidence,
            behavior_evidence=behavior_evidence,
            verdict=aggregate_verdict(ordered),
        )

    async def _run_global_commands(
        self,
        *,
        request: VerificationRunRequest,
        workspace: Path,
        candidate_digest: str,
        gate: GateKind,
        checks: tuple[CommandSpec, ...],
        evidence: list[CommandEvidence],
        missing_reason: str,
        additional_forbidden_patterns: tuple[str, ...] = (),
    ) -> GateResult:
        if not checks:
            return blocked(gate, missing_reason)
        items: list[CommandEvidence] = []
        for check in checks:
            item = await self.command_runner.run(
                check,
                run_id=request.run_id,
                cycle=request.cycle,
                gate=gate,
                workspace=workspace,
                candidate_ref=request.candidate_ref,
                policy_digest=self.policy.digest,
                additional_forbidden_patterns=additional_forbidden_patterns,
            )
            items.append(item)
            evidence.append(item)
        return evaluate_command_gate(
            gate,
            items,
            expected_contracts={
                (None, None, check.id): command_contract_digest(
                    check, additional_forbidden_patterns
                )
                for check in checks
            },
            run_id=request.run_id,
            cycle=request.cycle,
            policy_digest=self.policy.digest,
            expected_skill_digests=request.expected_skill_digests,
            candidate_ref=request.candidate_ref,
            candidate_digest=candidate_digest,
        )

    @staticmethod
    def _skill_scenarios(
        skills: tuple[ResolvedVerificationSkill, ...], *, ui: bool
    ) -> tuple[tuple[str | None, str, ScenarioSpec], ...]:
        out: list[tuple[str | None, str, ScenarioSpec]] = []
        for skill in skills:
            scenarios = skill.spec.ui if ui else skill.spec.integration
            for scenario in scenarios:
                out.append(
                    (skill.name, f"{skill.name}:{scenario.id}", scenario)
                )
        return tuple(out)

    async def _run_scenarios(
        self,
        *,
        request: VerificationRunRequest,
        workspace: Path,
        candidate_digest: str,
        gate: GateKind,
        scenarios: tuple[tuple[str | None, str, ScenarioSpec], ...],
        evidence: list[CommandEvidence],
        missing_reason: str,
    ) -> GateResult:
        if not scenarios:
            return blocked(gate, missing_reason)
        items: list[CommandEvidence] = []
        expected: dict[tuple[str | None, str | None, str], str] = {}
        for skill_name, scenario_id, scenario in scenarios:
            for step in scenario.steps:
                expected[(skill_name, scenario_id, step.id)] = (
                    command_contract_digest(step)
                )
                item = await self.command_runner.run(
                    step,
                    run_id=request.run_id,
                    cycle=request.cycle,
                    gate=gate,
                    workspace=workspace,
                    candidate_ref=request.candidate_ref,
                    policy_digest=self.policy.digest,
                    skill_digest=(
                        request.expected_skill_digests.get(skill_name)
                        if skill_name is not None
                        else None
                    ),
                    scenario_id=scenario_id,
                    skill_name=skill_name,
                    variant=Variant.CANDIDATE,
                )
                items.append(item)
                evidence.append(item)
        result = evaluate_command_gate(
            gate,
            items,
            expected_contracts=expected,
            run_id=request.run_id,
            cycle=request.cycle,
            policy_digest=self.policy.digest,
            expected_skill_digests=request.expected_skill_digests,
            candidate_ref=request.candidate_ref,
            candidate_digest=candidate_digest,
        )
        scenario_keys = {
            (skill_name, scenario_id)
            for skill_name, scenario_id, _ in scenarios
        }
        relevant_contracts = tuple(
            contract
            for contract in request.assertion_contracts
            if (contract.skill_name, contract.scenario_id) in scenario_keys
        )
        assertion_failures = validate_assertion_contracts(
            relevant_contracts,
            items,
        )
        if assertion_failures and result.status is GateStatus.PASS:
            return GateResult(
                gate=gate,
                status=GateStatus.BLOCKED,
                summary=f"{gate.value} 场景断言证据不完整",
                evidence_ids=result.evidence_ids,
                failures=assertion_failures,
            )
        return result

    async def _trace_result(
        self,
        request: VerificationRunRequest,
        context: EvidenceCollectionContext,
        scenario_ids: set[str],
    ) -> tuple[GateResult, TraceEvidence | None]:
        if self.policy.trace is None:
            return blocked(GateKind.TRACE, "Trace 门禁配置缺失"), None
        if self.trace_provider is None:
            return blocked(GateKind.TRACE, "Trace provider 未配置"), None
        try:
            collected = await _collect_before_deadline(
                self.trace_provider.collect_trace(context),
                self.policy.evidence_timeout_ms,
            )
            evidence = TraceEvidence.model_validate(
                collected.model_dump(mode="python")
            )
        except Exception as exc:  # provider 故障不允许降级放行
            return (
                error(GateKind.TRACE, f"Trace provider 异常: {type(exc).__name__}: {exc}"),
                None,
            )
        return (
            evaluate_trace_gate(
                self.policy.trace,
                evidence,
                run_id=request.run_id,
                cycle=request.cycle,
                candidate_ref=request.candidate_ref,
                candidate_digest=context.candidate_digest,
                policy_digest=self.policy.digest,
                expected_skill_digests=context.skill_digests,
                scenario_ids=scenario_ids,
                scenario_input_digests=request.scenario_input_digests,
            ),
            evidence,
        )

    async def _log_result(
        self,
        request: VerificationRunRequest,
        context: EvidenceCollectionContext,
        scenario_ids: set[str],
        scenario_input_digests: dict[str, str],
    ) -> tuple[GateResult, LogEvidence | None]:
        if self.policy.staging_log is None:
            return blocked(GateKind.STAGING_LOG, "预发日志门禁配置缺失"), None
        if self.log_provider is None:
            return blocked(GateKind.STAGING_LOG, "日志 provider 未配置"), None
        try:
            collected = await _collect_before_deadline(
                self.log_provider.collect_logs(context),
                self.policy.evidence_timeout_ms,
            )
            evidence = LogEvidence.model_validate(
                collected.model_dump(mode="python")
            )
        except Exception as exc:
            return (
                error(
                    GateKind.STAGING_LOG,
                    f"日志 provider 异常: {type(exc).__name__}: {exc}",
                ),
                None,
            )
        return (
            evaluate_log_gate(
                self.policy.staging_log,
                evidence,
                run_id=request.run_id,
                cycle=request.cycle,
                control_ref=request.control_ref,
                control_digest=request.control_digest,
                candidate_ref=request.candidate_ref,
                candidate_digest=context.candidate_digest,
                policy_digest=self.policy.digest,
                expected_skill_digests=context.skill_digests,
                scenario_ids=scenario_ids,
                scenario_input_digests=scenario_input_digests,
            ),
            evidence,
        )

    async def _behavior_result(
        self,
        request: VerificationRunRequest,
        context: EvidenceCollectionContext,
        candidate_traces: dict[str, TraceObservation],
        scenario_input_digests: dict[str, str],
    ) -> tuple[GateResult, BehaviorEvidence | None]:
        if self.policy.behavior is None:
            return (
                blocked(GateKind.BEHAVIOR_COMPARE, "行为对比门禁配置缺失"),
                None,
            )
        if self.behavior_provider is None:
            return (
                blocked(GateKind.BEHAVIOR_COMPARE, "行为 provider 未配置"),
                None,
            )
        try:
            collected = await _collect_before_deadline(
                self.behavior_provider.collect_behavior(context),
                self.policy.evidence_timeout_ms,
            )
            evidence = BehaviorEvidence.model_validate(
                collected.model_dump(mode="python")
            )
        except Exception as exc:
            return (
                error(
                    GateKind.BEHAVIOR_COMPARE,
                    f"行为 provider 异常: {type(exc).__name__}: {exc}",
                ),
                None,
            )
        return (
            evaluate_behavior_gate(
                self.policy.behavior,
                evidence,
                run_id=request.run_id,
                cycle=request.cycle,
                control_ref=request.control_ref,
                control_digest=request.control_digest,
                candidate_ref=request.candidate_ref,
                candidate_digest=context.candidate_digest,
                policy_digest=self.policy.digest,
                expected_skill_digests=context.skill_digests,
                candidate_traces=candidate_traces,
                scenario_input_digests=scenario_input_digests,
                assertion_contracts=request.assertion_contracts,
            ),
            evidence,
        )


__all__ = ["VerificationEngine"]
