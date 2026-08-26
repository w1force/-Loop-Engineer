"""FailureRouter invariants — the core "only REPAIR consumes a round" contract."""

from __future__ import annotations

from core.contracts.failure import (
    FailureOwner,
    FailureRecord,
    NextAction,
    Retryability,
    StageName,
)
from core.orchestrator.router import (
    FailureRouter,
    classify_replay_failures,
    classify_exception,
)


def _rec(owner: FailureOwner, retry: Retryability = Retryability.NONE) -> FailureRecord:
    return FailureRecord(
        code="c", stage=StageName.VERIFICATION, owner=owner, retryability=retry, summary="x"
    )


def test_repair_rejection_consumes_a_round_until_budget():
    router = FailureRouter(max_repair_rounds=3)
    d = router.route((_rec(FailureOwner.REPAIR),), repair_rounds_used=0)
    assert d.next_action is NextAction.NEW_REPAIR_ROUND and d.consumes_repair_cycle
    d = router.route((_rec(FailureOwner.REPAIR),), repair_rounds_used=3)
    assert d.next_action is NextAction.ESCALATE and not d.consumes_repair_cycle


def test_infrastructure_retries_without_consuming_a_round():
    router = FailureRouter(max_infra_retries=2)
    d = router.route(
        (_rec(FailureOwner.INFRASTRUCTURE, Retryability.SAME_STAGE),),
        repair_rounds_used=1,
        infra_retries_used=0,
    )
    assert d.next_action is NextAction.RETRY_STAGE
    assert not d.consumes_repair_cycle
    d = router.route(
        (_rec(FailureOwner.INFRASTRUCTURE, Retryability.SAME_STAGE),),
        repair_rounds_used=1,
        infra_retries_used=2,
    )
    assert d.next_action is NextAction.ESCALATE


def test_integrity_is_immediate_invalidation():
    d = FailureRouter().route((_rec(FailureOwner.INTEGRITY),), repair_rounds_used=0)
    assert d.next_action is NextAction.INVALIDATE_RUN and d.escalate


def test_policy_blocks_or_replans():
    r = FailureRouter()
    assert r.route((_rec(FailureOwner.POLICY),), repair_rounds_used=0).next_action is (
        NextAction.ESCALATE
    )
    assert r.route(
        (_rec(FailureOwner.POLICY, Retryability.SAME_STAGE),), repair_rounds_used=0
    ).next_action is NextAction.REPLAN_SAME_CANDIDATE


def test_diagnosis_rediagnoses():
    d = FailureRouter().route((_rec(FailureOwner.DIAGNOSIS),), repair_rounds_used=0)
    assert d.next_action is NextAction.REDIAGNOSE


def test_governing_priority_prefers_blocking_over_repair():
    # An infra blip alongside a candidate rejection must not burn a repair round.
    d = FailureRouter().route(
        (
            _rec(FailureOwner.REPAIR),
            _rec(FailureOwner.INFRASTRUCTURE, Retryability.SAME_STAGE),
        ),
        repair_rounds_used=0,
    )
    assert d.owner is FailureOwner.INFRASTRUCTURE
    assert not d.consumes_repair_cycle


def test_classify_replay_control_vs_candidate():
    control = classify_replay_failures(("control did not reproduce the fault",))
    assert control[0].owner is FailureOwner.DIAGNOSIS
    candidate = classify_replay_failures(("candidate still fails scenario A",))
    assert candidate[0].owner is FailureOwner.REPAIR


def test_classify_exception_defaults_to_non_repair():
    integ = classify_exception(
        RuntimeError("candidate changed after freeze"), stage=StageName.VERIFICATION
    )
    assert integ.owner is FailureOwner.INTEGRITY
    unknown = classify_exception(RuntimeError("weird"), stage=StageName.REPAIR)
    # unknown must never silently become a repair round
    assert unknown.owner is not FailureOwner.REPAIR
