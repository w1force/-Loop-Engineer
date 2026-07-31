"""DiagnosisSession 与 create_diagnosis_session 测试 - TDD RED 阶段

先写失败测试,锁定 session 一期状态机与 build_result 行为,再实现最小代码。

覆盖:
- create_diagnosis_session: 解析平台 -> inspect_case -> 构造 initial plan -> 构造 session
- run_action 一期状态机:
  * 去重缓存 (相同 action_id + normalized args + hypothesis_id -> cached)
  * gating 不过 -> rejected (带原因)
  * gating 通过但一期不执行 -> rejected ("phase 1: action execution not available")
  * session 一期绝不调用 fake.execute
- build_result: PLANNED -> INSUFFICIENT_CAPABILITY; AVAILABLE 无证据 -> INCONCLUSIVE
"""

import pytest

from diagnose.model import (
    AnalysisActionRequest,
    ArtifactKind,
    ArtifactRef,
    Capability,
    DiagnosisCase,
    DiagnosisStatus,
    DiagnosticPlatformDescriptor,
    DiagnosticTaxonomy,
    Hypothesis,
    PlatformStatus,
)
from diagnose.platform import DiagnosticPlatform
from diagnose.registry import PlatformRegistry


# --------------------------------------------------------------------------- #
# 测试夹具构造
# --------------------------------------------------------------------------- #
def _capability(cid: str = "cap-log", kinds: set[ArtifactKind] | None = None) -> Capability:
    return Capability(
        id=cid,
        description=f"capability {cid}",
        required_artifact_kinds=kinds if kinds is not None else {ArtifactKind.LOG},
    )


def _action(aid: str = "act-parse-log", cap: str = "cap-log", cost: int = 1):
    from diagnose.model import AnalysisActionSpec

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
    capabilities=None,
    actions=None,
    pid: str = "fake",
) -> DiagnosticPlatformDescriptor:
    return DiagnosticPlatformDescriptor(
        id=pid,
        display_name=f"Fake {pid}",
        status=status,
        description=f"fake platform {pid}",
        taxonomy=DiagnosticTaxonomy(categories={}),
        artifact_kinds={ArtifactKind.LOG},
        capabilities=capabilities if capabilities is not None else [_capability()],
        actions=actions if actions is not None else [_action()],
    )


class FakeExecutablePlatform(DiagnosticPlatform):
    """带 execute 方法的 fake 平台 (超出 Protocol),用于断言一期不调用 execute。

    inspect_case 原样返回 case.artifacts;seed_hypotheses 返回构造时给定的列表。
    execute 方法仅记录调用次数,不应被一期 session 触发。
    """

    def __init__(
        self,
        descriptor: DiagnosticPlatformDescriptor,
        hypotheses: list[Hypothesis] | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._hypotheses = list(hypotheses) if hypotheses else []
        self.execute_call_count = 0

    @property
    def descriptor(self) -> DiagnosticPlatformDescriptor:
        return self._descriptor

    def inspect_case(self, case: DiagnosisCase) -> list[ArtifactRef]:
        return list(case.artifacts)

    def seed_hypotheses(self, case: DiagnosisCase) -> list[Hypothesis]:
        return list(self._hypotheses)

    def execute(self, request, context=None):
        """超出基础 Protocol 的执行方法,一期 session 不应调用。"""
        self.execute_call_count += 1
        return []


def _registry_with(platform) -> PlatformRegistry:
    reg = PlatformRegistry()
    reg.register(platform)
    return reg


def _case(pid: str = "fake", artifacts=None) -> DiagnosisCase:
    return DiagnosisCase(
        id="case-1",
        platform_id=pid,
        root_dir="/tmp",
        artifacts=artifacts if artifacts is not None else [
            ArtifactRef(id="a-log", kind=ArtifactKind.LOG, path="/app.log"),
        ],
    )


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #
class TestImports:
    def test_imports(self):
        from diagnose.api import create_diagnosis_session
        from diagnose.session import DiagnosisSession

        assert create_diagnosis_session is not None
        assert DiagnosisSession is not None

    def test_fake_platform_satisfies_protocol(self):
        """FakeExecutablePlatform 满足基础 DiagnosticPlatform Protocol。"""
        platform = FakeExecutablePlatform(_descriptor())
        assert isinstance(platform, DiagnosticPlatform)


# --------------------------------------------------------------------------- #
# create_diagnosis_session
# --------------------------------------------------------------------------- #
class TestCreateSession:
    """公共入口: 解析平台 -> inspect_case -> 构造 plan -> 构造 session。"""

    def test_create_session_returns_session_with_case(self):
        from diagnose.api import create_diagnosis_session
        from diagnose.session import DiagnosisSession

        platform = FakeExecutablePlatform(_descriptor())
        registry = _registry_with(platform)
        case = _case()

        session = create_diagnosis_session(case, registry)
        assert isinstance(session, DiagnosisSession)
        assert session.case.id == "case-1"

    def test_create_session_resolves_platform_from_registry(self):
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(pid="java-jvm"))
        registry = _registry_with(platform)
        case = _case(pid="java-jvm")

        session = create_diagnosis_session(case, registry)
        assert session.platform is platform
        assert session.descriptor.id == "java-jvm"

    def test_create_session_invokes_inspect_case(self):
        """inspect_case 用于补全/校验 artifacts,session 应持有补全后的 artifacts。"""
        from diagnose.api import create_diagnosis_session

        # 让 fake inspect_case 返回一个额外的标记 artifact,验证 inspect 产出被采用
        platform = FakeExecutablePlatform(_descriptor())
        original_inspect = platform.inspect_case

        def inspect_with_extra_marker(case):
            base_artifacts = original_inspect(case)
            # 返回原 artifacts + 一个标记 artifact
            return base_artifacts + [
                ArtifactRef(id="inspected-extra", kind=ArtifactKind.LOG, path="/marker.log")
            ]

        platform.inspect_case = inspect_with_extra_marker
        registry = _registry_with(platform)
        case = _case()

        session = create_diagnosis_session(case, registry)
        # 验证 inspect 产出被采用: 标记 artifact 应出现在 session.case.artifacts
        artifact_ids = {a.id for a in session.case.artifacts}
        assert "inspected-extra" in artifact_ids

    def test_create_session_builds_initial_plan(self):
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(
            _descriptor(status=PlatformStatus.AVAILABLE),
            hypotheses=_hypotheses("h1"),
        )
        registry = _registry_with(platform)

        session = create_diagnosis_session(_case(), registry)
        assert session.plan is not None
        assert "act-parse-log" in session.plan.allowed_action_ids
        assert session.plan.seed_hypothesis_ids == ["h1"]

    def test_create_session_unknown_platform_raises(self):
        from diagnose.api import create_diagnosis_session
        from diagnose.errors import UnknownPlatformError

        registry = PlatformRegistry()
        with pytest.raises(UnknownPlatformError):
            create_diagnosis_session(_case(pid="ghost"), registry)

    # ------------------------------------------------------------------ #
    # budget 解析: 显式参数 > case.metadata["budget"] > _DEFAULT_BUDGET(10)
    # ------------------------------------------------------------------ #
    def _two_action_descriptor(self) -> DiagnosticPlatformDescriptor:
        """两个 cost=1 的 action (共享 cap-log), 用于检验 budget 截断。"""
        return _descriptor(
            status=PlatformStatus.AVAILABLE,
            actions=[_action(aid="act-a", cost=1), _action(aid="act-b", cost=1)],
        )

    def test_create_session_explicit_budget_limits_allowed_actions(self):
        """显式 budget=1: 只够放行第一个 action, 第二个因预算不足进 rejected。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(self._two_action_descriptor())
        registry = _registry_with(platform)

        session = create_diagnosis_session(_case(), registry, budget=1)
        plan = session.plan

        assert plan.budget == 1
        assert plan.allowed_action_ids == ["act-a"]
        assert "act-b" in plan.rejected_actions
        assert "budget" in plan.rejected_actions["act-b"]

    def test_create_session_explicit_budget_overrides_metadata(self):
        """显式 budget 优先级高于 case.metadata["budget"]。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(self._two_action_descriptor())
        registry = _registry_with(platform)
        # metadata 给 5 (足够放行两个), 显式给 1 应压过 metadata。
        case = _case().model_copy(update={"metadata": {"budget": 5}})

        session = create_diagnosis_session(case, registry, budget=1)
        plan = session.plan

        assert plan.budget == 1
        assert plan.allowed_action_ids == ["act-a"]
        assert "act-b" in plan.rejected_actions

    def test_create_session_budget_falls_back_to_metadata(self):
        """不传显式 budget 时走 case.metadata["budget"]。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(self._two_action_descriptor())
        registry = _registry_with(platform)
        case = _case().model_copy(update={"metadata": {"budget": 1}})

        session = create_diagnosis_session(case, registry)
        plan = session.plan

        assert plan.budget == 1
        assert plan.allowed_action_ids == ["act-a"]
        assert "act-b" in plan.rejected_actions

    def test_create_session_budget_falls_back_to_default(self):
        """既不传显式 budget, metadata 也没给 -> 缺省 10, 两个 cost=1 action 全放行。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(self._two_action_descriptor())
        registry = _registry_with(platform)

        session = create_diagnosis_session(_case(), registry)
        plan = session.plan

        # _DEFAULT_BUDGET 锁死为 10 (见 docs/diagnosis/case-format.md)。
        assert plan.budget == 10
        assert plan.allowed_action_ids == ["act-a", "act-b"]
        assert plan.rejected_actions == {}

    def test_create_session_explicit_invalid_budget_falls_through(self):
        """显式传入非法 budget (bool / 非正 int) 应回退, 不进 plan。

        bool 与非正 int 与 metadata 路径走同一兜底: 这里 metadata 也缺省 -> 回退到 10。
        """
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(self._two_action_descriptor())
        registry = _registry_with(platform)

        # bool True 不能被当作 1; 0 / 负数也不能作为预算。
        for bad in (True, 0, -3):
            session = create_diagnosis_session(_case(), registry, budget=bad)
            assert session.plan.budget == 10, f"bad budget={bad!r} 不应进入 plan"
            assert session.plan.allowed_action_ids == ["act-a", "act-b"]


def _hypotheses(*ids: str) -> list[Hypothesis]:
    return [
        Hypothesis(id=hid, category="unknown", statement=f"hypothesis {hid}")
        for hid in ids
    ]


# --------------------------------------------------------------------------- #
# run_action 一期状态机
# --------------------------------------------------------------------------- #
class TestRunActionStateMachine:
    """run_action 状态机: 缓存 / gating 拒绝 / phase 1 不执行。"""

    def test_planned_platform_rejects_action(self):
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.PLANNED))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        inv = session.run_action(AnalysisActionRequest(action_id="act-parse-log"))
        assert inv.status == "rejected"
        assert inv.reason is not None
        assert "AVAILABLE" in inv.reason or "not" in inv.reason

    def test_action_not_in_allowed_rejected(self):
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(
            _descriptor(
                status=PlatformStatus.AVAILABLE,
                capabilities=[_capability(cid="cap-log")],
                actions=[_action(aid="act-ok", cap="cap-log")],
            )
        )
        session = create_diagnosis_session(_case(), _registry_with(platform))

        inv = session.run_action(AnalysisActionRequest(action_id="act-unknown"))
        assert inv.status == "rejected"
        assert inv.reason is not None
        assert "act-unknown" in inv.reason

    def test_action_artifact_missing_rejected(self):
        """action 的 capability 要求 HEAP_SNAPSHOT,case 没有,action 不在 allowed。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(
            _descriptor(
                status=PlatformStatus.AVAILABLE,
                capabilities=[_capability(cid="cap-heap", kinds={ArtifactKind.HEAP_SNAPSHOT})],
                actions=[_action(aid="act-heap", cap="cap-heap")],
            )
        )
        session = create_diagnosis_session(_case(), _registry_with(platform))

        inv = session.run_action(AnalysisActionRequest(action_id="act-heap"))
        assert inv.status == "rejected"
        assert inv.reason is not None
        # 验证 reason 包含缺失的 artifact kinds 信息（枚举 value 为小写下划线格式）
        assert "missing required artifact kinds" in inv.reason
        assert "heap_snapshot" in inv.reason

    def test_gating_pass_but_phase1_rejects(self):
        """gating 全通过,但一期不执行 -> rejected,reason 含 'phase 1'。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.AVAILABLE))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        inv = session.run_action(AnalysisActionRequest(action_id="act-parse-log"))
        assert inv.status == "rejected"
        assert inv.reason is not None
        assert "phase 1" in inv.reason
        assert "execution not available" in inv.reason

    def test_run_action_records_invocation(self):
        """每个请求都记入 invocations。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.AVAILABLE))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        before = len(session.invocations)
        session.run_action(AnalysisActionRequest(action_id="act-parse-log"))
        after = len(session.invocations)
        assert after == before + 1

    def test_run_action_never_calls_execute(self):
        """关键: session 一期绝不调用 fake.execute。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.AVAILABLE))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        session.run_action(AnalysisActionRequest(action_id="act-parse-log"))
        session.run_action(AnalysisActionRequest(action_id="act-parse-log"))
        session.run_action(AnalysisActionRequest(action_id="act-unknown"))
        assert platform.execute_call_count == 0


# --------------------------------------------------------------------------- #
# 去重缓存
# --------------------------------------------------------------------------- #
class TestRunActionDedupCache:
    """相同 action_id + normalized args + hypothesis_id 第二次返回 cached。"""

    def test_duplicate_request_returns_cached(self):
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.AVAILABLE))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        req = AnalysisActionRequest(action_id="act-parse-log", arguments={"k": "v"})
        first = session.run_action(req)
        second = session.run_action(req)

        assert first.status == "rejected"  # phase 1
        assert second.status == "cached"

    def test_cached_preserves_evidence_ids(self):
        """cached invocation 复用首次的 evidence_ids (一期为空)。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.AVAILABLE))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        req = AnalysisActionRequest(action_id="act-parse-log")
        first = session.run_action(req)
        second = session.run_action(req)

        assert second.evidence_ids == first.evidence_ids

    def test_different_arguments_not_cached(self):
        """arguments 不同 -> 不命中缓存,正常走 gating。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.AVAILABLE))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        first = session.run_action(
            AnalysisActionRequest(action_id="act-parse-log", arguments={"a": 1})
        )
        second = session.run_action(
            AnalysisActionRequest(action_id="act-parse-log", arguments={"a": 2})
        )
        assert first.status == "rejected"
        assert second.status == "rejected"  # 不同参数,不缓存

    def test_different_argument_order_cached(self):
        """arguments 键顺序不同但内容相同 -> 命中缓存 (normalized 比较)。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.AVAILABLE))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        first = session.run_action(
            AnalysisActionRequest(action_id="act-parse-log", arguments={"a": 1, "b": 2})
        )
        second = session.run_action(
            AnalysisActionRequest(action_id="act-parse-log", arguments={"b": 2, "a": 1})
        )
        assert first.status == "rejected"
        assert second.status == "cached"

    def test_different_hypothesis_id_not_cached(self):
        """hypothesis_id 不同 -> 不命中缓存。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.AVAILABLE))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        first = session.run_action(
            AnalysisActionRequest(action_id="act-parse-log", hypothesis_id="h1")
        )
        second = session.run_action(
            AnalysisActionRequest(action_id="act-parse-log", hypothesis_id="h2")
        )
        assert first.status == "rejected"
        assert second.status == "rejected"

    def test_different_action_id_not_cached(self):
        """action_id 不同 -> 不命中缓存。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(
            _descriptor(
                status=PlatformStatus.AVAILABLE,
                capabilities=[_capability(cid="cap-log")],
                actions=[
                    _action(aid="act-a", cap="cap-log"),
                    _action(aid="act-b", cap="cap-log"),
                ],
            )
        )
        session = create_diagnosis_session(_case(), _registry_with(platform))

        first = session.run_action(AnalysisActionRequest(action_id="act-a"))
        second = session.run_action(AnalysisActionRequest(action_id="act-b"))
        assert first.status == "rejected"
        assert second.status == "rejected"


# --------------------------------------------------------------------------- #
# build_result
# --------------------------------------------------------------------------- #
class TestBuildResult:
    """build_result 组装 DiagnosisResult (一期 root_cause=None)。"""

    def test_planned_platform_insufficient_capability(self):
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.PLANNED))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        result = session.build_result()
        assert result.status == DiagnosisStatus.INSUFFICIENT_CAPABILITY
        assert result.root_cause is None
        # PLANNED 平台缺失能力应有可审计说明
        assert len(result.missing_capabilities) > 0

    def test_planned_platform_missing_capabilities_describe_reason(self):
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.PLANNED))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        result = session.build_result()
        joined = " ".join(result.missing_capabilities)
        assert "PLANNED" in joined or "planned" in joined

    def test_available_no_evidence_inconclusive(self):
        """AVAILABLE 但一期未执行任何 action (无证据) -> INCONCLUSIVE。"""
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.AVAILABLE))
        session = create_diagnosis_session(_case(), _registry_with(platform))

        result = session.build_result()
        assert result.status == DiagnosisStatus.INCONCLUSIVE
        assert result.root_cause is None

    def test_build_result_carries_case_and_platform_ids(self):
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(pid="java-jvm"))
        session = create_diagnosis_session(_case(pid="java-jvm"), _registry_with(platform))

        result = session.build_result()
        assert result.case_id == "case-1"
        assert result.platform_id == "java-jvm"

    def test_build_result_includes_invocations(self):
        from diagnose.api import create_diagnosis_session

        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.AVAILABLE))
        session = create_diagnosis_session(_case(), _registry_with(platform))
        session.run_action(AnalysisActionRequest(action_id="act-parse-log"))

        result = session.build_result()
        assert len(result.invocations) >= 1


# --------------------------------------------------------------------------- #
# get_context
# --------------------------------------------------------------------------- #
class TestGetContext:
    """get_context: artifact 额外带 absolute_path (供需绝对路径的 MCP 工具)。"""

    def test_artifact_includes_absolute_path(self, tmp_path):
        # absolute_path 属 case-specific 可变信息, 走 GetDiagnosisContext (这里),
        # 不进被 <system-reminder> 包的平台 guidance 稳定块。
        from diagnose.api import create_diagnosis_session

        (tmp_path / "app.log").write_text("x")
        case = DiagnosisCase(
            id="c",
            platform_id="fake",
            root_dir=str(tmp_path),
            artifacts=[ArtifactRef(id="a-log", kind=ArtifactKind.LOG, path="app.log")],
        )
        platform = FakeExecutablePlatform(_descriptor(status=PlatformStatus.AVAILABLE))
        session = create_diagnosis_session(case, _registry_with(platform))

        ctx = session.get_context()
        art = ctx["artifacts"][0]
        assert art["absolute_path"] == str((tmp_path / "app.log").resolve())
        # 相对 path 仍保留 (ArtifactRef 原字段不变)
        assert art["path"] == "app.log"
