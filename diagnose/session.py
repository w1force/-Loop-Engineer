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

import hashlib
import json
from pathlib import Path
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
    Claim,
    ClaimProposal,
    ClaimStatus,
    DiagnosisReview,
    EvidenceDraft,
    EvidenceFinding,
    EvidenceLocation,
    EvidenceRecord,
    FindingOutcome,
    Hypothesis,
    HypothesisStatus,
    PlatformStatus,
    ProposalReviewVerdict,
    ReviewCycle,
    ReviewDecision,
    UnresolvedReviewAction,
)
from diagnose.errors import InvalidReviewError, StaleReviewError
from diagnose.platform import DiagnosticPlatform
from diagnose.validation import ClaimProposalValidator, ValidationIssue

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
        self.validated_claims: list[Claim] = []
        self.unvalidated_claims: list[Claim] = []
        self.claim_proposals: dict[str, ClaimProposal] = {}
        self.review_history: list[ReviewCycle] = []
        self.revision: int = 0
        self.review_requested_revision: int | None = None
        self.review_complete: bool = False
        self.workflow_incomplete_reason: str | None = None
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
            validated_claims=list(self.validated_claims),
            unvalidated_claims=list(self.unvalidated_claims),
            claim_proposals=list(self.claim_proposals.values()),
            missing_capabilities=missing,
            follow_up_questions=follow_ups,
            review_history=list(self.review_history),
            review_complete=self.review_complete,
            workflow_incomplete_reason=self.workflow_incomplete_reason,
        )

    def get_context(self) -> dict[str, Any]:
        """返回 Agent 诊断决策所需的受控上下文。

        artifacts 每条额外带 absolute_path: 部分 MCP 工具 (TDA parse_log /
        open_heap_dump) 需要绝对路径, 而 ArtifactRef.path 是相对 root_dir 的相对路径。
        absolute_path 属 case-specific 可变信息, 放这里 (Agent 经 GetDiagnosisContext 取),
        不进被 <system-reminder> 包的平台 guidance 稳定块 —— 否则具体本地地址会污染
        稳定前缀的 cache 命中。
        """
        root = Path(self.case.root_dir).resolve()
        artifacts = []
        for artifact in self.case.artifacts:
            dump = artifact.model_dump(mode="json")
            dump["absolute_path"] = str((root / artifact.path).resolve())
            artifacts.append(dump)
        return {
            "case_id": self.case.id,
            "platform_id": self.descriptor.id,
            "platform_status": self.descriptor.status.value,
            "taxonomy": self.descriptor.taxonomy.categories,
            "artifacts": artifacts,
            "hypotheses": [hypothesis.model_dump(mode="json") for hypothesis in self.hypotheses],
            "evidence": [record.model_dump(mode="json") for record in self.catalog.all()],
            "claim_proposals": [proposal.model_dump(mode="json") for proposal in self.claim_proposals.values()],
            "revision": self.revision,
            "review_history": [cycle.model_dump(mode="json") for cycle in self.review_history],
        }

    def capture_evidence(
        self,
        *,
        artifact_ids: list[str],
        analyzer_id: str,
        summary: str,
        finding: EvidenceFinding | None = None,
        locations: list[EvidenceLocation] | None = None,
        data: dict[str, Any] | None = None,
        confidence: float | None = None,
    ) -> EvidenceRecord:
        """登记 Agent 选择的诊断证据。

        Agent 应基于本轮实际读取的工具输出填写 ``analyzer_id`` 和 ``data``。
        一期不在 core 记录工具调用，因此 session 只负责 case artifact 边界、
        EvidenceCatalog 幂等和后续 claim 引用校验。
        """
        known_artifacts = {artifact.id for artifact in self.case.artifacts}
        unknown_artifacts = sorted(set(artifact_ids) - known_artifacts)
        if unknown_artifacts:
            raise ValueError(f"unknown case artifact ids: {unknown_artifacts}")
        if not artifact_ids:
            raise ValueError("at least one case artifact id is required")
        signature = json.dumps(
            {
                "artifact_ids": sorted(artifact_ids),
                "analyzer_id": analyzer_id,
                "summary": summary,
                "finding": finding.model_dump(mode="json") if finding is not None else None,
                "locations": [location.model_dump(mode="json") for location in locations or []],
                "data": data or {},
            },
            sort_keys=True,
            default=str,
        )
        draft = EvidenceDraft(
            dedup_key=f"agent-capture:{hashlib.sha256(signature.encode()).hexdigest()}",
            platform_id=self.descriptor.id,
            artifact_ids=list(artifact_ids),
            analyzer_id=analyzer_id,
            summary=summary,
            finding=finding,
            locations=locations or [],
            data=data or {},
            confidence=confidence,
        )
        before = len(self.catalog.all())
        record = self.catalog.append([draft])[0]
        if len(self.catalog.all()) != before:
            self._touch()
        return record

    def update_hypothesis(self, hypothesis: Hypothesis) -> Hypothesis:
        """更新已存在假设；禁止 Agent 用未知 ID 悄然新增状态。"""
        issues = self._validate_hypothesis(hypothesis, terminal_required=False)
        if issues:
            raise ValueError("; ".join(issues))
        for index, current in enumerate(self.hypotheses):
            if current.id == hypothesis.id:
                if current == hypothesis:
                    return current
                self.hypotheses[index] = hypothesis
                self._touch()
                return hypothesis
        raise ValueError(f"unknown hypothesis id: {hypothesis.id}")

    def submit_claim_proposal(self, proposal: ClaimProposal) -> ClaimProposal:
        """登记正向结论提案；只有 controller 能在审查后生成最终 Claim。"""
        issues = self.validate_claim_proposal(proposal)
        if issues:
            raise ValueError("; ".join(issue.message for issue in issues if issue.blocking))
        current = self.claim_proposals.get(proposal.id)
        if current is not None and current == proposal:
            return current
        self.claim_proposals[proposal.id] = proposal
        self._touch()
        return proposal

    def validate_claim_proposal(self, proposal: ClaimProposal) -> list[ValidationIssue]:
        issues = ClaimProposalValidator().validate(
            proposal,
            catalog=self.catalog,
            case=self.case,
            taxonomy=self.descriptor.taxonomy,
        )
        referenced = [
            record for record in self.catalog.all() if record.id in proposal.evidence_ids
        ]
        issues.extend(self.platform.validate_claim_proposal(proposal, referenced))
        return issues

    def request_review(self) -> tuple[bool, list[str]]:
        """冻结当前 revision 作为 reviewer 的只读审查目标。"""
        reasons = self._pre_review_reasons()
        if reasons:
            return False, reasons
        self.review_requested_revision = self.revision
        self.review_complete = False
        return True, []

    def get_review_context(self) -> dict[str, Any]:
        """返回 reviewer 所需的结构化只读快照。"""
        return {
            **self.get_context(),
            "review_requested_revision": self.review_requested_revision,
            "deterministic_issues": [
                issue.model_dump(mode="json")
                for proposal in self.claim_proposals.values()
                for issue in self.validate_claim_proposal(proposal)
            ],
        }

    def submit_review(self, review: DiagnosisReview, *, round_index: int) -> DiagnosisReview:
        """校验并登记一轮 revision-bound 审查。"""
        if self.review_requested_revision is None:
            raise InvalidReviewError("no review has been requested")
        if review.reviewed_revision != self.review_requested_revision or review.reviewed_revision != self.revision:
            raise StaleReviewError(
                f"review revision {review.reviewed_revision} does not match current revision {self.revision}"
            )
        if any(cycle.round_index == round_index for cycle in self.review_history):
            raise InvalidReviewError(f"review round already submitted: {round_index}")
        expected = set(self.claim_proposals)
        submitted = [item.proposal_id for item in review.proposal_reviews]
        if len(submitted) != len(set(submitted)) or set(submitted) != expected:
            raise InvalidReviewError(
                f"proposal reviews must cover exactly {sorted(expected)}, got {sorted(submitted)}"
            )
        finding_codes = [finding.code for finding in review.findings]
        if len(finding_codes) != len(set(finding_codes)):
            raise InvalidReviewError("review finding codes must be unique within a review")
        known_codes = set(finding_codes)
        unknown_codes = sorted({
            code
            for proposal_review in review.proposal_reviews
            for code in proposal_review.finding_codes
            if code not in known_codes
        })
        if unknown_codes:
            raise InvalidReviewError(
                f"proposal reviews reference unknown finding codes: {unknown_codes}"
            )
        proposal_targets = {
            finding.target_id
            for finding in review.findings
            if finding.target_type == "claim_proposal" and finding.target_id is not None
        }
        unknown_targets = sorted(proposal_targets - expected)
        if unknown_targets:
            raise InvalidReviewError(
                f"review findings target unknown claim proposals: {unknown_targets}"
            )
        known_hypotheses = {hypothesis.id for hypothesis in self.hypotheses}
        known_evidence = {record.id for record in self.catalog.all()}
        for finding in review.findings:
            if finding.target_type == "hypothesis" and finding.target_id not in known_hypotheses:
                raise InvalidReviewError(
                    f"review finding targets unknown hypothesis: {finding.target_id!r}"
                )
            if finding.target_type == "evidence" and finding.target_id not in known_evidence:
                raise InvalidReviewError(
                    f"review finding targets unknown evidence: {finding.target_id!r}"
                )
            if finding.target_type == "diagnosis" and finding.target_id is not None:
                raise InvalidReviewError("diagnosis-level review finding must not set target_id")
        findings_by_code = {finding.code: finding for finding in review.findings}
        for proposal_review in review.proposal_reviews:
            mismatched = sorted(
                code
                for code in proposal_review.finding_codes
                if findings_by_code[code].target_type == "claim_proposal"
                and findings_by_code[code].target_id != proposal_review.proposal_id
            )
            if mismatched:
                raise InvalidReviewError(
                    f"proposal review references findings for another proposal: {mismatched}"
                )
        unknown = sorted({eid for finding in review.findings for eid in finding.evidence_ids} - known_evidence)
        if unknown:
            raise InvalidReviewError(f"review findings reference unknown evidence: {unknown}")
        self.review_history.append(ReviewCycle(round_index=round_index, review=review))
        return review

    def apply_review(
        self,
        review: DiagnosisReview,
        *,
        unresolved_action: UnresolvedReviewAction,
        finalize_unresolved: bool,
    ) -> None:
        """由 controller 应用审查；reviewer 本身无权写最终 claims。"""
        if review.reviewed_revision != self.revision:
            raise StaleReviewError("cannot apply a stale review")
        if review.decision == ReviewDecision.REVISION_REQUIRED and not finalize_unresolved:
            return
        self.validated_claims.clear()
        self.unvalidated_claims.clear()
        by_id = {item.proposal_id: item for item in review.proposal_reviews}
        for proposal in self.claim_proposals.values():
            verdict = by_id[proposal.id]
            approved = verdict.verdict == ProposalReviewVerdict.APPROVE
            claim = Claim(
                **proposal.model_dump(),
                status=ClaimStatus.VALIDATED if approved else ClaimStatus.UNVALIDATED,
                validation_note=None if approved else verdict.rationale,
            )
            (self.validated_claims if approved else self.unvalidated_claims).append(claim)
        if review.decision == ReviewDecision.REVISION_REQUIRED and unresolved_action == UnresolvedReviewAction.FAIL:
            self.workflow_incomplete_reason = "diagnosis review remained unresolved"
            self.review_complete = False
        else:
            self.review_complete = True

    def finalize_gate(self) -> tuple[bool, list[str]]:
        """检查 Agent 是否可以结束本次诊断。

        闸门检查证据状态，不要求调用某个固定 MCP 工具。这样更换 MCP 时仍保持
        有效：Agent 可以根据实际调查结果登记证据或提交不确定结论；确定性结论
        则必须能回到已登记证据。
        """
        reasons = self._pre_review_reasons()
        for claim in self.validated_claims:
            if not claim.evidence_ids:
                reasons.append(f"validated claim {claim.id!r} has no evidence references")
        if not self.review_complete:
            reasons.append("independent diagnosis review is not complete")
        if self.review_requested_revision != self.revision:
            reasons.append("latest review request is stale or missing")
        return not reasons, reasons

    def mark_incomplete(self, reason: str) -> None:
        self.workflow_incomplete_reason = reason

    def finalize_without_review(self) -> None:
        """显式关闭审查时仅产出 unvalidated claims。"""
        self.validated_claims.clear()
        self.unvalidated_claims = [
            Claim(
                **proposal.model_dump(),
                status=ClaimStatus.UNVALIDATED,
                validation_note="independent review disabled",
            )
            for proposal in self.claim_proposals.values()
        ]
        self.review_complete = False

    def _pre_review_reasons(self) -> list[str]:
        reasons: list[str] = []
        for hypothesis in self.hypotheses:
            reasons.extend(self._validate_hypothesis(hypothesis, terminal_required=True))
        for proposal in self.claim_proposals.values():
            reasons.extend(issue.message for issue in self.validate_claim_proposal(proposal) if issue.blocking)
        return reasons

    def _validate_hypothesis(self, hypothesis: Hypothesis, *, terminal_required: bool) -> list[str]:
        reasons: list[str] = []
        if hypothesis.category not in self.descriptor.taxonomy.categories:
            reasons.append(f"hypothesis {hypothesis.id!r} has unknown category {hypothesis.category!r}")
        supporting = set(hypothesis.supporting_evidence_ids)
        contradicting = set(hypothesis.contradicting_evidence_ids)
        inconclusive = set(hypothesis.inconclusive_evidence_ids)
        overlaps = sorted(
            (supporting & contradicting)
            | (supporting & inconclusive)
            | (contradicting & inconclusive)
        )
        if overlaps:
            reasons.append(
                f"hypothesis {hypothesis.id!r} assigns evidence to multiple directions: {overlaps}"
            )
        known = {record.id for record in self.catalog.all()}
        missing = sorted((supporting | contradicting | inconclusive) - known)
        if missing:
            reasons.append(f"hypothesis {hypothesis.id!r} references missing evidence: {missing}")
        unknown_directional = sorted(
            evidence_id
            for evidence_id in supporting | contradicting
            if (record := self.catalog.get(evidence_id)) is not None
            and record.finding is not None
            and record.finding.outcome == FindingOutcome.UNKNOWN
        )
        if unknown_directional:
            reasons.append(
                f"hypothesis {hypothesis.id!r} assigns finding.outcome=unknown as support or "
                f"contradiction: {unknown_directional}; use inconclusive_evidence_ids"
            )
        if hypothesis.status in (HypothesisStatus.SUPPORTED, HypothesisStatus.CONFIRMED) and not supporting:
            reasons.append(f"hypothesis {hypothesis.id!r} requires supporting evidence")
        if hypothesis.status == HypothesisStatus.CONTRADICTED and not contradicting:
            reasons.append(f"hypothesis {hypothesis.id!r} requires contradicting evidence")
        if hypothesis.status == HypothesisStatus.INCONCLUSIVE and not hypothesis.status_note:
            reasons.append(f"hypothesis {hypothesis.id!r} requires status_note when inconclusive")
        if inconclusive and hypothesis.status != HypothesisStatus.INCONCLUSIVE:
            reasons.append(
                f"hypothesis {hypothesis.id!r} may use inconclusive_evidence_ids only when "
                "status=inconclusive"
            )
        if terminal_required and hypothesis.status in (HypothesisStatus.PENDING, HypothesisStatus.SUPPORTED):
            reasons.append(f"hypothesis {hypothesis.id!r} is not terminal: {hypothesis.status.value}")
        return reasons

    def _touch(self) -> None:
        self.revision += 1
        self.review_requested_revision = None
        self.review_complete = False
        self.validated_claims.clear()
        self.unvalidated_claims.clear()
        self.workflow_incomplete_reason = None

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
