"""诊断公共入口

create_diagnosis_session 是一期的公共 API,把平台解析、工件识别、计划构建、
session 构造串成一条调用链。

流程 (来自 Task 4 brief):
1. registry.get(case.platform_id) 解析平台 (fail closed, 未知抛 UnknownPlatformError);
2. platform.inspect_case(case) 做轻量确定性工件识别, 用其产出覆盖 case.artifacts;
3. platform.seed_hypotheses(case) 产出初始假设;
4. planner.build_initial_plan(case, descriptor, hypotheses, budget) 组装计划;
5. 构造 DiagnosisSession。

budget 优先级: 显式 create_diagnosis_session(..., budget=N) 参数 >
case.metadata["budget"] > _DEFAULT_BUDGET。不传 budget 时行为不变。
"""

from diagnose.catalog import EvidenceCatalog
from diagnose.model import DiagnosisCase
from diagnose.planner import DiagnosisPlanner
from diagnose.registry import PlatformRegistry
from diagnose.session import DiagnosisSession

_DEFAULT_BUDGET = 10
_BUDGET_META_KEY = "budget"


def create_diagnosis_session(
    case: DiagnosisCase,
    registry: PlatformRegistry,
    budget: int | None = None,
) -> DiagnosisSession:
    """公共入口: 解析平台 -> inspect_case -> 构造 plan -> 构造 session。

    一期 session 不执行任何 action; 这里只完成协调所需的只读准备。

    budget 优先级: 显式 ``budget`` 参数 > ``case.metadata["budget"]`` >
    ``_DEFAULT_BUDGET``。不传 ``budget`` 时行为与历史版本一致 (走 metadata / default)。
    """
    platform = registry.get(case.platform_id)

    # inspect_case 做确定性工件识别, 产出作为 session 持有的 artifacts 视图。
    inspected_artifacts = platform.inspect_case(case)
    # 用 inspect 产出覆盖 case.artifacts (补全 size/sha256 等), 保持 case 不变性
    # 通过构造一个新 case 对象实现, 避免就地修改入参。
    resolved_case = case.model_copy(update={"artifacts": list(inspected_artifacts)})

    descriptor = platform.descriptor
    hypotheses = platform.seed_hypotheses(resolved_case)

    resolved_budget = _resolve_budget(resolved_case, explicit=budget)
    planner = DiagnosisPlanner()
    plan = planner.build_initial_plan(
        resolved_case, descriptor, hypotheses, resolved_budget
    )

    return DiagnosisSession(
        case=resolved_case,
        platform=platform,
        descriptor=descriptor,
        catalog=EvidenceCatalog(),
        hypotheses=hypotheses,
        plan=plan,
    )


def _resolve_budget(case: DiagnosisCase, explicit: int | None = None) -> int:
    """解析 budget, 优先级: 显式参数 > case.metadata["budget"] > _DEFAULT_BUDGET。

    每一级都施加同样的合法性校验, 避免非法值进入 plan:
    - bool 一律排除 (bool 是 int 子类, 显式跳过, 避免 True 被当作 1);
    - 非正 int 一律按缺省处理 (避免零或负预算进入 plan)。
    三级都非法或缺省时返回 _DEFAULT_BUDGET。
    """
    for raw in (explicit, case.metadata.get(_BUDGET_META_KEY)):
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and raw > 0:
            return raw
    return _DEFAULT_BUDGET
