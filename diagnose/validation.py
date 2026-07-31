"""语言无关的结论提案与证据引用校验。"""
from __future__ import annotations

from pydantic import BaseModel

from diagnose.catalog import EvidenceCatalog
from diagnose.model import ClaimProposal, DiagnosisCase, DiagnosticTaxonomy


class ValidationIssue(BaseModel):
    code: str
    message: str
    target_id: str | None = None
    blocking: bool = True


class ClaimProposalValidator:
    """只检查可确定的引用、分类和工件边界，不解析自然语言 statement。"""

    def validate(
        self,
        proposal: ClaimProposal,
        *,
        catalog: EvidenceCatalog,
        case: DiagnosisCase,
        taxonomy: DiagnosticTaxonomy,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if proposal.category not in taxonomy.categories:
            issues.append(self._issue("unknown_category", f"unknown claim category: {proposal.category!r}", proposal.id))
        if not proposal.evidence_ids:
            issues.append(self._issue("missing_evidence", "claim proposal has no evidence references", proposal.id))
        if len(proposal.evidence_ids) != len(set(proposal.evidence_ids)):
            issues.append(self._issue("duplicate_evidence", "claim proposal contains duplicate evidence references", proposal.id))
        missing_evidence = [eid for eid in proposal.evidence_ids if catalog.get(eid) is None]
        if missing_evidence:
            issues.append(self._issue("unknown_evidence", "claim proposal references missing evidence: " + ", ".join(missing_evidence), proposal.id))

        known_artifacts = {artifact.id for artifact in case.artifacts}
        unknown_artifacts = sorted(set(proposal.artifact_ids) - known_artifacts)
        if unknown_artifacts:
            issues.append(self._issue("unknown_artifact", "claim proposal references unknown artifacts: " + ", ".join(unknown_artifacts), proposal.id))
        if len(proposal.artifact_ids) != len(set(proposal.artifact_ids)):
            issues.append(self._issue("duplicate_artifact", "claim proposal contains duplicate artifact references", proposal.id))

        referenced_artifacts: set[str] = set()
        for evidence_id in proposal.evidence_ids:
            record = catalog.get(evidence_id)
            if record is not None:
                referenced_artifacts.update(record.artifact_ids)
        if referenced_artifacts and not referenced_artifacts.issubset(set(proposal.artifact_ids)):
            missing = sorted(referenced_artifacts - set(proposal.artifact_ids))
            required = sorted(referenced_artifacts)
            issues.append(self._issue(
                "artifact_evidence_mismatch",
                "claim proposal omits artifacts used by evidence: "
                + ", ".join(missing)
                + "; set artifact_ids to cover the referenced-evidence union: "
                + ", ".join(required),
                proposal.id,
            ))
        return issues

    @staticmethod
    def _issue(code: str, message: str, target_id: str) -> ValidationIssue:
        return ValidationIssue(code=code, message=message, target_id=target_id)
