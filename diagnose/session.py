"""诊断会话

DiagnosisSession 持有单次诊断 case 的全部运行时状态,协调平台、计划与调用记录。

一期状态机 (来自 Task 4 brief, controller 裁定):
一期 session 持有基础 DiagnosticPlatform (无 execute), 且不接 ExecutableDiagnosticPlatform,
故一期绝不执行任何 action。run_action 的状态转移:
  1. 去重缓存: 相同 action_id + normalized arguments + hypothesis_id 已有 invocation 记录
     -> 返回新 ActionInvocation(status="cached", ...), 复用首次 evidence_ids, 不重复 gating。
  2. gating 不过 (平台非 AVAILABLE / action 不在 plan.allowed_action_ids / 预算不足)
     -> status="rejected", reason 具体说明。
  3. gating 通过但一期不执行
     -> status="rejected", reason="phase 1: action execution not available (...)"。
  4. 每个请求都记入 invocations。

本模块不依赖 core/, 不调模型, 不调用平台 execute。
"""

import json
from typing import Any, Literal

from diagnose.catalog import EvidenceCatalog
from diagnose.model import (
    ActionInvocation,
    AnalysisActionRequest,
    DiagnosisCase,
    DiagnosisPlan,
    DiagnosisResult,
    DiagnosisStatus,
    DiagnosticPlatformDescriptor,
    Hypothesis,
    PlatformStatus,
)
from diagnose.platform import DiagnosticPlatform

# 一期 invocation id 前缀与宽度, 按 session 内顺序单调递增。
_INVOCATION_ID_PREFIX = "INV"
_INVOCATION_ID_WIDTH = 4

# 一期 gating 通过但无 execute 时的统一拒因。
_PHASE1_NO_EXECUTE_REASON = (
    "phase 1: action execution not available (no ExecutableDiagnosticPlatform)"
)


def _normalize_arguments(arguments: dict[str, Any]) -> str:
    """把参数字典规范化为稳定字串, 用于去重比对。

    使用 sort_keys=True 保证键顺序无关; default=str 兜底不可序列化对象。
    """
    return json.dumps(arguments, sort_keys=True, default=str)


class DiagnosisSession:
    """诊断会话

    持有 case / platform / descriptor / catalog / hypotheses / plan / invocations。
    一期只读式协调: 不调用平台 execute, 所有 gating 通过的请求也以 rejected (phase 1) 结束。
    """

    def __init__(
        self,
        case: DiagnosisCase,
        platform: DiagnosticPlatform,
        descriptor: DiagnosticPlatformDescriptor,
        catalog: EvidenceCatalog,
        hypotheses: list[Hypothesis],
        plan: DiagnosisPlan,
    ) -> None:
        self.case: DiagnosisCase = case
        self.platform: DiagnosticPlatform = platform
        self.descriptor: DiagnosticPlatformDescriptor = descriptor
        self.catalog: EvidenceCatalog = catalog
        self.hypotheses: list[Hypothesis] = list(hypotheses)
        self.plan: DiagnosisPlan = plan
        self.invocations: list[ActionInvocation] = []
        self._next_invocation_seq: int = 0

    # ------------------------------------------------------------------ #
    # run_action 一期状态机
    # ------------------------------------------------------------------ #
    def run_action(self, request: AnalysisActionRequest) -> ActionInvocation:
        """处理一次动作请求,按一期状态机产出 invocation 并记入 invocations。

        顺序: 去重缓存 -> gating 拒绝 -> phase 1 拒绝。绝不调用 platform.execute。
        """
        normalized = _normalize_arguments(request.arguments)

        # 1. 去重缓存: 仅按 action_id + normalized args + hypothesis_id 匹配, 不看状态。
        cached = self._find_cached(request.action_id, normalized, request.hypothesis_id)
        if cached is not None:
            invocation = self._new_invocation(
                action_id=cached.action_id,
                arguments=cached.arguments,
                hypothesis_id=cached.hypothesis_id,
                status="cached",
                evidence_ids=list(cached.evidence_ids),
                reason="duplicate request: cached from prior invocation",
            )
            self.invocations.append(invocation)
            return invocation

        # 2. gating: 平台状态 / 计划允许列表 / 预算
        reject_reason = self._gate(request)
        if reject_reason is None:
            # 3. gating 通过但一期不执行
            reject_reason = _PHASE1_NO_EXECUTE_REASON

        invocation = self._new_invocation(
            action_id=request.action_id,
            arguments=request.arguments,
            hypothesis_id=request.hypothesis_id,
            status="rejected",
            evidence_ids=[],
            reason=reject_reason,
        )
        self.invocations.append(invocation)
        return invocation

    # ------------------------------------------------------------------ #
    # build_result
    # ------------------------------------------------------------------ #
    def build_result(self) -> DiagnosisResult:
        """组装 DiagnosisResult。

        一期裁定:
        - PLANNED / DISABLED 平台 -> INSUFFICIENT_CAPABILITY, 列出缺失能力与追问。
        - AVAILABLE 平台: 一期未执行任何 action (无证据) -> INCONCLUSIVE。
        - root_cause 一期恒为 None。
        """
        status, missing, follow_ups = self._derive_status_and_gaps()

        return DiagnosisResult(
            case_id=self.case.id,
            platform_id=self.descriptor.id,
            status=status,
            root_cause=None,
            invocations=list(self.invocations),
            evidence=self.catalog.all(),
            hypotheses=list(self.hypotheses),
            missing_capabilities=missing,
            follow_up_questions=follow_ups,
        )

    # ------------------------------------------------------------------ #
    # 内部: 去重匹配
    # ------------------------------------------------------------------ #
    def _find_cached(
        self,
        action_id: str,
        normalized_args: str,
        hypothesis_id: str | None,
    ) -> ActionInvocation | None:
        """返回首个匹配 action_id + normalized args + hypothesis_id 的 invocation。

        匹配不看 status: 即使首次是 rejected, 第二次相同请求也命中缓存。
        """
        for inv in self.invocations:
            if inv.action_id != action_id:
                continue
            if inv.hypothesis_id != hypothesis_id:
                continue
            if _normalize_arguments(inv.arguments) != normalized_args:
                continue
            return inv
        return None

    # ------------------------------------------------------------------ #
    # 内部: gating
    # ------------------------------------------------------------------ #
    def _gate(self, request: AnalysisActionRequest) -> str | None:
        """返回拒因; 返回 None 表示 gating 通过 (一期随即被 phase 1 拒绝)。

        一期不可达: gating 通过的 action 都被 phase 1 rejected (无 execute 接入)。
        预留 execute 接入后生效。
        """
        if self.descriptor.status != PlatformStatus.AVAILABLE:
            return f"platform not AVAILABLE: {self.descriptor.status.value}"

        if request.action_id not in self.plan.allowed_action_ids:
            reason = self.plan.rejected_actions.get(request.action_id)
            if reason is not None:
                return f"action not allowed: {reason}"
            return f"action not allowed: unknown action {request.action_id!r}"

        remaining = self._remaining_budget()
        cost = self._action_cost(request.action_id)
        if cost > remaining:
            return (
                f"budget exhausted: estimated_cost={cost}, remaining budget={remaining}"
            )
        return None

    def _remaining_budget(self) -> int:
        """剩余预算 = plan.budget - 已 completed invocation 的 cost 之和。

        一期没有 completed invocation, 故始终等于 plan.budget。保留扣减逻辑以备后续 execute 接入。
        """
        spent = 0
        for inv in self.invocations:
            if inv.status == "completed":
                spent += self._action_cost(inv.action_id)
        return self.plan.budget - spent

    def _action_cost(self, action_id: str) -> int:
        """从 descriptor.actions 查 action 的 estimated_cost, 未知则按 1 计。"""
        for action in self.descriptor.actions:
            if action.id == action_id:
                return action.estimated_cost
        return 1

    # ------------------------------------------------------------------ #
    # 内部: build_result 状态裁定
    # ------------------------------------------------------------------ #
    def _derive_status_and_gaps(
        self,
    ) -> tuple[DiagnosisStatus, list[str], list[str]]:
        """根据平台状态与已收集证据裁定结果状态、缺失能力、追问。

        - PLANNED: 缺可执行分析能力, INSUFFICIENT_CAPABILITY;
        - DISABLED: 平台被禁用, INSUFFICIENT_CAPABILITY;
        - AVAILABLE 但无证据 (一期未执行 action): INCONCLUSIVE;
        - AVAILABLE 且有证据: 仍 INCONCLUSIVE (一期不构建 root_cause)。
        """
        missing: list[str] = []
        follow_ups: list[str] = []

        if self.descriptor.status == PlatformStatus.PLANNED:
            missing.append(
                f"platform {self.descriptor.id} is PLANNED: "
                "no executable analysis capability available"
            )
            follow_ups.append(
                f"enable or implement analysis actions for platform {self.descriptor.id}"
            )
            return DiagnosisStatus.INSUFFICIENT_CAPABILITY, missing, follow_ups

        if self.descriptor.status == PlatformStatus.DISABLED:
            missing.append(
                f"platform {self.descriptor.id} is DISABLED: "
                "registered but turned off in configuration"
            )
            follow_ups.append(
                f"enable platform {self.descriptor.id} in registry configuration"
            )
            return DiagnosisStatus.INSUFFICIENT_CAPABILITY, missing, follow_ups

        # AVAILABLE: 一期不执行 action, 无证据 -> INCONCLUSIVE。
        if not self.catalog.all():
            follow_ups.append(
                "no evidence collected: phase 1 does not execute analysis actions"
            )
            return DiagnosisStatus.INCONCLUSIVE, missing, follow_ups

        # 有证据 (后续 execute 接入后才会出现): 一期仍不构建 root_cause。
        return DiagnosisStatus.INCONCLUSIVE, missing, follow_ups

    # ------------------------------------------------------------------ #
    # 内部: invocation 构造
    # ------------------------------------------------------------------ #
    def _new_invocation(
        self,
        action_id: str,
        arguments: dict[str, Any],
        hypothesis_id: str | None,
        status: Literal["cached", "rejected"],
        evidence_ids: list[str],
        reason: str,
    ) -> ActionInvocation:
        """分配顺序 id 并构造 invocation (不写入 invocations, 由调用方决定)。"""
        self._next_invocation_seq += 1
        invocation_id = (
            f"{_INVOCATION_ID_PREFIX}-{self._next_invocation_seq:0{_INVOCATION_ID_WIDTH}d}"
        )
        return ActionInvocation(
            id=invocation_id,
            action_id=action_id,
            arguments=dict(arguments),
            hypothesis_id=hypothesis_id,
            status=status,
            evidence_ids=evidence_ids,
            reason=reason,
        )
