"""Java/JVM 结构化 claim proposal policy。"""
from diagnose.model import (
    ClaimProposal,
    EvidenceFinding,
    EvidenceRecord,
    EvidenceTimeBasis,
    FindingOutcome,
)
from diagnose.platform_impl.java_jvm.platform import JavaJvmDiagnosticPlatform

_PLATFORM = JavaJvmDiagnosticPlatform()


def _evidence(
    kind: str,
    outcome: FindingOutcome = FindingOutcome.PRESENT,
) -> EvidenceRecord:
    return EvidenceRecord(
        id="EVD-0001",
        dedup_key="k",
        platform_id="java-jvm",
        artifact_ids=["a"],
        analyzer_id="tda",
        summary="display text",
        finding=EvidenceFinding(kind=kind, outcome=outcome, scope="test"),
    )


def _proposal(category: str, time_basis=EvidenceTimeBasis.POINT_IN_TIME) -> ClaimProposal:
    return ClaimProposal(id="p", category=category, statement="This wording must not change policy.", evidence_ids=["EVD-0001"], artifact_ids=["a"], time_basis=time_basis)


def _codes(proposal, evidence):
    return {issue.code for issue in _PLATFORM.validate_claim_proposal(proposal, [evidence])}


def test_deadlock_requires_present_cycle_not_statement_text():
    assert _codes(
        _proposal("deadlock"),
        _evidence("deadlock_cycle", FindingOutcome.ABSENT),
    ) == {"deadlock_cycle_required"}
    assert _codes(_proposal("deadlock"), _evidence("deadlock_cycle")) == set()


def test_lock_contention_is_distinct_from_deadlock():
    assert _codes(_proposal("lock_contention"), _evidence("monitor_contention")) == set()


def test_single_snapshot_cannot_validate_cpu_hotspot():
    assert _codes(_proposal("cpu_hotspot"), _evidence("repeated_hot_stack")) == {"cpu_duration_evidence_required"}


def test_interval_cpu_profile_and_multi_snapshot_heap_growth_are_allowed():
    assert _codes(_proposal("cpu_hotspot", EvidenceTimeBasis.INTERVAL_PROFILE), _evidence("cpu_profile")) == set()
    assert _codes(_proposal("heap_leak", EvidenceTimeBasis.MULTI_SNAPSHOT), _evidence("heap_growth")) == set()


def test_retention_does_not_imply_heap_leak():
    assert _codes(_proposal("memory_retention"), _evidence("dominator")) == set()
    assert _codes(_proposal("heap_leak", EvidenceTimeBasis.POINT_IN_TIME), _evidence("dominator")) == {"heap_growth_over_time_required"}


def test_rejection_explains_required_and_observed_finding():
    issues = _PLATFORM.validate_claim_proposal(
        _proposal("lock_contention"),
        [_evidence("monitor_contention", FindingOutcome.UNKNOWN)],
    )

    assert len(issues) == 1
    assert "finding=monitor_contention/present" in issues[0].message
    assert "monitor_contention/unknown (scope=test)" in issues[0].message
    assert "hypothesis" in issues[0].message
    assert "INCONCLUSIVE" in issues[0].message


def test_temporal_rejection_reports_observed_time_basis():
    issues = _PLATFORM.validate_claim_proposal(
        _proposal("heap_leak", EvidenceTimeBasis.POINT_IN_TIME),
        [_evidence("heap_growth")],
    )

    assert len(issues) == 1
    assert "time_basis in [multi_snapshot, event_sequence]" in issues[0].message
    assert "observed time_basis=point_in_time" in issues[0].message
