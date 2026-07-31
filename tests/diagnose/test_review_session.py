"""DiagnosisSession 的 proposal、revision 和审查放行状态机。"""
from diagnose.api import create_diagnosis_session
from diagnose.model import (
    ArtifactKind,
    ArtifactRef,
    ClaimProposal,
    DiagnosisCase,
    DiagnosisReview,
    EvidenceFinding,
    FindingOutcome,
    Hypothesis,
    HypothesisStatus,
    ProposalReview,
    ProposalReviewVerdict,
    ReviewDecision,
    UnresolvedReviewAction,
)
from diagnose.platform_impl.java_jvm.platform import JavaJvmDiagnosticPlatform
from diagnose.registry import PlatformRegistry
from diagnose.errors import InvalidReviewError


def _session(tmp_path):
    (tmp_path / "thread.txt").write_text("x")
    registry = PlatformRegistry()
    registry.register(JavaJvmDiagnosticPlatform())
    session = create_diagnosis_session(DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path), artifacts=[ArtifactRef(id="thread", kind=ArtifactKind.THREAD_SNAPSHOT, path="thread.txt")]), registry)
    evidence = session.capture_evidence(
        artifact_ids=["thread"],
        analyzer_id="tda",
        summary="contention",
        finding=EvidenceFinding(
            kind="monitor_contention",
            outcome=FindingOutcome.PRESENT,
            scope="thread_snapshot",
        ),
    )
    for hypothesis in list(session.hypotheses):
        if hypothesis.id == "seed-lock-contention":
            session.update_hypothesis(hypothesis.model_copy(update={"status": HypothesisStatus.CONFIRMED, "supporting_evidence_ids": [evidence.id]}))
        else:
            session.update_hypothesis(hypothesis.model_copy(update={"status": HypothesisStatus.INCONCLUSIVE, "status_note": "not enough evidence"}))
    proposal = session.submit_claim_proposal(ClaimProposal(id="p", category="lock_contention", statement="workers contend", evidence_ids=[evidence.id], artifact_ids=["thread"]))
    return session, proposal


def test_review_approval_promotes_only_after_matching_revision(tmp_path):
    session, proposal = _session(tmp_path)
    ok, reasons = session.request_review()
    assert ok, reasons
    review = DiagnosisReview(reviewed_revision=session.revision, decision=ReviewDecision.APPROVED, proposal_reviews=[ProposalReview(proposal_id=proposal.id, verdict=ProposalReviewVerdict.APPROVE, rationale="supported")])
    session.submit_review(review, round_index=0)
    session.apply_review(review, unresolved_action=UnresolvedReviewAction.DOWNGRADE, finalize_unresolved=False)
    assert [claim.id for claim in session.validated_claims] == ["p"]
    assert session.finalize_gate() == (True, [])


def test_mutation_invalidates_previous_review(tmp_path):
    session, proposal = _session(tmp_path)
    assert session.request_review()[0]
    old_revision = session.revision
    session.submit_review(DiagnosisReview(reviewed_revision=old_revision, decision=ReviewDecision.APPROVED, proposal_reviews=[ProposalReview(proposal_id=proposal.id, verdict=ProposalReviewVerdict.APPROVE, rationale="yes")]), round_index=0)
    session.capture_evidence(
        artifact_ids=["thread"],
        analyzer_id="tda",
        summary="more",
        finding=EvidenceFinding(
            kind="monitor_contention",
            outcome=FindingOutcome.PRESENT,
            scope="thread_snapshot",
        ),
    )
    assert session.revision > old_revision
    assert session.review_complete is False


def test_confirmed_hypothesis_cannot_use_fake_evidence(tmp_path):
    session, _ = _session(tmp_path)
    hypothesis = next(item for item in session.hypotheses if item.id == "seed-lock-contention")
    try:
        session.update_hypothesis(hypothesis.model_copy(update={"supporting_evidence_ids": ["EVD-9999"]}))
    except ValueError as error:
        assert "missing evidence" in str(error)
    else:
        raise AssertionError("fake evidence must be rejected")


def test_inconclusive_hypothesis_can_reference_unknown_scope_evidence(tmp_path):
    session, _ = _session(tmp_path)
    hypothesis = next(item for item in session.hypotheses if item.id == "seed-lock-contention")
    updated = session.update_hypothesis(
        hypothesis.model_copy(
            update={
                "status": HypothesisStatus.INCONCLUSIVE,
                "status_note": "single snapshot does not establish duration",
                "supporting_evidence_ids": [],
                "inconclusive_evidence_ids": ["EVD-0001"],
            }
        )
    )
    assert updated.inconclusive_evidence_ids == ["EVD-0001"]


def test_hypothesis_evidence_cannot_be_assigned_to_multiple_directions(tmp_path):
    session, _ = _session(tmp_path)
    hypothesis = next(item for item in session.hypotheses if item.id == "seed-lock-contention")
    try:
        session.update_hypothesis(
            hypothesis.model_copy(
                update={
                    "status": HypothesisStatus.INCONCLUSIVE,
                    "status_note": "ambiguous",
                    "supporting_evidence_ids": ["EVD-0001"],
                    "inconclusive_evidence_ids": ["EVD-0001"],
                }
            )
        )
    except ValueError as error:
        assert "multiple directions" in str(error)
    else:
        raise AssertionError("evidence must not be assigned to support and inconclusive directions")


def test_unknown_finding_cannot_be_used_as_contradicting_evidence(tmp_path):
    session, _ = _session(tmp_path)
    unknown = session.capture_evidence(
        artifact_ids=["thread"],
        analyzer_id="tda",
        summary="one snapshot cannot establish duration",
        finding=EvidenceFinding(
            kind="repeated_hot_stack",
            outcome=FindingOutcome.UNKNOWN,
            scope="thread_snapshot",
        ),
    )
    hypothesis = next(item for item in session.hypotheses if item.id == "seed-lock-contention")
    try:
        session.update_hypothesis(
            hypothesis.model_copy(
                update={
                    "status": HypothesisStatus.INCONCLUSIVE,
                    "status_note": "insufficient duration evidence",
                    "supporting_evidence_ids": [],
                    "contradicting_evidence_ids": [unknown.id],
                }
            )
        )
    except ValueError as error:
        assert "finding.outcome=unknown" in str(error)
        assert "inconclusive_evidence_ids" in str(error)
    else:
        raise AssertionError("unknown finding must not be treated as contradictory")


def test_review_rejects_unknown_finding_code_reference(tmp_path):
    session, proposal = _session(tmp_path)
    assert session.request_review()[0]
    review = DiagnosisReview(
        reviewed_revision=session.revision,
        decision=ReviewDecision.APPROVED,
        proposal_reviews=[ProposalReview(
            proposal_id=proposal.id,
            verdict=ProposalReviewVerdict.APPROVE,
            rationale="supported",
            finding_codes=["missing-code"],
        )],
    )
    try:
        session.submit_review(review, round_index=0)
    except InvalidReviewError as error:
        assert "unknown finding codes" in str(error)
    else:
        raise AssertionError("unknown finding code must be rejected")
