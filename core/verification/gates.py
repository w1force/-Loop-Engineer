"""文章 Verification 条件的确定性判定器。"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .models import (
    BehaviorEvidence,
    BehaviorGateSpec,
    CommandEvidence,
    FrozenVerificationSkill,
    GateKind,
    GateResult,
    GateStatus,
    LogEvidence,
    LogGateSpec,
    ResolvedVerificationSkill,
    ScenarioAssertionContract,
    TraceEvidence,
    TraceGateSpec,
    TraceObservation,
    Variant,
    VerificationPolicy,
    VerificationVerdict,
)


REQUIRED_GATES = frozenset(GateKind)


def blocked(gate: GateKind, reason: str) -> GateResult:
    return GateResult(
        gate=gate,
        status=GateStatus.BLOCKED,
        summary=reason,
        failures=(reason,),
    )


def error(gate: GateKind, reason: str) -> GateResult:
    return GateResult(
        gate=gate,
        status=GateStatus.ERROR,
        summary=reason,
        failures=(reason,),
    )


def evaluate_command_gate(
    gate: GateKind,
    evidence: Iterable[CommandEvidence],
    *,
    expected_contracts: dict[tuple[str | None, str | None, str], str],
    run_id: str,
    cycle: int,
    policy_digest: str,
    expected_skill_digests: dict[str, str],
    candidate_ref: str,
    candidate_digest: str,
) -> GateResult:
    items = tuple(evidence)
    if not expected_contracts:
        return blocked(gate, f"{gate.value} 没有配置必需检查")
    keys = [(item.skill_name, item.scenario_id, item.check_id) for item in items]
    if len(keys) != len(set(keys)):
        return blocked(gate, f"{gate.value} 出现重复命令证据")
    observed = set(keys)
    expected_keys = set(expected_contracts)
    missing = expected_keys - observed
    unexpected = observed - expected_keys
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"缺少证据: {sorted(missing, key=str)}")
        if unexpected:
            details.append(f"出现未配置证据: {sorted(unexpected, key=str)}")
        return blocked(gate, "; ".join(details))

    binding_failures = [
        item.check_id
        for item in items
        if item.run_id != run_id
        or item.cycle != cycle
        or item.gate is not gate
        or item.variant is not Variant.CANDIDATE
        or item.policy_digest != policy_digest
        or item.candidate_ref != candidate_ref
        or item.candidate_digest_before != candidate_digest
        or item.candidate_digest_after != candidate_digest
        or item.command_spec_digest
        != expected_contracts.get((item.skill_name, item.scenario_id, item.check_id))
        or (
            item.skill_name is None
            and item.skill_digest is not None
        )
        or (
            item.skill_name is not None
            and item.skill_digest != expected_skill_digests.get(item.skill_name)
        )
    ]
    if binding_failures:
        return blocked(
            gate,
            "证据未绑定当前 candidate 或验证期间 candidate 已变化: "
            + ", ".join(binding_failures),
        )
    errors = [item for item in items if item.error is not None or item.timed_out]
    if errors:
        failures = tuple(
            f"{item.check_id}: {', '.join(item.failures) or item.error or 'timeout'}"
            for item in errors
        )
        return GateResult(
            gate=gate,
            status=GateStatus.ERROR,
            summary=f"{gate.value} 执行器错误",
            evidence_ids=tuple(item.evidence_id for item in items),
            failures=failures,
        )
    failed = [item for item in items if not item.passed]
    if failed:
        failures = tuple(
            f"{item.check_id}: {', '.join(item.failures)}" for item in failed
        )
        return GateResult(
            gate=gate,
            status=GateStatus.FAIL,
            summary=f"{gate.value} 检查未通过",
            evidence_ids=tuple(item.evidence_id for item in items),
            failures=failures,
        )
    return GateResult(
        gate=gate,
        status=GateStatus.PASS,
        summary=f"{gate.value} 全部检查通过",
        evidence_ids=tuple(item.evidence_id for item in items),
    )


def validate_assertion_contracts(
    contracts: Iterable[ScenarioAssertionContract],
    evidence: Iterable[CommandEvidence],
) -> tuple[str, ...]:
    """Require every frozen scenario assertion to resolve to one passing command."""

    command_items = tuple(
        item
        for item in evidence
        if item.gate in {GateKind.INTEGRATION, GateKind.UI}
    )
    by_key: dict[tuple[str | None, str | None, str], list[CommandEvidence]] = {}
    for item in command_items:
        key = (item.skill_name, item.scenario_id, item.check_id)
        by_key.setdefault(key, []).append(item)

    failures: list[str] = []
    assertion_fields = (
        ("回归", "regression_assertions"),
        ("边界", "boundary_assertions"),
        ("副作用", "side_effect_assertions"),
    )
    for contract in contracts:
        for label, field_name in assertion_fields:
            for check_id in getattr(contract, field_name):
                key = (contract.skill_name, contract.scenario_id, check_id)
                matches = by_key.get(key, [])
                if len(matches) != 1:
                    failures.append(
                        f"{contract.scenario_id}: {label}断言 {check_id} "
                        f"需要且只能绑定一份命令证据，实际 {len(matches)} 份"
                    )
                    continue
                item = matches[0]
                if item.variant is not Variant.CANDIDATE or not item.passed:
                    failures.append(
                        f"{contract.scenario_id}: {label}断言 {check_id} 未通过"
                    )
    return tuple(failures)


def validate_assertion_contract_definitions(
    contracts: tuple[ScenarioAssertionContract, ...],
    *,
    skills: Iterable[FrozenVerificationSkill | ResolvedVerificationSkill],
    policy: VerificationPolicy,
) -> tuple[str, ...]:
    """Validate that frozen assertions are complete and match trusted contracts."""

    command_scenarios = {
        (skill.name, f"{skill.name}:{scenario.id}"): scenario
        for skill in skills
        for scenario in (*skill.spec.integration, *skill.spec.ui)
    }
    if policy.ui is not None and policy.ui.mode == "required":
        command_scenarios.update(
            {
                (None, f"global:{scenario.id}"): scenario
                for scenario in policy.ui.global_scenarios
            }
        )
    command_scenario_ids = [scenario_id for _, scenario_id in command_scenarios]
    contract_by_key = {
        (contract.skill_name, contract.scenario_id): contract
        for contract in contracts
    }
    failures: list[str] = []
    if len(command_scenario_ids) != len(set(command_scenario_ids)):
        failures.append("Skill 与全局 UI scenario id 冲突")
    assertion_fields = (
        "regression_assertions",
        "boundary_assertions",
        "side_effect_assertions",
    )

    for key, scenario in command_scenarios.items():
        contract = contract_by_key.get(key)
        if contract is None:
            failures.append(f"{key[1]}: 缺少受信命令场景 assertion contract")
            continue
        if not any(getattr(contract, field) for field in assertion_fields):
            failures.append(f"{contract.scenario_id}: 命令场景至少需要一个断言 step")
        step_ids = {step.id for step in scenario.steps}
        for field in assertion_fields:
            unknown = sorted(set(getattr(contract, field)) - step_ids)
            if unknown:
                failures.append(
                    f"{contract.scenario_id}: {field} 引用未知或跨场景 step "
                    + ", ".join(unknown)
                )
        expected_assertions = scenario.trusted_assertion_groups
        actual_assertions = {
            field: getattr(contract, field) for field in assertion_fields
        }
        if actual_assertions != expected_assertions:
            failures.append(
                f"{contract.scenario_id}: assertion 分类与受信 Skill step 不一致"
            )

    for contract in contracts:
        has_assertions = any(
            getattr(contract, field) for field in assertion_fields
        )
        if has_assertions and (
            contract.skill_name, contract.scenario_id
        ) not in command_scenarios:
            failures.append(f"{contract.scenario_id}: 断言无法绑定受信命令场景")

    missing_groups = [
        field
        for field in assertion_fields
        if not any(getattr(contract, field) for contract in contracts)
    ]
    if missing_groups:
        failures.append("验证方案缺少断言类别: " + ", ".join(missing_groups))

    behavior_by_id = {
        scenario.scenario_id: scenario
        for scenario in (policy.behavior.scenarios if policy.behavior else ())
    }
    contract_by_scenario = {contract.scenario_id: contract for contract in contracts}
    for scenario_id, expected in behavior_by_id.items():
        contract = contract_by_scenario.get(scenario_id)
        if contract is None:
            failures.append(f"{scenario_id}: 缺少 behavior assertion contract")
        elif contract.forbidden_changed_paths != expected.forbidden_changed_paths:
            failures.append(
                f"{scenario_id}: forbidden_changed_paths 与受信 policy 不一致"
            )
    invalid_forbidden = sorted(
        contract.scenario_id
        for contract in contracts
        if contract.forbidden_changed_paths
        and contract.scenario_id not in behavior_by_id
    )
    if invalid_forbidden:
        failures.append(
            "forbidden_changed_paths 只能绑定 behavior 场景: "
            + ", ".join(invalid_forbidden)
        )
    return tuple(failures)


def _binding_failure(
    *,
    run_id: str,
    cycle: int,
    candidate_ref: str,
    candidate_digest: str,
    policy_digest: str,
    evidence_run_id: str,
    evidence_cycle: int,
    evidence_ref: str,
    evidence_digest: str,
    evidence_policy_digest: str,
    expected_skill_digests: dict[str, str],
    evidence_skill_digests: dict[str, str],
    control_ref: str | None = None,
    control_digest: str | None = None,
    evidence_control_ref: str | None = None,
    evidence_control_digest: str | None = None,
) -> str | None:
    if evidence_run_id != run_id:
        return "证据 run_id 与当前运行不一致"
    if evidence_cycle != cycle:
        return "证据 cycle 与当前验证轮次不一致"
    if evidence_ref != candidate_ref:
        return "证据 candidate_ref 与当前候选版本不一致"
    if evidence_digest != candidate_digest:
        return "证据 candidate_digest 与当前候选内容不一致"
    if evidence_policy_digest != policy_digest:
        return "证据 policy_digest 与当前门禁策略不一致"
    if evidence_skill_digests != expected_skill_digests:
        return "证据 skill_digests 与当前冻结 Skill 不一致"
    if control_ref is not None and evidence_control_ref != control_ref:
        return "证据 control_ref 与冻结基线不一致"
    if control_digest is not None and evidence_control_digest != control_digest:
        return "证据 control_digest 与冻结基线不一致"
    return None


def evaluate_trace_gate(
    spec: TraceGateSpec,
    evidence: TraceEvidence,
    *,
    run_id: str,
    cycle: int,
    candidate_ref: str,
    candidate_digest: str,
    policy_digest: str,
    expected_skill_digests: dict[str, str],
    scenario_ids: set[str],
    scenario_input_digests: dict[str, str],
) -> GateResult:
    if not scenario_ids:
        return blocked(GateKind.TRACE, "Trace 门禁没有关联的验证场景")
    binding = _binding_failure(
        run_id=run_id,
        cycle=cycle,
        candidate_ref=candidate_ref,
        candidate_digest=candidate_digest,
        policy_digest=policy_digest,
        evidence_run_id=evidence.run_id,
        evidence_cycle=evidence.cycle,
        evidence_ref=evidence.candidate_ref,
        evidence_digest=evidence.candidate_digest,
        evidence_policy_digest=evidence.policy_digest,
        expected_skill_digests=expected_skill_digests,
        evidence_skill_digests=evidence.skill_digests,
    )
    if binding:
        return blocked(GateKind.TRACE, binding)
    if evidence.collector_error is not None:
        return error(GateKind.TRACE, f"Trace provider 错误: {evidence.collector_error}")
    if not evidence.collection_complete:
        return blocked(GateKind.TRACE, "Trace 采集窗口未完成")
    if not evidence.observations:
        return blocked(GateKind.TRACE, "没有采集到 candidate Trace")
    if set(scenario_input_digests) != scenario_ids:
        return blocked(GateKind.TRACE, "Trace 门禁缺少冻结场景输入摘要")
    trace_ids = [item.trace_id for item in evidence.observations]
    if len(trace_ids) != len(set(trace_ids)):
        return blocked(GateKind.TRACE, "Trace evidence 包含重复 trace_id")

    observed_scenarios = {item.scenario_id for item in evidence.observations}
    missing_scenarios = scenario_ids - observed_scenarios
    if missing_scenarios:
        return blocked(
            GateKind.TRACE,
            "缺少场景 Trace: " + ", ".join(sorted(missing_scenarios)),
        )
    extra_scenarios = observed_scenarios - scenario_ids
    if extra_scenarios:
        return blocked(
            GateKind.TRACE,
            "出现未配置场景 Trace: " + ", ".join(sorted(extra_scenarios)),
        )
    duplicate_scenarios = sorted(
        scenario_id
        for scenario_id in scenario_ids
        if sum(
            item.scenario_id == scenario_id for item in evidence.observations
        )
        != 1
    )
    if duplicate_scenarios:
        return blocked(
            GateKind.TRACE,
            "每个场景必须恰有一份 candidate Trace: "
            + ", ".join(duplicate_scenarios),
        )

    missing_fields: list[str] = []
    failures: list[str] = []
    evidence_ids: list[str] = []
    for item in evidence.observations:
        evidence_ids.append(item.trace_id)
        if item.input_digest != scenario_input_digests[item.scenario_id]:
            return blocked(
                GateKind.TRACE,
                f"Trace 场景 {item.scenario_id} 未绑定冻结 Plan 输入",
            )
        fields = {
            "error_observations": item.error_observations,
            "actual_model": item.actual_model,
            "fallback_used": item.fallback_used,
            "input_tokens": item.input_tokens,
            "finished": item.finished,
            "input_digest": item.input_digest,
        }
        absent = [name for name, value in fields.items() if value is None]
        if not item.request_id and not item.session_id:
            absent.append("request_id/session_id")
        if absent:
            missing_fields.append(f"{item.trace_id}: {', '.join(absent)}")
            continue
        if item.error_observations != 0:
            failures.append(
                f"{item.trace_id}: ERROR observations={item.error_observations}, expected 0"
            )
        if item.actual_model != spec.expected_model:
            failures.append(
                f"{item.trace_id}: actual_model={item.actual_model}, expected {spec.expected_model}"
            )
        if item.fallback_used and item.scenario_id not in spec.allowed_fallback_scenarios:
            failures.append(f"{item.trace_id}: unexpected fallback")
        if item.input_tokens is not None and item.input_tokens >= spec.max_input_tokens:
            failures.append(
                f"{item.trace_id}: input_tokens={item.input_tokens}, must be < {spec.max_input_tokens}"
            )
        if item.finished is not True:
            failures.append(f"{item.trace_id}: agent finished is not true")
    if missing_fields:
        return blocked(
            GateKind.TRACE,
            "Trace 缺少硬指标字段: " + "; ".join(missing_fields),
        )
    if failures:
        return GateResult(
            gate=GateKind.TRACE,
            status=GateStatus.FAIL,
            summary="Trace 硬指标未通过",
            evidence_ids=tuple(evidence_ids),
            failures=tuple(failures),
        )
    return GateResult(
        gate=GateKind.TRACE,
        status=GateStatus.PASS,
        summary="Trace 硬指标全部通过",
        evidence_ids=tuple(evidence_ids),
    )


def evaluate_log_gate(
    spec: LogGateSpec,
    evidence: LogEvidence,
    *,
    run_id: str,
    cycle: int,
    control_ref: str,
    control_digest: str,
    candidate_ref: str,
    candidate_digest: str,
    policy_digest: str,
    expected_skill_digests: dict[str, str],
    scenario_ids: set[str],
    scenario_input_digests: dict[str, str],
) -> GateResult:
    if not scenario_ids:
        return blocked(GateKind.STAGING_LOG, "日志门禁没有关联的验证场景")
    binding = _binding_failure(
        run_id=run_id,
        cycle=cycle,
        candidate_ref=candidate_ref,
        candidate_digest=candidate_digest,
        policy_digest=policy_digest,
        evidence_run_id=evidence.run_id,
        evidence_cycle=evidence.cycle,
        evidence_ref=evidence.candidate_ref,
        evidence_digest=evidence.candidate_digest,
        evidence_policy_digest=evidence.policy_digest,
        expected_skill_digests=expected_skill_digests,
        evidence_skill_digests=evidence.skill_digests,
        control_ref=control_ref,
        control_digest=control_digest,
        evidence_control_ref=evidence.control_ref,
        evidence_control_digest=evidence.control_digest,
    )
    if binding:
        return blocked(GateKind.STAGING_LOG, binding)
    if evidence.collector_error is not None:
        return error(
            GateKind.STAGING_LOG,
            f"日志 provider 错误: {evidence.collector_error}",
        )
    if not evidence.collection_complete:
        return blocked(GateKind.STAGING_LOG, "日志观察窗口未完成")
    if set(evidence.collected_variants) != {Variant.CONTROL, Variant.CANDIDATE}:
        return blocked(GateKind.STAGING_LOG, "日志证据必须完整覆盖 control 和 candidate")
    required_windows = {
        (scenario_id, variant)
        for scenario_id in scenario_ids
        for variant in (Variant.CONTROL, Variant.CANDIDATE)
    }
    collected_windows = {
        (item.scenario_id, item.variant) for item in evidence.collected_scenarios
    }
    if collected_windows != required_windows:
        missing = sorted(required_windows - collected_windows, key=str)
        extras = sorted(collected_windows - required_windows, key=str)
        details = []
        if missing:
            details.append(f"缺少日志采集窗口: {missing}")
        if extras:
            details.append(f"出现未配置日志采集窗口: {extras}")
        return blocked(GateKind.STAGING_LOG, "; ".join(details))
    if set(scenario_input_digests) != scenario_ids:
        return blocked(GateKind.STAGING_LOG, "日志门禁缺少冻结场景输入摘要")
    for scenario_id in scenario_ids:
        windows = [
            item
            for item in evidence.collected_scenarios
            if item.scenario_id == scenario_id
        ]
        if any(
            item.input_digest != scenario_input_digests[scenario_id]
            for item in windows
        ):
            return blocked(
                GateKind.STAGING_LOG,
                f"日志场景 {scenario_id} 未绑定同一冻结输入",
            )
    observation_scenarios = {item.scenario_id for item in evidence.observations}
    extras = observation_scenarios - scenario_ids
    if extras:
        return blocked(
            GateKind.STAGING_LOG,
            "日志包含未配置场景: " + ", ".join(sorted(extras)),
        )

    error_levels = set(spec.error_levels)

    def fingerprint(item):
        return (
            item.scenario_id,
            item.service,
            item.error_type,
            item.event_code or "",
            item.message_template or "",
            item.business_frame or "",
        )

    control = {
        fingerprint(item)
        for item in evidence.observations
        if item.variant is Variant.CONTROL and item.level.upper() in error_levels
    }
    candidate = {
        fingerprint(item)
        for item in evidence.observations
        if item.variant is Variant.CANDIDATE and item.level.upper() in error_levels
    }
    new_types = sorted(candidate - control)
    ids = tuple(
        item.observation_id
        for item in evidence.observations
    )
    if new_types:
        failure = "candidate 新增 ERROR 指纹: " + ", ".join(
            "/".join(item) for item in new_types
        )
        return GateResult(
            gate=GateKind.STAGING_LOG,
            status=GateStatus.FAIL,
            summary="预发日志出现新增 ERROR 类型",
            evidence_ids=ids,
            failures=(failure,),
        )
    return GateResult(
        gate=GateKind.STAGING_LOG,
        status=GateStatus.PASS,
        summary="candidate 未产生 control 中不存在的 ERROR 类型",
        evidence_ids=ids,
    )


def _dict_child_path(path: str, key: str) -> str:
    if key and (key[0].isalpha() or key[0] == "_") and all(
        char.isalnum() or char in "_-" for char in key[1:]
    ):
        return f"{path}.{key}"
    # Hex segments make keys containing '.', brackets, or glob metacharacters
    # unambiguous while keeping ordinary API fields readable as ``$.field``.
    return f"{path}.{{{key.encode('utf-8').hex()}}}"


def _leaf_paths(value: Any, path: str) -> set[str]:
    if isinstance(value, dict):
        if not value:
            return {path}
        return {
            leaf
            for key, child in value.items()
            for leaf in _leaf_paths(child, _dict_child_path(path, key))
        }
    if isinstance(value, list):
        if not value:
            return {path}
        return {
            leaf
            for index, child in enumerate(value)
            for leaf in _leaf_paths(child, f"{path}[{index}]")
        }
    return {path}


def _changed_paths(control: Any, candidate: Any, path: str = "$") -> set[str]:
    if isinstance(control, dict) and isinstance(candidate, dict):
        changed: set[str] = set()
        for key in control.keys() | candidate.keys():
            child_path = _dict_child_path(path, key)
            if key not in control:
                changed.update(_leaf_paths(candidate[key], child_path))
            elif key not in candidate:
                changed.update(_leaf_paths(control[key], child_path))
            else:
                changed.update(_changed_paths(control[key], candidate[key], child_path))
        return changed
    if isinstance(control, list) and isinstance(candidate, list):
        changed = set()
        shared = min(len(control), len(candidate))
        for index in range(shared):
            changed.update(
                _changed_paths(control[index], candidate[index], f"{path}[{index}]")
            )
        for index in range(shared, len(control)):
            changed.update(_leaf_paths(control[index], f"{path}[{index}]"))
        for index in range(shared, len(candidate)):
            changed.update(_leaf_paths(candidate[index], f"{path}[{index}]"))
        return changed
    # JSON behavior contracts retain value types; Python otherwise considers
    # ``True == 1`` and ``1 == 1.0`` and could hide a wire-format regression.
    return set() if type(control) is type(candidate) and control == candidate else {path}


def _path_matches(path: str, pattern: str) -> bool:
    """Match only ``*``/``?`` globs; JSON array brackets stay literal."""

    expression = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(expression, path) is not None


def evaluate_behavior_gate(
    spec: BehaviorGateSpec,
    evidence: BehaviorEvidence,
    *,
    run_id: str,
    cycle: int,
    control_ref: str,
    control_digest: str,
    candidate_ref: str,
    candidate_digest: str,
    policy_digest: str,
    expected_skill_digests: dict[str, str],
    candidate_traces: dict[str, TraceObservation],
    scenario_input_digests: dict[str, str],
    assertion_contracts: tuple[ScenarioAssertionContract, ...] = (),
) -> GateResult:
    binding = _binding_failure(
        run_id=run_id,
        cycle=cycle,
        candidate_ref=candidate_ref,
        candidate_digest=candidate_digest,
        policy_digest=policy_digest,
        evidence_run_id=evidence.run_id,
        evidence_cycle=evidence.cycle,
        evidence_ref=evidence.candidate_ref,
        evidence_digest=evidence.candidate_digest,
        evidence_policy_digest=evidence.policy_digest,
        expected_skill_digests=expected_skill_digests,
        evidence_skill_digests=evidence.skill_digests,
        control_ref=control_ref,
        control_digest=control_digest,
        evidence_control_ref=evidence.control_ref,
        evidence_control_digest=evidence.control_digest,
    )
    if binding:
        return blocked(GateKind.BEHAVIOR_COMPARE, binding)
    if evidence.collector_error is not None:
        return error(
            GateKind.BEHAVIOR_COMPARE,
            f"行为 provider 错误: {evidence.collector_error}",
        )
    if not evidence.collection_complete:
        return blocked(GateKind.BEHAVIOR_COMPARE, "行为对比采集未完成")
    if set(evidence.collected_variants) != {Variant.CONTROL, Variant.CANDIDATE}:
        return blocked(
            GateKind.BEHAVIOR_COMPARE,
            "行为证据必须完整覆盖 control 和 candidate",
        )
    configured = {item.scenario_id for item in spec.scenarios}
    contract_ids = [item.scenario_id for item in assertion_contracts]
    if len(contract_ids) != len(set(contract_ids)):
        return blocked(GateKind.BEHAVIOR_COMPARE, "场景断言契约包含重复 scenario_id")
    invalid_forbidden = sorted(
        item.scenario_id
        for item in assertion_contracts
        if item.forbidden_changed_paths and item.scenario_id not in configured
    )
    if invalid_forbidden:
        return blocked(
            GateKind.BEHAVIOR_COMPARE,
            "禁止行为变化只能绑定 behavior 场景: "
            + ", ".join(invalid_forbidden),
        )
    contract_by_scenario = {
        item.scenario_id: item for item in assertion_contracts
    }
    if assertion_contracts:
        mismatched_forbidden = sorted(
            expected.scenario_id
            for expected in spec.scenarios
            if expected.scenario_id not in contract_by_scenario
            or contract_by_scenario[
                expected.scenario_id
            ].forbidden_changed_paths
            != expected.forbidden_changed_paths
        )
        if mismatched_forbidden:
            return blocked(
                GateKind.BEHAVIOR_COMPARE,
                "forbidden_changed_paths 与受信 behavior policy 不一致: "
                + ", ".join(mismatched_forbidden),
            )
    missing_input_digests = configured - set(scenario_input_digests)
    if missing_input_digests:
        return blocked(
            GateKind.BEHAVIOR_COMPARE,
            "行为门禁缺少冻结场景输入摘要: "
            + ", ".join(sorted(missing_input_digests)),
        )
    required_windows = {
        (scenario_id, variant)
        for scenario_id in configured
        for variant in (Variant.CONTROL, Variant.CANDIDATE)
    }
    collected_windows = {
        (item.scenario_id, item.variant) for item in evidence.collected_scenarios
    }
    if collected_windows != required_windows:
        return blocked(
            GateKind.BEHAVIOR_COMPARE,
            "行为采集窗口必须精确覆盖每个场景的 control 和 candidate",
        )
    window_by_key = {
        (item.scenario_id, item.variant): item
        for item in evidence.collected_scenarios
    }

    failures: list[str] = []
    ids: list[str] = []
    for expected in spec.scenarios:
        matches = [
            item
            for item in evidence.observations
            if item.scenario_id == expected.scenario_id
        ]
        control = [item for item in matches if item.variant is Variant.CONTROL]
        candidate = [item for item in matches if item.variant is Variant.CANDIDATE]
        if len(control) != 1 or len(candidate) != 1:
            return blocked(
                GateKind.BEHAVIOR_COMPARE,
                f"场景 {expected.scenario_id} 必须恰有一份 control 和 candidate 证据",
            )
        old, new = control[0], candidate[0]
        ids.extend(
            (
                old.observation_id,
                new.observation_id,
            )
        )
        if (
            expected.expected_control_outcome is not None
            and old.outcome != expected.expected_control_outcome
        ):
            failures.append(
                f"{expected.scenario_id}: control outcome={old.outcome}, expected "
                f"{expected.expected_control_outcome}"
            )
        if new.outcome != expected.expected_candidate_outcome:
            failures.append(
                f"{expected.scenario_id}: candidate outcome={new.outcome}, expected "
                f"{expected.expected_candidate_outcome}"
            )
        if old.outcome != new.outcome and expected.expected_control_outcome is None:
            failures.append(
                f"{expected.scenario_id}: control/candidate outcome 变化未声明为 reproducer"
            )
        missing_trace = [
            side
            for side, item in (("control", old), ("candidate", new))
            if item.model is None
            or item.tool_calls is None
            or item.finished is None
            or (not item.request_id and not item.session_id)
            or not item.trace_id
            or item.input_digest is None
        ]
        if missing_trace:
            return blocked(
                GateKind.BEHAVIOR_COMPARE,
                f"{expected.scenario_id} 缺少 Trace/输入身份字段: {', '.join(missing_trace)}",
            )
        if old.input_digest != new.input_digest:
            failures.append(
                f"{expected.scenario_id}: control/candidate 未使用同一冻结输入"
            )
        expected_input_digest = scenario_input_digests[expected.scenario_id]
        if (
            old.input_digest != expected_input_digest
            or new.input_digest != expected_input_digest
        ):
            return blocked(
                GateKind.BEHAVIOR_COMPARE,
                f"{expected.scenario_id}: 行为证据未绑定冻结 Plan 输入",
            )
        if any(
            window_by_key[(expected.scenario_id, variant)].input_digest
            != expected_input_digest
            for variant in (Variant.CONTROL, Variant.CANDIDATE)
        ):
            return blocked(
                GateKind.BEHAVIOR_COMPARE,
                f"{expected.scenario_id}: 行为采集窗口与冻结输入摘要不一致",
            )
        if old.finished is not True or new.finished is not True:
            failures.append(f"{expected.scenario_id}: control/candidate 必须完整结束")
        if new.trace_id not in candidate_traces:
            return blocked(
                GateKind.BEHAVIOR_COMPARE,
                f"{expected.scenario_id}: candidate 行为 trace_id 未出现在 Trace 证据",
            )
        trace = candidate_traces[new.trace_id]
        shared_identity = any(
            left is not None and right is not None and left == right
            for left, right in (
                (trace.request_id, new.request_id),
                (trace.session_id, new.session_id),
            )
        )
        conflicting_identity = any(
            left is not None and right is not None and left != right
            for left, right in (
                (trace.request_id, new.request_id),
                (trace.session_id, new.session_id),
            )
        )
        if (
            trace.scenario_id != expected.scenario_id
            or not shared_identity
            or conflicting_identity
            or trace.input_digest != new.input_digest
            or trace.actual_model != new.model
            or trace.finished != new.finished
        ):
            return blocked(
                GateKind.BEHAVIOR_COMPARE,
                f"{expected.scenario_id}: candidate 行为与 Trace 身份或硬指标不一致",
            )
        changed = _changed_paths(old.payload, new.payload)
        if old.model != new.model:
            changed.add("@model")
        if old.tool_calls != new.tool_calls:
            changed.add("@tool_calls")
        if old.finished != new.finished:
            changed.add("@finished")
        forbidden_matches = sorted(
            path
            for path in changed
            if any(
                _path_matches(path, pattern)
                for pattern in expected.forbidden_changed_paths
            )
        )
        if forbidden_matches:
            failures.append(
                f"{expected.scenario_id}: 命中禁止行为变化 {forbidden_matches}"
            )
        unexpected = sorted(
            path
            for path in changed
            if not any(
                _path_matches(path, pattern)
                for pattern in expected.allowed_changed_paths
            )
        )
        if unexpected:
            failures.append(
                f"{expected.scenario_id}: 未声明的行为偏差 {unexpected}"
            )
        missing_required = sorted(
            pattern
            for pattern in expected.required_changed_paths
            if not any(_path_matches(path, pattern) for path in changed)
        )
        if missing_required:
            failures.append(
                f"{expected.scenario_id}: 未观察到必需修复差异 {missing_required}"
            )
    observed = {item.scenario_id for item in evidence.observations}
    extras = sorted(observed - configured)
    if extras:
        failures.append("出现未配置的行为场景: " + ", ".join(extras))
    if failures:
        return GateResult(
            gate=GateKind.BEHAVIOR_COMPARE,
            status=GateStatus.FAIL,
            summary="control/candidate 存在非预期行为偏差",
            evidence_ids=tuple(ids),
            failures=tuple(failures),
        )
    return GateResult(
        gate=GateKind.BEHAVIOR_COMPARE,
        status=GateStatus.PASS,
        summary="control/candidate 行为仅包含已声明差异",
        evidence_ids=tuple(ids),
    )


def aggregate_verdict(results: Iterable[GateResult]) -> VerificationVerdict:
    items = tuple(results)
    counts = {kind: 0 for kind in GateKind}
    for item in items:
        counts[item.gate] += 1
    if any(count != 1 for count in counts.values()):
        return VerificationVerdict.ERROR
    if any(item.status is GateStatus.ERROR for item in items):
        return VerificationVerdict.ERROR
    if any(item.status in {GateStatus.BLOCKED, GateStatus.SKIPPED} for item in items):
        return VerificationVerdict.BLOCKED
    if any(item.status is GateStatus.FAIL for item in items):
        return VerificationVerdict.REJECTED
    if any(
        item.status is GateStatus.NOT_APPLICABLE and item.gate is not GateKind.UI
        for item in items
    ):
        return VerificationVerdict.ERROR
    if all(
        item.status is GateStatus.PASS
        or (item.gate is GateKind.UI and item.status is GateStatus.NOT_APPLICABLE)
        for item in items
    ):
        return VerificationVerdict.VERIFIED
    return VerificationVerdict.ERROR


__all__ = [
    "REQUIRED_GATES",
    "aggregate_verdict",
    "blocked",
    "error",
    "evaluate_behavior_gate",
    "evaluate_command_gate",
    "evaluate_log_gate",
    "evaluate_trace_gate",
    "validate_assertion_contract_definitions",
    "validate_assertion_contracts",
]
