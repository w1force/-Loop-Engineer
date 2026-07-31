"""ClaimProposalValidator 的语言无关确定性校验。"""
from diagnose.catalog import EvidenceCatalog
from diagnose.model import (
    ArtifactKind,
    ArtifactRef,
    ClaimProposal,
    DiagnosisCase,
    DiagnosticTaxonomy,
    EvidenceDraft,
)
from diagnose.validation import ClaimProposalValidator, ValidationIssue


def _case() -> DiagnosisCase:
    return DiagnosisCase(
        id="c", platform_id="p", root_dir="/tmp",
        artifacts=[ArtifactRef(id="a1", kind=ArtifactKind.LOG, path="x")],
    )


def _catalog() -> EvidenceCatalog:
    catalog = EvidenceCatalog()
    catalog.append([EvidenceDraft(dedup_key="d", platform_id="p", artifact_ids=["a1"], analyzer_id="a", summary="s")])
    return catalog


def _proposal(**changes: object) -> ClaimProposal:
    values: dict[str, object] = {
        "id": "p1",
        "category": "cat",
        "statement": "The result wording is irrelevant.",
        "evidence_ids": ["EVD-0001"],
        "artifact_ids": ["a1"],
    }
    values.update(changes)
    return ClaimProposal.model_validate(values)


def _codes(proposal: ClaimProposal) -> set[str]:
    return {issue.code for issue in ClaimProposalValidator().validate(
        proposal, catalog=_catalog(), case=_case(), taxonomy=DiagnosticTaxonomy(categories={"cat": "test"})
    )}


def test_validation_issue_is_structured():
    issue = ValidationIssue(code="x", message="y")
    assert issue.blocking is True


def test_valid_proposal_does_not_depend_on_statement_wording():
    assert _codes(_proposal(statement="no deadlock exists")) == set()


def test_missing_evidence_is_rejected():
    assert "missing_evidence" in _codes(_proposal(evidence_ids=[]))


def test_unknown_evidence_is_rejected():
    assert "unknown_evidence" in _codes(_proposal(evidence_ids=["EVD-9999"]))


def test_unknown_category_and_artifact_are_rejected():
    assert "unknown_category" in _codes(_proposal(category="other"))
    assert "unknown_artifact" in _codes(_proposal(artifact_ids=["other"]))


def test_duplicate_ids_and_artifact_evidence_mismatch_are_rejected():
    assert "duplicate_evidence" in _codes(_proposal(evidence_ids=["EVD-0001", "EVD-0001"]))
    assert "duplicate_artifact" in _codes(_proposal(artifact_ids=["a1", "a1"]))
    assert "artifact_evidence_mismatch" in _codes(_proposal(artifact_ids=[]))
