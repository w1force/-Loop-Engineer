from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.learning.archive import RepairTrajectoryArchive
from core.learning.catalog import LearnedSkillCatalog
from core.learning.models import (
    HumanReviewDecision,
    PendingRepairTrajectory,
    ReviewStatus,
    ShareGPTTrajectory,
    canonical_digest,
    repair_archive_payload_digest,
)
from core.learning.service import RepairLearningService
from core.learning.trajectory import CompressionConfig, TrajectoryCompressor
from core.learning.review_worker import PendingReviewWorker


class _Generator:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def generate(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected generator call")
        return self.responses.pop(0)


class _ConcurrentCreateGenerator(_Generator):
    """Hold the first two proposals until both workers selected no candidate."""

    def __init__(self, response: str) -> None:
        super().__init__()
        self.response = response
        self._arrived = 0
        self._ready = asyncio.Event()

    async def generate(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if len(self.calls) <= 2:
            self._arrived += 1
            if self._arrived == 2:
                self._ready.set()
            await asyncio.wait_for(self._ready.wait(), timeout=5)
        return self.response


def _service(tmp_path: Path, generator: _Generator) -> RepairLearningService:
    return RepairLearningService(
        archive=RepairTrajectoryArchive(tmp_path / "archive"),
        catalog=LearnedSkillCatalog(tmp_path / "skills"),
        compressor=TrajectoryCompressor(
            generator=generator,
            config=CompressionConfig(target_max_tokens=100_000),
        ),
        generator=generator,
    )


def _pending(
    tmp_path: Path,
    *,
    run_id: str,
    completed: bool = True,
    reasoning: bool = True,
) -> PendingRepairTrajectory:
    trajectory = ShareGPTTrajectory(
        run_id=run_id,
        incident_id="incident-1",
        cycle=1,
        conversations=(
            {"from": "system", "value": "repair SOP"},
            {"from": "human", "value": "INCIDENT_JSON diagnosis result"},
            {"from": "gpt", "value": "inspected and repaired"},
        ),
        model="model-a",
        completed=completed,
        terminal_reason="completed" if completed else "model_error",
        reasoning_blocks=(
            ({"turn": 1, "type": "thinking", "thinking": "root cause path"},)
            if reasoning
            else ()
        ),
    )
    path = tmp_path / f"{run_id}.sharegpt.jsonl"
    path.write_text(
        json.dumps(trajectory.model_dump(mode="json"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    incident = {
        "incident_id": "incident-1",
        "matched_rule": "checkout-timeout",
        "root_cause": "fallback was skipped",
        "risk_tags": ["availability"],
        "failure_signature": {
            "code": "checkout.timeout",
            "error_type": "TimeoutError",
            "event_code": None,
            "message_pattern": "timed out",
        },
        "source_locations": [{"path": "service.py"}],
    }
    incident_digest = canonical_digest(incident)
    plan_digest = "b" * 64
    replay_digest = "c" * 64
    report = {
        "run_id": run_id,
        "cycle": 1,
        "incident_id": "incident-1",
        "incident_digest": incident_digest,
        "plan_digest": plan_digest,
        "replay_digest": replay_digest,
        "candidate_digest": "a" * 64,
        "verdict": "verified",
    }
    repair = {
        "implementation_summary": "use the bounded fallback",
        "test_entrypoints": ["pytest -q tests/test_checkout.py"],
        "unresolved_risks": [],
        "trajectory_path": str(path),
    }
    candidate = {
        "candidate_digest": "a" * 64,
        "changed_files": [{"path": "service.py", "kind": "modified"}],
        "unified_diff": "x" * 100_000,
    }
    verification_plan = {"digest": plan_digest}
    replay_receipt = {"digest": replay_digest}
    return PendingRepairTrajectory(
        run_id=run_id,
        incident_id="incident-1",
        cycle=1,
        trajectory_path=str(path),
        incident_digest=incident_digest,
        plan_digest=plan_digest,
        replay_digest=replay_digest,
        report_digest=canonical_digest(report),
        archive_payload_digest=repair_archive_payload_digest(
            incident=incident,
            repair=repair,
            candidate=candidate,
            verification_plan=verification_plan,
            replay_receipt=replay_receipt,
            verification_report=report,
            prior_failures=(),
        ),
        incident=incident,
        repair=repair,
        candidate=candidate,
        verification_plan=verification_plan,
        replay_receipt=replay_receipt,
        verification_report=report,
    )


def _receipt(pending: PendingRepairTrajectory) -> dict:
    return {
        "schema_version": "github-pr-receipt/v2",
        "app_id": "checkout",
        "verification_run_id": pending.run_id,
        "verification_cycle": pending.cycle,
        "verification_incident_id": "incident-1",
        "verification_incident_digest": pending.incident_digest,
        "candidate_digest": "a" * 64,
        "verification_report_digest": pending.report_digest,
        "verification_report_path": "/evidence/report.json",
        "repository": "acme/service",
        "branch": "fix/checkout-timeout_20260831_1",
        "base_branch": "main",
        "pull_request_number": 7,
        "pull_request_url": "https://github.com/acme/service/pull/7",
        "commit_sha": "d" * 40,
        "reviewers": ["owner"],
    }


def _decision(run_id: str, status: ReviewStatus) -> HumanReviewDecision:
    kwargs = {}
    if status in {ReviewStatus.APPROVED, ReviewStatus.REJECTED}:
        kwargs = {"reviewer": "owner", "review_id": 11}
    return HumanReviewDecision(
        run_id=run_id,
        repository="acme/service",
        pull_request_number=7,
        commit_sha="d" * 40,
        status=status,
        **kwargs,
    )


def _mutation(action: str, *, target: str | None = None) -> str:
    payload = {
        "action": action,
        "target_name": target,
        "rationale": "reusable timeout fallback",
        "description": "Diagnose and repair checkout timeout fallback failures.",
        "applicable_when": ["checkout timeout reproduces on the control build"],
        "diagnosis_steps": ["confirm the fallback branch was skipped"],
        "repair_steps": ["route timeout through the bounded fallback"],
        "pitfalls": ["do not weaken timeout verification"],
    }
    return json.dumps(payload)


@pytest.mark.asyncio
async def test_rejected_trajectory_is_review_only_even_without_reasoning(
    tmp_path: Path,
) -> None:
    generator = _Generator()
    service = _service(tmp_path, generator)
    pending = _pending(
        tmp_path, run_id="run-rejected", completed=False, reasoning=False
    )
    service.archive.save_pending(pending)
    service.archive.bind_release(pending.run_id, _receipt(pending))

    result = await service.process_review(
        _decision(pending.run_id, ReviewStatus.REJECTED)
    )

    assert result.action == "archived_failed"
    assert Path(result.archive_path or "").is_file()
    assert service.catalog.list() == ()
    assert generator.calls == []


@pytest.mark.asyncio
async def test_stale_head_is_archived_as_a_terminal_failure(tmp_path: Path) -> None:
    generator = _Generator()
    service = _service(tmp_path, generator)
    pending = _pending(tmp_path, run_id="run-stale-head")
    service.archive.save_pending(pending)
    service.archive.bind_release(pending.run_id, _receipt(pending))

    result = await service.process_review(
        _decision(pending.run_id, ReviewStatus.STALE_HEAD)
    )

    assert result.action == "archived_failed"
    assert result.review_status is ReviewStatus.STALE_HEAD
    assert Path(result.archive_path or "").is_file()
    assert service.archive.load_terminal_result(pending.run_id) == result
    assert service.archive.list_pending() == ()
    assert service.catalog.list() == ()
    assert generator.calls == []


@pytest.mark.asyncio
async def test_approved_without_visible_reasoning_is_archived_but_not_distilled(
    tmp_path: Path,
) -> None:
    generator = _Generator()
    service = _service(tmp_path, generator)
    pending = _pending(tmp_path, run_id="run-no-reasoning", reasoning=False)
    service.archive.save_pending(pending)
    service.archive.bind_release(pending.run_id, _receipt(pending))

    result = await service.process_review(
        _decision(pending.run_id, ReviewStatus.APPROVED)
    )

    assert result.action == "noop"
    assert "no provider-visible reasoning" in (result.reason or "")
    assert service.catalog.list() == ()
    assert generator.calls == []


@pytest.mark.asyncio
async def test_approved_create_then_exact_target_update_is_idempotent(
    tmp_path: Path,
) -> None:
    generator = _Generator(_mutation("create"))
    service = _service(tmp_path, generator)
    first = _pending(tmp_path, run_id="run-create")
    service.archive.save_pending(first)
    service.archive.bind_release(first.run_id, _receipt(first))
    decision = _decision(first.run_id, ReviewStatus.APPROVED)

    created = await service.process_review(decision)
    repeated = await service.process_review(decision)

    assert created.action == "created"
    assert repeated == created
    assert created.skill_name and created.skill_name.startswith("learned-repair-")
    skill = service.catalog.get(created.skill_name)
    assert skill is not None and skill.revision == 1
    assert "root cause path" in generator.calls[0]["prompt"]

    replacement = json.loads(_mutation("update", target=created.skill_name))
    replacement["diagnosis_steps"] = ["use the replacement diagnostic clue"]
    replacement["repair_steps"] = ["use the corrected bounded fallback"]
    generator.responses.append(json.dumps(replacement))
    second = _pending(tmp_path, run_id="run-update")
    service.archive.save_pending(second)
    service.archive.bind_release(second.run_id, _receipt(second))
    updated = await service.process_review(
        _decision(second.run_id, ReviewStatus.APPROVED)
    )

    assert updated.action == "updated"
    skill = service.catalog.get(created.skill_name)
    assert skill is not None
    assert skill.revision == 2
    assert len(skill.provenance) == 2
    assert skill.diagnosis_steps == ("use the replacement diagnostic clue",)
    assert skill.repair_steps == ("use the corrected bounded fallback",)
    # The 100 KB diff is archived, but never repeated into the distillation prompt.
    assert "x" * 1_000 not in generator.calls[-1]["prompt"]


@pytest.mark.asyncio
async def test_unconfigured_reviewer_cannot_archive_a_rejection(tmp_path: Path) -> None:
    service = _service(tmp_path, _Generator())
    pending = _pending(tmp_path, run_id="run-untrusted")
    service.archive.save_pending(pending)
    service.archive.bind_release(pending.run_id, _receipt(pending))
    decision = _decision(pending.run_id, ReviewStatus.REJECTED).model_copy(
        update={"reviewer": "intruder"}
    )

    with pytest.raises(ValueError, match="not configured"):
        await service.process_review(decision)
    assert service.catalog.list() == ()


@pytest.mark.asyncio
async def test_embedded_release_receipt_must_match_immutable_artifact(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _Generator(_mutation("create")))
    pending = _pending(tmp_path, run_id="run-receipt-tamper")
    pending_path = Path(service.archive.save_pending(pending))
    service.archive.bind_release(pending.run_id, _receipt(pending))
    payload = json.loads(pending_path.read_text(encoding="utf-8"))
    payload["release_receipt"]["reviewers"] = ["intruder"]
    pending_path.write_text(json.dumps(payload), encoding="utf-8")
    decision = _decision(pending.run_id, ReviewStatus.APPROVED).model_copy(
        update={"reviewer": "intruder"}
    )

    with pytest.raises(ValueError, match="does not match archive"):
        await service.process_review(decision)
    assert service.catalog.list() == ()


@pytest.mark.asyncio
async def test_pending_capture_freezes_trajectory_before_human_review(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _Generator())
    pending = _pending(tmp_path, run_id="run-frozen")
    original_source = Path(pending.trajectory_path or "")

    service.archive.save_pending(pending)
    frozen = service.archive.load_pending(pending.run_id)
    assert frozen.trajectory_path != str(original_source)
    assert frozen.trajectory_digest is not None

    original_source.write_text('{"corrupted": true}\n', encoding="utf-8")
    loaded = service.archive.load_sharegpt(frozen)
    assert loaded.digest == frozen.trajectory_digest
    assert loaded.conversations[-1]["value"] == "inspected and repaired"


@pytest.mark.asyncio
async def test_capture_verified_binds_all_trusted_digests(tmp_path: Path) -> None:
    service = _service(tmp_path, _Generator())
    fixture = _pending(tmp_path, run_id="run-capture")

    result = await service.capture_verified(
        request={"run_id": fixture.run_id, "incident": fixture.incident},
        repair=fixture.repair,
        candidate=fixture.candidate,
        plan={"digest": fixture.plan_digest},
        replay_receipt={"digest": fixture.replay_digest},
        report=fixture.verification_report,
    )

    frozen = service.archive.load_pending(fixture.run_id)
    assert result.action == "pending"
    assert frozen.incident_digest == fixture.incident_digest
    assert frozen.plan_digest == fixture.plan_digest
    assert frozen.replay_digest == fixture.replay_digest
    assert frozen.report_digest == fixture.report_digest
    assert frozen.trajectory_digest is not None


@pytest.mark.asyncio
async def test_report_provenance_keeps_the_pre_redaction_receipt_digest(
    tmp_path: Path,
) -> None:
    generator = _Generator(_mutation("create"))
    service = _service(tmp_path, generator)
    fixture = _pending(tmp_path, run_id="run-redacted-report")
    report = {
        **fixture.verification_report,
        "operator_note": "API_KEY=do-not-store-this",
    }
    expected_report_digest = canonical_digest(report)

    await service.capture_verified(
        request={"run_id": fixture.run_id, "incident": fixture.incident},
        repair=fixture.repair,
        candidate=fixture.candidate,
        plan=fixture.verification_plan,
        replay_receipt=fixture.replay_receipt,
        report=report,
    )
    frozen = service.archive.load_pending(fixture.run_id)
    service.archive.bind_release(fixture.run_id, _receipt(frozen))
    result = await service.process_review(
        _decision(fixture.run_id, ReviewStatus.APPROVED)
    )

    skill = service.catalog.get(result.skill_name or "")
    assert skill is not None
    assert skill.provenance[0].report_digest == expected_report_digest
    assert "do-not-store-this" not in json.dumps(
        frozen.model_dump(mode="json"), ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_one_verified_run_can_only_reach_one_terminal_learning_result(
    tmp_path: Path,
) -> None:
    generator = _Generator(_mutation("create"))
    service = _service(tmp_path, generator)
    pending = _pending(tmp_path, run_id="run-once")
    service.archive.save_pending(pending)
    service.archive.bind_release(pending.run_id, _receipt(pending))

    first = _decision(pending.run_id, ReviewStatus.APPROVED)
    created = await service.process_review(first)
    changed_payload = first.model_copy(
        update={"review_id": 12, "payload_digest": "e" * 64}
    )
    repeated = await service.process_review(changed_payload)

    assert repeated == created
    assert len(generator.calls) == 1
    skill = service.catalog.get(created.skill_name or "")
    assert skill is not None and skill.revision == 1
    assert [item.run_id for item in skill.provenance] == [pending.run_id]


@pytest.mark.asyncio
async def test_two_service_instances_cannot_apply_one_run_twice(
    tmp_path: Path,
) -> None:
    archive = RepairTrajectoryArchive(tmp_path / "archive")
    catalog = LearnedSkillCatalog(tmp_path / "skills")
    first_generator = _Generator(_mutation("create"))
    second_payload = json.loads(_mutation("create"))
    second_payload["repair_steps"] = ["use a different repair proposal"]
    second_generator = _Generator(json.dumps(second_payload))
    services = [
        RepairLearningService(
            archive=RepairTrajectoryArchive(archive.root),
            catalog=LearnedSkillCatalog(catalog.root),
            compressor=TrajectoryCompressor(
                generator=generator,
                config=CompressionConfig(target_max_tokens=100_000),
            ),
            generator=generator,
        )
        for generator in (first_generator, second_generator)
    ]
    pending = _pending(tmp_path, run_id="run-cross-worker-once")
    archive.save_pending(pending)
    archive.bind_release(pending.run_id, _receipt(pending))
    decision = _decision(pending.run_id, ReviewStatus.APPROVED)

    results = await asyncio.gather(
        *(service.process_review(decision) for service in services)
    )

    assert results[0] == results[1]
    assert results[0].action == "created"
    assert len(first_generator.calls) + len(second_generator.calls) == 1
    skills = catalog.list()
    assert len(skills) == 1
    assert [item.run_id for item in skills[0].provenance] == [pending.run_id]


@pytest.mark.asyncio
async def test_concurrent_first_create_for_one_family_is_cas_exclusive_and_retryable(
    tmp_path: Path,
) -> None:
    generator = _ConcurrentCreateGenerator(_mutation("create"))
    services = [_service(tmp_path, generator) for _ in range(2)]
    pending = [
        _pending(tmp_path, run_id="run-family-a"),
        _pending(tmp_path, run_id="run-family-b"),
    ]
    for service, item in zip(services, pending, strict=True):
        service.archive.save_pending(item)
        service.archive.bind_release(item.run_id, _receipt(item))

    first_attempts = await asyncio.gather(
        *(
            service.process_review(_decision(item.run_id, ReviewStatus.APPROVED))
            for service, item in zip(services, pending, strict=True)
        ),
        return_exceptions=True,
    )

    created = [item for item in first_attempts if not isinstance(item, BaseException)]
    conflicts = [item for item in first_attempts if isinstance(item, BaseException)]
    assert len(created) == 1
    assert created[0].action == "created"
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], RuntimeError)
    assert "expected=0, actual=1" in str(conflicts[0])
    skills = services[0].catalog.list()
    assert len(skills) == 1
    assert skills[0].revision == 1

    winner_run = skills[0].provenance[0].run_id
    loser_index = next(
        index for index, item in enumerate(pending) if item.run_id != winner_run
    )
    generator.response = _mutation("update", target=skills[0].name)
    retried = await services[loser_index].process_review(
        _decision(pending[loser_index].run_id, ReviewStatus.APPROVED)
    )

    assert retried.action == "updated"
    final_skills = services[0].catalog.list()
    assert len(final_skills) == 1
    assert final_skills[0].revision == 2
    assert {item.run_id for item in final_skills[0].provenance} == {
        "run-family-a",
        "run-family-b",
    }


@pytest.mark.asyncio
async def test_catalog_mutation_is_recovered_after_terminal_write_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = _Generator(_mutation("create"))
    service = _service(tmp_path, generator)
    pending = _pending(tmp_path, run_id="run-terminal-recovery")
    service.archive.save_pending(pending)
    service.archive.bind_release(pending.run_id, _receipt(pending))
    decision = _decision(pending.run_id, ReviewStatus.APPROVED)
    save_terminal = service.archive.save_terminal_result
    attempts = 0

    def fail_once(result):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated crash before terminal receipt")
        return save_terminal(result)

    monkeypatch.setattr(service.archive, "save_terminal_result", fail_once)
    with pytest.raises(OSError, match="simulated crash"):
        await service.process_review(decision)

    recovered = await service.process_review(
        _decision(pending.run_id, ReviewStatus.REJECTED)
    )

    assert recovered.action == "created"
    assert recovered.review_status is ReviewStatus.APPROVED
    assert recovered.skill_name is not None
    assert recovered.skill_digest == service.catalog.digest(
        service.catalog.get(recovered.skill_name)
    )
    assert len(generator.calls) == 1


def test_pending_archive_rejects_structured_payload_tampering(tmp_path: Path) -> None:
    service = _service(tmp_path, _Generator())
    pending = _pending(tmp_path, run_id="run-payload-integrity")
    pending_path = Path(service.archive.save_pending(pending))
    payload = json.loads(pending_path.read_text(encoding="utf-8"))
    payload["incident"]["matched_rule"] = "tampered-rule"
    pending_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="archived repair payload digest mismatch"):
        service.archive.load_pending(pending.run_id)


def test_release_binding_checks_cycle_incident_and_report_digests(tmp_path: Path) -> None:
    service = _service(tmp_path, _Generator())
    pending = _pending(tmp_path, run_id="run-binding")
    service.archive.save_pending(pending)
    receipt = _receipt(pending)
    receipt["verification_cycle"] = 2
    receipt["verification_incident_digest"] = "e" * 64
    receipt["verification_report_digest"] = "f" * 64
    with pytest.raises(ValueError, match="verification_cycle"):
        service.archive.bind_release(pending.run_id, receipt)
    assert service.archive.load_pending(pending.run_id).release_receipt is None


@pytest.mark.asyncio
async def test_pending_review_worker_reads_bound_receipts(tmp_path: Path) -> None:
    service = _service(tmp_path, _Generator())
    pending = _pending(tmp_path, run_id="run-worker")
    service.archive.save_pending(pending)
    service.archive.bind_release(pending.run_id, _receipt(pending))

    class _Monitor:
        async def poll(self, receipt):
            assert receipt.verification_run_id == pending.run_id
            return (
                _decision(pending.run_id, ReviewStatus.PENDING),
                object(),
            )

    summary = await PendingReviewWorker(
        archive=service.archive,
        monitor=_Monitor(),
    ).poll_once()

    assert summary.checked == 1
    assert summary.pending == 1
    assert summary.finalized == 0
    assert summary.failures == ()


@pytest.mark.asyncio
async def test_pending_review_worker_reports_an_unbound_trajectory(tmp_path: Path) -> None:
    service = _service(tmp_path, _Generator())
    pending = _pending(tmp_path, run_id="run-orphan")
    service.archive.save_pending(pending)

    class _Monitor:
        async def poll(self, receipt):
            raise AssertionError("an unbound trajectory cannot be polled")

    summary = await PendingReviewWorker(
        archive=service.archive,
        monitor=_Monitor(),
    ).poll_once()

    assert summary.checked == 1
    assert summary.pending == 1
    assert summary.finalized == 0
    assert "no bound release receipt" in summary.failures[0]


@pytest.mark.asyncio
async def test_pending_review_worker_recovers_a_persisted_release_receipt(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _Generator())
    pending = _pending(tmp_path, run_id="run-reconcile")
    service.archive.save_pending(pending)
    receipt_root = tmp_path / "release-receipts"
    receipt_root.mkdir()
    receipt_root.joinpath(f"{pending.run_id}.json").write_text(
        json.dumps(_receipt(pending)), encoding="utf-8"
    )

    class _Monitor:
        async def poll(self, receipt):
            return (_decision(pending.run_id, ReviewStatus.PENDING), object())

    summary = await PendingReviewWorker(
        archive=service.archive,
        monitor=_Monitor(),
        receipt_roots=(receipt_root,),
    ).poll_once()

    assert summary.checked == 1
    assert summary.pending == 1
    assert summary.failures == ()
    assert service.archive.load_pending(pending.run_id).release_receipt is not None


@pytest.mark.asyncio
async def test_pending_review_worker_reconciles_embedded_and_operator_receipts(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _Generator())
    pending = _pending(tmp_path, run_id="run-receipt-conflict")
    service.archive.save_pending(pending)
    service.archive.bind_release(pending.run_id, _receipt(pending))
    receipt_root = tmp_path / "operator-receipts"
    receipt_root.mkdir()
    conflicting = _receipt(pending)
    conflicting["reviewers"] = ["different-owner"]
    receipt_root.joinpath(f"{pending.run_id}.json").write_text(
        json.dumps(conflicting), encoding="utf-8"
    )

    class _Monitor:
        async def poll(self, receipt):
            raise AssertionError("a conflicting receipt cannot be polled")

    summary = await PendingReviewWorker(
        archive=service.archive,
        monitor=_Monitor(),
        receipt_roots=(receipt_root,),
    ).poll_once()

    assert summary.finalized == 0
    assert summary.pending == 0
    assert "conflicting release receipts" in summary.failures[0]
