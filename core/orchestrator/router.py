"""Structured failure routing — the fix for the coordinator's all-failures->repair bug.

The old ``VerificationCoordinator`` wrapped its whole cycle in one ``except
Exception`` and pushed every failure into ``prior_failures`` for the next repair
round. Infra blips, policy blocks, integrity violations and observability gaps all
looked like "the candidate is still broken", wasting repair rounds and sometimes
looping forever.

Here every failure is classified into a ``FailureRecord`` with an explicit
``owner``, and :class:`FailureRouter` maps the governing failure to a
:class:`NextAction`. Only ``owner == REPAIR`` rejections consume a repair round;
infrastructure/policy/observability/integrity failures never do.
"""

from __future__ import annotations

from pydantic import Field

from core.contracts.base import Contract
from core.contracts.failure import (
    FailureOwner,
    FailureRecord,
    NextAction,
    Retryability,
    StageName,
    StageStatus,
)
from core.verification.models import (
    GateStatus,
    VerificationReport,
    VerificationVerdict,
)

# Higher = more governing. When a stage returns several findings, the failure with
# the highest priority decides the next action. A REPAIR rejection is the *normal*
# retry path, so it only wins when nothing more blocking is present.
_OWNER_PRIORITY: dict[FailureOwner, int] = {
    FailureOwner.INTEGRITY: 70,
    FailureOwner.POLICY: 60,
    FailureOwner.DIAGNOSIS: 50,
    FailureOwner.OBSERVABILITY: 40,
    FailureOwner.INFRASTRUCTURE: 30,
    FailureOwner.RELEASE: 20,
    FailureOwner.REPAIR: 10,
}


class RoutingDecision(Contract):
    next_action: NextAction
    owner: FailureOwner
    consumes_repair_cycle: bool
    escalate: bool
    reason: str = Field(min_length=1)
    governing: FailureRecord | None = None
    findings: tuple[FailureRecord, ...] = ()


def _governing(findings: tuple[FailureRecord, ...]) -> FailureRecord:
    return max(findings, key=lambda f: _OWNER_PRIORITY.get(f.owner, 0))


class FailureRouter:
    """Deterministically decide the next action from structured findings."""

    def __init__(self, *, max_repair_rounds: int = 3, max_infra_retries: int = 2):
        if max_repair_rounds < 1:
            raise ValueError("max_repair_rounds must be positive")
        self.max_repair_rounds = max_repair_rounds
        self.max_infra_retries = max_infra_retries

    def route(
        self,
        findings: tuple[FailureRecord, ...],
        *,
        repair_rounds_used: int,
        infra_retries_used: int = 0,
    ) -> RoutingDecision:
        if not findings:
            raise ValueError("route() requires at least one failure")
        gov = _governing(findings)
        owner = gov.owner

        if owner is FailureOwner.REPAIR:
            if repair_rounds_used >= self.max_repair_rounds:
                return self._decide(
                    NextAction.ESCALATE, gov, findings,
                    escalate=True,
                    reason=(
                        f"candidate rejected after {repair_rounds_used} repair "
                        f"round(s); repair budget exhausted"
                    ),
                )
            return self._decide(
                NextAction.NEW_REPAIR_ROUND, gov, findings,
                consumes_repair_cycle=True,
                reason=f"candidate rejected: {gov.summary}",
            )

        if owner is FailureOwner.DIAGNOSIS:
            return self._decide(
                NextAction.REDIAGNOSE, gov, findings,
                reason=f"diagnosis wrong / control cannot reproduce: {gov.summary}",
            )

        if owner is FailureOwner.POLICY:
            if gov.retryability is Retryability.SAME_STAGE:
                return self._decide(
                    NextAction.REPLAN_SAME_CANDIDATE, gov, findings,
                    reason=f"plan violates policy, replanning: {gov.summary}",
                )
            return self._decide(
                NextAction.ESCALATE, gov, findings,
                escalate=True,
                reason=f"blocked by policy / missing skill or proof: {gov.summary}",
            )

        if owner is FailureOwner.INFRASTRUCTURE:
            if (
                gov.retryability is Retryability.SAME_STAGE
                and infra_retries_used < self.max_infra_retries
            ):
                return self._decide(
                    NextAction.RETRY_STAGE, gov, findings,
                    reason=f"transient infrastructure error, retrying: {gov.summary}",
                )
            return self._decide(
                NextAction.ESCALATE, gov, findings,
                escalate=True,
                reason=f"infrastructure error, retry budget exhausted: {gov.summary}",
            )

        if owner is FailureOwner.OBSERVABILITY:
            if gov.retryability is Retryability.EXTERNAL_WAIT:
                return self._decide(
                    NextAction.WAIT_EXTERNAL, gov, findings,
                    reason=f"waiting for evidence/watermark: {gov.summary}",
                )
            return self._decide(
                NextAction.REVERIFY_SAME_CANDIDATE, gov, findings,
                reason=f"incomplete evidence, re-collecting: {gov.summary}",
            )

        if owner is FailureOwner.INTEGRITY:
            return self._decide(
                NextAction.INVALIDATE_RUN, gov, findings,
                escalate=True,
                reason=f"frozen input changed — integrity blocked: {gov.summary}",
            )

        if owner is FailureOwner.RELEASE:
            return self._decide(
                NextAction.RETRY_STAGE, gov, findings,
                reason=f"release side-effect failed, idempotent retry: {gov.summary}",
            )

        raise ValueError(f"unroutable failure owner: {owner}")

    @staticmethod
    def _decide(
        action: NextAction,
        gov: FailureRecord,
        findings: tuple[FailureRecord, ...],
        *,
        consumes_repair_cycle: bool = False,
        escalate: bool = False,
        reason: str,
    ) -> RoutingDecision:
        return RoutingDecision(
            next_action=action,
            owner=gov.owner,
            consumes_repair_cycle=consumes_repair_cycle,
            escalate=escalate,
            reason=reason,
            governing=gov,
            findings=findings,
        )


# ── classifiers: raw signals -> structured FailureRecord ──────────────────────

def classify_verification_report(
    report: VerificationReport,
) -> tuple[FailureRecord, ...]:
    """Turn a non-VERIFIED machine report into owner-tagged findings.

    Verdict is the primary signal; gate results refine the owner (a TRACE/log gate
    in ERROR is observability/infra, a BLOCKED gate is policy) so a transient trace
    collection error never masquerades as a broken candidate.
    """

    if report.verdict is VerificationVerdict.VERIFIED:
        return ()

    findings: list[FailureRecord] = []
    for gate in report.gate_results:
        if gate.status in (GateStatus.PASS, GateStatus.NOT_APPLICABLE, GateStatus.SKIPPED):
            continue
        if gate.status is GateStatus.ERROR:
            owner = (
                FailureOwner.OBSERVABILITY
                if gate.gate.value in ("trace", "staging_log")
                else FailureOwner.INFRASTRUCTURE
            )
            retry = Retryability.SAME_STAGE
        elif gate.status is GateStatus.BLOCKED:
            owner, retry = FailureOwner.POLICY, Retryability.NONE
        else:  # FAIL -> the candidate really failed this gate
            owner, retry = FailureOwner.REPAIR, Retryability.NONE
        findings.append(
            FailureRecord(
                code=f"gate.{gate.gate.value}.{gate.status.value}",
                stage=StageName.VERIFICATION,
                owner=owner,
                retryability=retry,
                consumes_repair_cycle=owner is FailureOwner.REPAIR,
                summary=f"gate {gate.gate.value} -> {gate.status.value}"
                + (f": {'; '.join(gate.failures)}" if getattr(gate, "failures", ()) else ""),
                gate=gate.gate.value,
                candidate_digest=report.candidate_digest,
            )
        )

    if not findings:
        # verdict is non-VERIFIED but no individual gate explains it: map verdict.
        owner_by_verdict = {
            VerificationVerdict.REJECTED: (FailureOwner.REPAIR, Retryability.NONE),
            VerificationVerdict.BLOCKED: (FailureOwner.POLICY, Retryability.NONE),
            VerificationVerdict.ERROR: (
                FailureOwner.INFRASTRUCTURE,
                Retryability.SAME_STAGE,
            ),
        }
        owner, retry = owner_by_verdict.get(
            report.verdict, (FailureOwner.INFRASTRUCTURE, Retryability.NONE)
        )
        findings.append(
            FailureRecord(
                code=f"verdict.{report.verdict.value}",
                stage=StageName.VERIFICATION,
                owner=owner,
                retryability=retry,
                consumes_repair_cycle=owner is FailureOwner.REPAIR,
                summary=f"verification verdict: {report.verdict.value}",
                candidate_digest=report.candidate_digest,
            )
        )
    return tuple(findings)


# message fragments -> owner. Order matters: earlier, more specific wins.
# Kept deliberately conservative: replay rejections and verification verdicts are
# classified structurally (see classify_replay_failures / classify_verification_report),
# not by scanning ambiguous exception text.
_MESSAGE_RULES: tuple[tuple[tuple[str, ...], FailureOwner, Retryability], ...] = (
    (("changed after", "workspace changed", "identity changed",
      "digest changed", "does not match frozen"), FailureOwner.INTEGRITY, Retryability.NONE),
    (("cannot reproduce", "did not reproduce", "failed to reproduce",
      "control did not"), FailureOwner.DIAGNOSIS, Retryability.NONE),
    (("unapproved skill", "required skill", "no required skills", "matched_rule",
      "policy", "proof"), FailureOwner.POLICY, Retryability.NONE),
    (("evidence", "window", "correlation", "watermark", "incomplete", "ambiguous",
      "late", "duplicate"), FailureOwner.OBSERVABILITY, Retryability.EXTERNAL_WAIT),
    # A failed repair preflight means the candidate itself is wrong -> repair again.
    (("lightweight verification", "self-check", "preflight"),
     FailureOwner.REPAIR, Retryability.NONE),
)


def classify_replay_failures(
    failures: tuple[str, ...], *, candidate_digest: str | None = None
) -> tuple[FailureRecord, ...]:
    """Classify control/candidate replay rejections.

    Control failing to reproduce the original fault means the diagnosis/baseline is
    wrong (``DIAGNOSIS`` -> rediagnose); anything else means the candidate did not
    fix it (``REPAIR`` -> new round).
    """

    records: list[FailureRecord] = []
    for item in failures or ("control/candidate replay rejected",):
        low = item.lower()
        if "control" in low or "reproduce" in low:
            owner = FailureOwner.DIAGNOSIS
        else:
            owner = FailureOwner.REPAIR
        records.append(
            FailureRecord(
                code=f"replay.{owner.value}",
                stage=StageName.VERIFICATION,
                owner=owner,
                retryability=Retryability.NONE,
                consumes_repair_cycle=owner is FailureOwner.REPAIR,
                summary=f"replay rejected: {item}"[:500],
                candidate_digest=candidate_digest,
            )
        )
    return tuple(records)


def classify_exception(exc: BaseException, *, stage: StageName) -> FailureRecord:
    """Best-effort classifier for exceptions raised by the trusted machinery.

    Typed replay errors are infrastructure; otherwise we scan the message. Unknown
    exceptions default to INFRASTRUCTURE with no retry so they escalate rather than
    silently burn repair rounds.
    """

    from core.verification.replay import ReplayError

    message = str(exc) or type(exc).__name__
    low = message.lower()

    if isinstance(exc, ReplayError):
        owner, retry = FailureOwner.INFRASTRUCTURE, Retryability.SAME_STAGE
    else:
        owner, retry = FailureOwner.INFRASTRUCTURE, Retryability.NONE
        for fragments, rule_owner, rule_retry in _MESSAGE_RULES:
            if any(fragment in low for fragment in fragments):
                owner, retry = rule_owner, rule_retry
                break

    return FailureRecord(
        code=f"{stage.value}.exception.{type(exc).__name__}",
        stage=stage,
        owner=owner,
        retryability=retry,
        consumes_repair_cycle=owner is FailureOwner.REPAIR,
        summary=message[:500],
    )


__all__ = [
    "FailureRouter",
    "RoutingDecision",
    "classify_exception",
    "classify_replay_failures",
    "classify_verification_report",
]
