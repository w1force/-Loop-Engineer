"""诊断模型包

导出所有诊断领域模型和枚举。本包不依赖 core/，可被普通 Python 直接调用。
"""

# Enum
from diagnose.model.case import ArtifactKind
from diagnose.model.platform import PlatformStatus
from diagnose.model.hypothesis import EvidenceTimeBasis, HypothesisStatus, ClaimStatus
from diagnose.model.result import DiagnosisStatus

# Case & Artifact
from diagnose.model.case import ArtifactRef, DiagnosisCase

# Platform
from diagnose.model.platform import Capability, AnalysisActionSpec
from diagnose.model.platform import DiagnosticTaxonomy, DiagnosticPlatformDescriptor

# Plan
from diagnose.model.plan import AnalysisActionRequest, ActionInvocation, DiagnosisPlan

# Evidence
from diagnose.model.evidence import (
    EvidenceDraft,
    EvidenceFinding,
    EvidenceLocation,
    EvidenceRecord,
    FindingOutcome,
)

# Hypothesis
from diagnose.model.hypothesis import Hypothesis, Claim, ClaimProposal

# Review
from diagnose.model.review import (
    DiagnosisReview,
    DiagnosisReviewPolicy,
    ProposalReview,
    ProposalReviewVerdict,
    ReviewCycle,
    ReviewDecision,
    ReviewFinding,
    ReviewMode,
    ReviewSeverity,
    UnresolvedReviewAction,
)

# Result
from diagnose.model.result import DiagnosisResult

__all__ = [
    # Enum
    "PlatformStatus",
    "ArtifactKind",
    "HypothesisStatus",
    "ClaimStatus",
    "DiagnosisStatus",
    "EvidenceTimeBasis",
    # Case & Artifact
    "ArtifactRef",
    "DiagnosisCase",
    # Platform
    "Capability",
    "AnalysisActionSpec",
    "DiagnosticTaxonomy",
    "DiagnosticPlatformDescriptor",
    # Plan
    "AnalysisActionRequest",
    "ActionInvocation",
    "DiagnosisPlan",
    # Evidence
    "EvidenceLocation",
    "EvidenceDraft",
    "EvidenceRecord",
    "EvidenceFinding",
    "FindingOutcome",
    # Hypothesis
    "Hypothesis",
    "Claim",
    "ClaimProposal",
    # Review
    "DiagnosisReview",
    "DiagnosisReviewPolicy",
    "ProposalReview",
    "ProposalReviewVerdict",
    "ReviewCycle",
    "ReviewDecision",
    "ReviewFinding",
    "ReviewMode",
    "ReviewSeverity",
    "UnresolvedReviewAction",
    # Result
    "DiagnosisResult",
]
