"""诊断计划构建器

DiagnosisPlanner 负责按平台能力与 case 工件筛选可执行 action,并组装 DiagnosisPlan。

gating 规则 (来自 plan Task 4 brief):
- allowed_actions 做静态 gating (不含 budget):
  1. descriptor.status == AVAILABLE;
  2. action.capability_id 在 descriptor.capabilities 中存在;
  3. 该 capability 的 required_artifact_kinds 是 case artifacts kind 集合的子集。
  不满足的 action 由 build_initial_plan 统一进 rejected_actions。
- build_initial_plan 在 allowed_actions 候选之上二次应用 budget 过滤:
  按 descriptor.actions 声明顺序选取, action.estimated_cost 超过剩余预算 -> rejected。

本模块不依赖 core/,不调模型,不执行任何 action。
"""

from diagnose.model import (
    AnalysisActionSpec,
    ArtifactRef,
    DiagnosisCase,
    DiagnosisPlan,
    DiagnosticPlatformDescriptor,
    Hypothesis,
    PlatformStatus,
)


class DiagnosisPlanner:
    """诊断计划构建器

    无状态。allowed_actions 做静态 gating;build_initial_plan 在其上应用 budget,
    并把所有被排除的 action 连同原因写入 rejected_actions,保证计划可审计。
    """

    def allowed_actions(
        self,
        descriptor: DiagnosticPlatformDescriptor,
        artifacts: list[ArtifactRef],
    ) -> list[AnalysisActionSpec]:
        """返回通过静态 gating 的 action 列表 (不考虑 budget)。

        gating 条件全部满足才放行:
        - 平台 status == AVAILABLE;
        - action.capability_id 在 descriptor.capabilities 中存在;
        - capability.required_artifact_kinds 是 case artifacts kind 集合的子集。
        """
        if descriptor.status != PlatformStatus.AVAILABLE:
            return []

        artifact_kinds = {a.kind for a in artifacts}
        cap_by_id = {c.id: c for c in descriptor.capabilities}

        allowed: list[AnalysisActionSpec] = []
        for action in descriptor.actions:
            capability = cap_by_id.get(action.capability_id)
            if capability is None:
                continue
            if not capability.required_artifact_kinds.issubset(artifact_kinds):
                continue
            allowed.append(action)
        return allowed

    def build_initial_plan(
        self,
        case: DiagnosisCase,
        descriptor: DiagnosticPlatformDescriptor,
        hypotheses: list[Hypothesis],
        budget: int,
    ) -> DiagnosisPlan:
        """组装初始 DiagnosisPlan。

        流程:
        1. 调 allowed_actions 拿到静态 gating 候选;
        2. 静态 gating 不通过的 action 进 rejected_actions (带具体原因);
        3. 候选按 descriptor.actions 声明顺序应用 budget, 超预算的进 rejected_actions;
        4. hypotheses 的 id 写入 seed_hypothesis_ids。
        """
        candidates = self.allowed_actions(descriptor, case.artifacts)
        candidate_ids = {a.id for a in candidates}

        rejected: dict[str, str] = {}
        # 先记录静态 gating 不通过的原因
        for action in descriptor.actions:
            if action.id in candidate_ids:
                continue
            rejected[action.id] = self._static_reject_reason(descriptor, action, case.artifacts)

        # 在候选之上应用 budget,按声明顺序扣减
        allowed_ids: list[str] = []
        remaining = budget
        for action in candidates:
            if action.estimated_cost <= remaining:
                allowed_ids.append(action.id)
                remaining -= action.estimated_cost
            else:
                rejected[action.id] = (
                    f"budget exceeded: estimated_cost={action.estimated_cost}, "
                    f"remaining budget={remaining}"
                )

        return DiagnosisPlan(
            allowed_action_ids=allowed_ids,
            rejected_actions=rejected,
            budget=budget,
            seed_hypothesis_ids=[h.id for h in hypotheses],
        )

    @staticmethod
    def _static_reject_reason(
        descriptor: DiagnosticPlatformDescriptor,
        action: AnalysisActionSpec,
        artifacts: list[ArtifactRef],
    ) -> str:
        """给出静态 gating 不通过的具体原因 (status / capability / artifact)。"""
        if descriptor.status != PlatformStatus.AVAILABLE:
            return f"platform not AVAILABLE: {descriptor.status.value}"

        cap_by_id = {c.id: c for c in descriptor.capabilities}
        capability = cap_by_id.get(action.capability_id)
        if capability is None:
            return f"capability not found: {action.capability_id}"

        artifact_kinds = {a.kind for a in artifacts}
        missing = capability.required_artifact_kinds - artifact_kinds
        missing_names = sorted(k.value for k in missing)
        return f"missing required artifact kinds: {missing_names}"
