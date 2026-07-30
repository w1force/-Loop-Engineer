"""诊断模型包

导出所有诊断领域模型和枚举。本包不依赖 core/，可被普通 Python 直接调用。
"""

# Enum
from diagnose.model.case import ArtifactKind
from diagnose.model.platform import PlatformStatus
from diagnose.model.hypothesis import HypothesisStatus, ClaimStatus
from diagnose.model.result import DiagnosisStatus

# Case & Artifact
from diagnose.model.case import ArtifactRef, DiagnosisCase

# Platform
from diagnose.model.platform import Capability, AnalysisActionSpec
from diagnose.model.platform import DiagnosticTaxonomy, DiagnosticPlatformDescriptor

# Plan
from diagnose.model.plan import AnalysisActionRequest, ActionInvocation, DiagnosisPlan

# Evidence
from diagnose.model.evidence import EvidenceLocation, EvidenceDraft, EvidenceRecord

# Hypothesis
from diagnose.model.hypothesis import Hypothesis, Claim

# Result
from diagnose.model.result import DiagnosisResult

__all__ = [
    # Enum
    "PlatformStatus",
    "ArtifactKind",
    "HypothesisStatus",
    "ClaimStatus",
    "DiagnosisStatus",
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
    # Hypothesis
    "Hypothesis",
    "Claim",
    # Result
    "DiagnosisResult",
]
