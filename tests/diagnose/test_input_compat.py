"""嵌套 proposal/review 被 provider 序列化为 JSON string 时的兼容性。"""
import json

from diagnose.agent_tools import ProposeDiagnosisClaimInput, UpdateDiagnosisHypothesisInput
from diagnose.model import ClaimProposal, DiagnosisReview, ReviewDecision
from diagnose.review_tools import SubmitDiagnosisReviewInput

_HYP_JSON = json.dumps({"id": "h", "category": "cat", "statement": "s"})
_PROPOSAL_JSON = json.dumps({"id": "p", "category": "cat", "statement": "s", "evidence_ids": ["EVD-0001"], "artifact_ids": ["a"]})
_REVIEW_JSON = json.dumps({"reviewed_revision": 1, "decision": "approved", "proposal_reviews": [{"proposal_id": "p", "verdict": "approve", "rationale": "supported"}]})


def test_hypothesis_json_string_is_accepted():
    assert UpdateDiagnosisHypothesisInput.model_validate({"hypothesis": _HYP_JSON}).hypothesis.id == "h"


def test_proposal_json_string_and_object_are_accepted():
    assert ProposeDiagnosisClaimInput.model_validate({"proposal": _PROPOSAL_JSON}).proposal.id == "p"
    proposal = ClaimProposal.model_validate_json(_PROPOSAL_JSON)
    assert ProposeDiagnosisClaimInput.model_validate({"proposal": proposal}).proposal is proposal


def test_review_json_string_and_object_are_accepted():
    assert SubmitDiagnosisReviewInput.model_validate({"review": _REVIEW_JSON}).review.reviewed_revision == 1
    review = DiagnosisReview(reviewed_revision=1, decision=ReviewDecision.APPROVED, proposal_reviews=[])
    assert SubmitDiagnosisReviewInput.model_validate({"review": review}).review is review
