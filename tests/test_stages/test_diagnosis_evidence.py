"""Diagnosis evidence plan freezing, exact lookup, fallback correlation and packing."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.contracts.evidence import (
    CorrelationIdentity,
    EvidenceLogPlanProposal,
    EvidenceMatchStatus,
    PrimarySignal,
    ProposedEvidenceLog,
)
from core.contracts.incident import ArtifactReference
from core.observability import LocalObservabilityStore
from core.stages.diagnosis import (
    DiagnosisEvidenceRetriever,
    EvidencePlanFreezer,
    EvidencePlanningError,
)


DIGEST = "a" * 64
TS = 1_000_000_000_000


def _attr(key: str, value: str):
    return {"key": key, "value": {"stringValue": value}}


def _ingest(
    store: LocalObservabilityStore,
    *,
    body: str,
    timestamp_ns: int = TS,
    request_id: str | None = None,
    erp: str | None = None,
    event_name: str | None = None,
) -> None:
    attributes = [_attr("logger.name", "order.chain")]
    if request_id:
        attributes.append(_attr("request.id", request_id))
    if erp:
        attributes.append(_attr("erp", erp))
    if event_name:
        attributes.append(_attr("event.name", event_name))
    inserted = store.ingest_otlp_logs(
        {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            _attr("service.name", "order-api"),
                            _attr("deployment.environment.name", "prod"),
                            _attr("deployment.version", "v1"),
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "orders"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(timestamp_ns),
                                    "observedTimeUnixNano": str(timestamp_ns),
                                    "severityText": "ERROR",
                                    "body": {"stringValue": body},
                                    "attributes": attributes,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )
    assert inserted == 1


def _primary(*, request_id: str | None = None) -> PrimarySignal:
    return PrimarySignal(
        event_id="event-primary",
        artifact=ArtifactReference(uri="signal://source#1", sha256=DIGEST),
        observed_at="2026-01-01T00:00:00Z",
        timestamp_ns=TS,
        service="order-api",
        environment="prod",
        deployment_version="v1",
        correlation=CorrelationIdentity(request_id=request_id),
        event_name="request_failed",
        error_type="TimeoutError",
        message="checkout timed out",
    )


def _proposal(*items: ProposedEvidenceLog) -> EvidenceLogPlanProposal:
    return EvidenceLogPlanProposal(evidence_logs=items)


def _item(
    *,
    evidence_id: str,
    line: int,
    template: str,
    priority: int = 90,
    extract: bool = False,
) -> ProposedEvidenceLog:
    return ProposedEvidenceLog(
        evidence_id=evidence_id,
        priority=priority,
        question="what happened on this call-chain edge?",
        reason="source emits the evidence needed to answer the question",
        source_path="service.py",
        start_line=line,
        logger="order.chain",
        level="ERROR",
        template=template,
        extract_fields=("request_id",) if extract else (),
        extract_patterns=(
            {"request_id": r"req=(?P<value>req-[0-9]+)"} if extract else {}
        ),
    )


def _freeze(
    tmp_path: Path, primary: PrimarySignal, *items: ProposedEvidenceLog
):
    return EvidencePlanFreezer().freeze(
        _proposal(*items),
        primary_signal=primary,
        control_workspace=tmp_path,
        control_ref="rev-control",
        planner_skill_digest="b" * 64,
    )


def test_freezer_rejects_agent_template_not_present_in_source(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        'logger.error(f"real message {request_id}")\n', encoding="utf-8"
    )
    with pytest.raises(EvidencePlanningError, match="absent from source"):
        _freeze(
            tmp_path,
            _primary(),
            _item(evidence_id="made-up", line=1, template="invented success message"),
        )


def test_known_id_uses_exact_lookup_without_bm25(tmp_path: Path) -> None:
    class ExactOnlyStore(LocalObservabilityStore):
        def search_logs_bm25(self, **kwargs):  # pragma: no cover - must stay unused
            raise AssertionError("BM25 must not run when a correlation ID is known")

    (tmp_path / "service.py").write_text(
        'logger.error(f"MCP call timed out req={request_id}")\n', encoding="utf-8"
    )
    store = ExactOnlyStore(tmp_path / "observability.db")
    _ingest(store, body="MCP call timed out req=req-7", request_id="req-7")
    primary = _primary(request_id="req-7")
    plan = _freeze(
        tmp_path,
        primary,
        _item(
            evidence_id="timeout",
            line=1,
            template="MCP call timed out req={request_id}",
        ),
    )

    bundle = DiagnosisEvidenceRetriever(store).retrieve(
        primary_signal=primary, plan=plan
    )

    assert bundle.correlation_proof is None
    assert bundle.results[0].status is EvidenceMatchStatus.EXACT_MATCH
    assert bundle.results[0].observations[0].body.endswith("req=req-7")


def test_missing_id_uses_bm25_anchor_then_switches_to_exact_lookup(
    tmp_path: Path,
) -> None:
    (tmp_path / "service.py").write_text(
        "\n".join(
            [
                'logger.error(f"received request req={request_id}")',
                'logger.error(f"dependency timed out req={request_id}")',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    store = LocalObservabilityStore(tmp_path / "observability.db")
    _ingest(store, body="received request req=req-42", timestamp_ns=TS - 10)
    _ingest(
        store,
        body="dependency timed out req=req-42",
        timestamp_ns=TS + 10,
        request_id="req-42",
    )
    primary = _primary()
    plan = _freeze(
        tmp_path,
        primary,
        _item(
            evidence_id="request-anchor",
            line=1,
            template="received request req={request_id}",
            priority=100,
            extract=True,
        ),
        _item(
            evidence_id="dependency",
            line=2,
            template="dependency timed out req={request_id}",
            priority=90,
        ),
    )

    bundle = DiagnosisEvidenceRetriever(store).retrieve(
        primary_signal=primary, plan=plan
    )

    assert bundle.correlation_proof is not None
    assert bundle.correlation_proof.identity.request_id == "req-42"
    assert all(
        result.status is EvidenceMatchStatus.EXACT_MATCH for result in bundle.results
    )


def test_close_fuzzy_id_candidates_are_reported_as_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        'logger.error(f"received request req={request_id}")\n', encoding="utf-8"
    )
    store = LocalObservabilityStore(tmp_path / "observability.db")
    _ingest(store, body="received request req=req-1")
    _ingest(store, body="received request req=req-2")
    primary = _primary()
    plan = _freeze(
        tmp_path,
        primary,
        _item(
            evidence_id="request-anchor",
            line=1,
            template="received request req={request_id}",
            extract=True,
        ),
    )

    bundle = DiagnosisEvidenceRetriever(store).retrieve(
        primary_signal=primary, plan=plan
    )

    assert bundle.correlation_proof is None
    assert bundle.results[0].status is EvidenceMatchStatus.CORRELATION_AMBIGUOUS


def test_weak_bm25_or_hit_cannot_bypass_source_template(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        'logger.error(f"dependency timed out req={request_id}")\n',
        encoding="utf-8",
    )
    store = LocalObservabilityStore(tmp_path / "observability.db")
    _ingest(store, body="dependency completed successfully", timestamp_ns=TS)
    primary = _primary()
    plan = _freeze(
        tmp_path,
        primary,
        _item(
            evidence_id="dependency-timeout",
            line=1,
            template="dependency timed out req={request_id}",
            extract=True,
        ),
    )

    bundle = DiagnosisEvidenceRetriever(store).retrieve(
        primary_signal=primary, plan=plan
    )

    assert bundle.correlation_proof is None
    assert bundle.results[0].status is EvidenceMatchStatus.NOT_FOUND
    assert bundle.results[0].observations == ()


def test_erp_is_only_a_shallow_bm25_filter_not_a_correlation_proof(
    tmp_path: Path,
) -> None:
    (tmp_path / "service.py").write_text(
        'logger.error(f"dependency timed out user={erp}")\n', encoding="utf-8"
    )
    store = LocalObservabilityStore(tmp_path / "observability.db")
    _ingest(store, body="dependency timed out user=alice", erp="alice")
    _ingest(store, body="dependency timed out user=bob", erp="bob")
    primary = _primary().model_copy(
        update={"correlation": CorrelationIdentity(erp="alice")}
    )
    plan = _freeze(
        tmp_path,
        primary,
        _item(
            evidence_id="dependency-timeout",
            line=1,
            template="dependency timed out user={erp}",
        ),
    )

    bundle = DiagnosisEvidenceRetriever(store).retrieve(
        primary_signal=primary, plan=plan
    )

    assert bundle.correlation_proof is None
    assert bundle.results[0].status is EvidenceMatchStatus.FUZZY_FALLBACK
    assert [item.extracted_identity.erp for item in bundle.results[0].observations] == [
        "alice"
    ]


def test_incomplete_collection_never_turns_absence_into_not_found(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        'logger.error(f"MCP call timed out req={request_id}")\n', encoding="utf-8"
    )
    store = LocalObservabilityStore(tmp_path / "observability.db")
    primary = _primary(request_id="req-404")
    plan = _freeze(
        tmp_path,
        primary,
        _item(
            evidence_id="timeout",
            line=1,
            template="MCP call timed out req={request_id}",
        ),
    )

    bundle = DiagnosisEvidenceRetriever(store).retrieve(
        primary_signal=primary,
        plan=plan,
        collection_complete=False,
    )

    assert bundle.collection_complete is False
    assert bundle.results[0].status is EvidenceMatchStatus.COLLECTION_INCOMPLETE
    assert "absence is not evidence" in (bundle.results[0].reason or "")


def test_priority_context_keeps_first_five_and_caps_later_logs(tmp_path: Path) -> None:
    source_lines = [
        f'logger.error(f"evidence-{index} {{details}}")' for index in range(1, 7)
    ]
    (tmp_path / "service.py").write_text(
        "\n".join(source_lines) + "\n", encoding="utf-8"
    )
    store = LocalObservabilityStore(tmp_path / "observability.db")
    for index in range(1, 6):
        _ingest(
            store,
            body=f"evidence-{index} short",
            timestamp_ns=TS + index,
            request_id="req-7",
        )
    _ingest(
        store,
        body="evidence-6 " + ("word " * 2_000) + "Caused by: root",
        timestamp_ns=TS + 6,
        request_id="req-7",
    )
    primary = _primary(request_id="req-7")
    plan = _freeze(
        tmp_path,
        primary,
        *(
            _item(
                evidence_id=f"evidence-{index}",
                line=index,
                template=f"evidence-{index} {{details}}",
                priority=101 - index,
            )
            for index in range(1, 7)
        ),
    )

    bundle = DiagnosisEvidenceRetriever(store).retrieve(
        primary_signal=primary, plan=plan
    )

    assert all(
        not result.observations[0].truncated for result in bundle.results[:5]
    )
    assert bundle.results[5].observations[0].truncated is True
    tail = bundle.results[5].observations[0]
    assert tail.included_ranges
    original = "evidence-6 " + ("word " * 2_000) + "Caused by: root"
    assert tail.body == "\n…[truncated]…\n".join(
        original[start:end] for start, end in tail.included_ranges
    )
    assert tail.included_ranges == tuple(sorted(tail.included_ranges))
    assert len(tail.body.encode("utf-8")) <= 1_024
    assert bundle.total_tokens <= 24_576


def test_exhausted_context_budget_is_explicit_not_empty_exact_match(
    tmp_path: Path,
) -> None:
    (tmp_path / "service.py").write_text(
        "\n".join(
            [
                'logger.error(f"first evidence {details}")',
                'logger.error(f"second evidence {details}")',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    store = LocalObservabilityStore(tmp_path / "observability.db")
    _ingest(store, body="first evidence x", request_id="req-7")
    _ingest(store, body="second evidence y", timestamp_ns=TS + 1, request_id="req-7")
    primary = _primary(request_id="req-7")
    plan = _freeze(
        tmp_path,
        primary,
        _item(evidence_id="first", line=1, template="first evidence {details}"),
        _item(evidence_id="second", line=2, template="second evidence {details}"),
    )

    bundle = DiagnosisEvidenceRetriever(
        store, max_total_tokens=64, max_observations=1
    ).retrieve(primary_signal=primary, plan=plan)

    assert bundle.results[0].observations
    assert bundle.results[1].status is EvidenceMatchStatus.CONTEXT_OMITTED
    assert bundle.results[1].observations == ()
    assert "budget" in (bundle.results[1].reason or "")
