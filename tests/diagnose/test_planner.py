"""DiagnosisPlanner 与 DiagnosisPlan 测试 - TDD RED 阶段

先写失败测试,锁定 DiagnosisPlan 结构与 DiagnosisPlanner 的 gating 行为,再实现最小代码。

覆盖:
- DiagnosisPlan 默认字段与从 diagnose.model 的导出
- allowed_actions gating: platform status / capability 存在性 / artifact kind 子集
  (allowed_actions 只做静态 gating,不含 budget)
- build_initial_plan: 组装 allowed/rejected/budget/seed_hypothesis_ids,
  并在 allowed_actions 候选之上应用 budget 过滤
"""

import pytest

from diagnose.model import (
    AnalysisActionSpec,
    ArtifactKind,
    ArtifactRef,
    Capability,
    DiagnosisCase,
    DiagnosticPlatformDescriptor,
    DiagnosticTaxonomy,
    Hypothesis,
    PlatformStatus,
)


# --------------------------------------------------------------------------- #
# 测试夹具构造
# --------------------------------------------------------------------------- #
def _capability(cid: str = "cap-log", kinds: set[ArtifactKind] | None = None) -> Capability:
    """构造最小 Capability。"""
    return Capability(
        id=cid,
        description=f"capability {cid}",
        required_artifact_kinds=kinds if kinds is not None else {ArtifactKind.LOG},
    )


def _action(
    aid: str = "act-parse-log",
    cap: str = "cap-log",
    cost: int = 1,
) -> AnalysisActionSpec:
    """构造最小 AnalysisActionSpec。"""
    return AnalysisActionSpec(
        id=aid,
        title=f"action {aid}",
        description=f"description of {aid}",
        capability_id=cap,
        input_schema={},
        estimated_cost=cost,
    )


def _descriptor(
    status: PlatformStatus = PlatformStatus.AVAILABLE,
    capabilities: list[Capability] | None = None,
    actions: list[AnalysisActionSpec] | None = None,
    artifact_kinds: set[ArtifactKind] | None = None,
    pid: str = "fake",
) -> DiagnosticPlatformDescriptor:
    """构造最小可用平台描述符。"""
    return DiagnosticPlatformDescriptor(
        id=pid,
        display_name=f"Fake {pid}",
        status=status,
        description=f"fake platform {pid}",
        taxonomy=DiagnosticTaxonomy(categories={}),
        artifact_kinds=artifact_kinds if artifact_kinds is not None else {ArtifactKind.LOG},
        capabilities=capabilities if capabilities is not None else [_capability()],
        actions=actions if actions is not None else [_action()],
    )


def _artifacts(*kinds: ArtifactKind) -> list[ArtifactRef]:
    """按给定 kinds 构造 ArtifactRef 列表。"""
    return [
        ArtifactRef(id=f"a-{k.value}", kind=k, path=f"/tmp/{k.value}")
        for k in kinds
    ]


def _case(
    artifacts: list[ArtifactRef] | None = None,
    pid: str = "fake",
) -> DiagnosisCase:
    """构造最小 DiagnosisCase,artifacts 默认为单个 LOG 工件。"""
    return DiagnosisCase(
        id="case-1",
        platform_id=pid,
        root_dir="/tmp",
        artifacts=artifacts if artifacts is not None else _artifacts(ArtifactKind.LOG),
    )


def _hypotheses(*ids: str) -> list[Hypothesis]:
    return [
        Hypothesis(id=hid, category="unknown", statement=f"hypothesis {hid}")
        for hid in ids
    ]


# --------------------------------------------------------------------------- #
# DiagnosisPlan 结构
# --------------------------------------------------------------------------- #
class TestDiagnosisPlanModel:
    """DiagnosisPlan 字段默认值与导出。"""

    def test_imports(self):
        from diagnose.model import DiagnosisPlan

        assert DiagnosisPlan is not None

    def test_plan_defaults_empty(self):
        from diagnose.model import DiagnosisPlan

        plan = DiagnosisPlan()
        assert plan.allowed_action_ids == []
        assert plan.rejected_actions == {}
        assert plan.budget == 0
        assert plan.seed_hypothesis_ids == []

    def test_plan_construct_with_values(self):
        from diagnose.model import DiagnosisPlan

        plan = DiagnosisPlan(
            allowed_action_ids=["a1", "a2"],
            rejected_actions={"a3": "missing capability"},
            budget=5,
            seed_hypothesis_ids=["h1"],
        )
        assert plan.allowed_action_ids == ["a1", "a2"]
        assert plan.rejected_actions == {"a3": "missing capability"}
        assert plan.budget == 5
        assert plan.seed_hypothesis_ids == ["h1"]


# --------------------------------------------------------------------------- #
# allowed_actions gating
# --------------------------------------------------------------------------- #
class TestAllowedActionsGating:
    """allowed_actions 按 status / capability / artifact kind 过滤 (不含 budget)。"""

    def test_imports_planner(self):
        from diagnose.planner import DiagnosisPlanner

        assert DiagnosisPlanner is not None

    def test_planned_platform_returns_empty(self):
        """PLANNED 平台没有可执行分析能力,allowed_actions 返回空。"""
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(status=PlatformStatus.PLANNED)
        result = DiagnosisPlanner().allowed_actions(desc, _artifacts(ArtifactKind.LOG))
        assert result == []

    def test_disabled_platform_returns_empty(self):
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(status=PlatformStatus.DISABLED)
        result = DiagnosisPlanner().allowed_actions(desc, _artifacts(ArtifactKind.LOG))
        assert result == []

    def test_available_with_matching_artifact_returns_action(self):
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(status=PlatformStatus.AVAILABLE)
        result = DiagnosisPlanner().allowed_actions(desc, _artifacts(ArtifactKind.LOG))
        assert len(result) == 1
        assert result[0].id == "act-parse-log"

    def test_capability_not_in_descriptor_returns_empty(self):
        """action 引用了 descriptor.capabilities 中不存在的 capability_id。"""
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(
            status=PlatformStatus.AVAILABLE,
            capabilities=[_capability(cid="cap-log")],
            actions=[_action(aid="act-x", cap="cap-missing")],
        )
        result = DiagnosisPlanner().allowed_actions(desc, _artifacts(ArtifactKind.LOG))
        assert result == []

    def test_required_artifact_kind_missing_returns_empty(self):
        """capability 要求 HEAP_SNAPSHOT,但 case 只有 LOG。"""
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(
            status=PlatformStatus.AVAILABLE,
            capabilities=[_capability(cid="cap-heap", kinds={ArtifactKind.HEAP_SNAPSHOT})],
            actions=[_action(aid="act-heap", cap="cap-heap")],
            artifact_kinds={ArtifactKind.HEAP_SNAPSHOT},
        )
        result = DiagnosisPlanner().allowed_actions(desc, _artifacts(ArtifactKind.LOG))
        assert result == []

    def test_required_artifact_kind_subset_returns_action(self):
        """capability 要求 {LOG, SOURCE},case 同时拥有两者 -> 放行。"""
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(
            status=PlatformStatus.AVAILABLE,
            capabilities=[
                _capability(cid="cap-both", kinds={ArtifactKind.LOG, ArtifactKind.SOURCE})
            ],
            actions=[_action(aid="act-both", cap="cap-both")],
            artifact_kinds={ArtifactKind.LOG, ArtifactKind.SOURCE},
        )
        result = DiagnosisPlanner().allowed_actions(
            desc, _artifacts(ArtifactKind.LOG, ArtifactKind.SOURCE)
        )
        assert len(result) == 1
        assert result[0].id == "act-both"

    def test_partial_actions_filtered(self):
        """两个 action,只有一个满足 artifact 要求。"""
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(
            status=PlatformStatus.AVAILABLE,
            capabilities=[
                _capability(cid="cap-log", kinds={ArtifactKind.LOG}),
                _capability(cid="cap-heap", kinds={ArtifactKind.HEAP_SNAPSHOT}),
            ],
            actions=[
                _action(aid="act-log", cap="cap-log"),
                _action(aid="act-heap", cap="cap-heap"),
            ],
            artifact_kinds={ArtifactKind.LOG, ArtifactKind.HEAP_SNAPSHOT},
        )
        result = DiagnosisPlanner().allowed_actions(desc, _artifacts(ArtifactKind.LOG))
        assert len(result) == 1
        assert result[0].id == "act-log"

    def test_allowed_actions_ignores_budget(self):
        """allowed_actions 只做静态 gating,不考虑 budget (cost=99 仍返回)。"""
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(
            status=PlatformStatus.AVAILABLE,
            actions=[_action(aid="act-pricey", cost=99)],
        )
        result = DiagnosisPlanner().allowed_actions(desc, _artifacts(ArtifactKind.LOG))
        assert len(result) == 1
        assert result[0].estimated_cost == 99


# --------------------------------------------------------------------------- #
# build_initial_plan
# --------------------------------------------------------------------------- #
class TestBuildInitialPlan:
    """build_initial_plan 组装 DiagnosisPlan,并在候选之上应用 budget 过滤。"""

    def test_build_plan_allowed_and_budget(self):
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(status=PlatformStatus.AVAILABLE)
        plan = DiagnosisPlanner().build_initial_plan(
            case=_case(),
            descriptor=desc,
            hypotheses=[],
            budget=3,
        )
        assert "act-parse-log" in plan.allowed_action_ids
        assert plan.budget == 3

    def test_build_plan_budget_filters_costly_action(self):
        """action cost=5 但 budget=2 -> 进 rejected_actions,不进 allowed。"""
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(
            status=PlatformStatus.AVAILABLE,
            actions=[_action(aid="act-pricey", cost=5)],
        )
        plan = DiagnosisPlanner().build_initial_plan(
            case=_case(),
            descriptor=desc,
            hypotheses=[],
            budget=2,
        )
        assert plan.allowed_action_ids == []
        assert "act-pricey" in plan.rejected_actions
        assert "budget" in plan.rejected_actions["act-pricey"]

    def test_build_plan_includes_rejected_from_static_gating(self):
        """静态 gating (capability/artifact) 不通过的 action 也进 rejected_actions。"""
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(
            status=PlatformStatus.AVAILABLE,
            capabilities=[_capability(cid="cap-log")],
            actions=[
                _action(aid="act-ok", cap="cap-log"),
                _action(aid="act-bad", cap="cap-missing"),
            ],
        )
        plan = DiagnosisPlanner().build_initial_plan(
            case=_case(),
            descriptor=desc,
            hypotheses=[],
            budget=10,
        )
        assert "act-ok" in plan.allowed_action_ids
        assert "act-bad" in plan.rejected_actions

    def test_build_plan_records_seed_hypothesis_ids(self):
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(status=PlatformStatus.AVAILABLE)
        plan = DiagnosisPlanner().build_initial_plan(
            case=_case(),
            descriptor=desc,
            hypotheses=_hypotheses("h1", "h2"),
            budget=1,
        )
        assert plan.seed_hypothesis_ids == ["h1", "h2"]

    def test_build_plan_planned_platform_rejects_all(self):
        """PLANNED 平台所有 action 进 rejected_actions,allowed 为空。"""
        from diagnose.planner import DiagnosisPlanner

        desc = _descriptor(
            status=PlatformStatus.PLANNED,
            actions=[_action(aid="act-a"), _action(aid="act-b")],
        )
        plan = DiagnosisPlanner().build_initial_plan(
            case=_case(),
            descriptor=desc,
            hypotheses=[],
            budget=10,
        )
        assert plan.allowed_action_ids == []
        assert set(plan.rejected_actions.keys()) == {"act-a", "act-b"}
