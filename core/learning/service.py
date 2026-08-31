"""Trusted orchestration for review-gated Repair Skill distillation."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from core.stages.common import parse_final_json
from core.verification.models import VerificationVerdict

from .archive import RepairTrajectoryArchive
from .catalog import LearnedSkillCatalog
from .models import (
    ExperienceSkill,
    HumanReviewDecision,
    LearningResult,
    PendingRepairTrajectory,
    ReviewStatus,
    SkillMatch,
    SkillMutationProposal,
    SkillProvenance,
    canonical_digest,
    repair_archive_payload_digest,
    utc_now,
)
from .prompts import SKILL_DISTILLATION_SYSTEM_PROMPT, build_skill_distillation_prompt
from .trajectory import (
    TextGenerator,
    TrajectoryCompressionError,
    TrajectoryCompressor,
    redact_sensitive,
)


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"learning artifact is not serializable: {type(value).__name__}")


class RepairLearningService:
    """Archive every reviewed trajectory; mutate Skills only after approval."""

    def __init__(
        self,
        *,
        archive: RepairTrajectoryArchive,
        catalog: LearnedSkillCatalog,
        compressor: TrajectoryCompressor,
        generator: TextGenerator,
        distillation_model: str = "google/gemini-3-flash",
        distillation_max_tokens: int = 2_500,
        candidate_limit: int = 5,
    ) -> None:
        if distillation_max_tokens < 1:
            raise ValueError("distillation_max_tokens must be positive")
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        self.archive = archive
        self.catalog = catalog
        self.compressor = compressor
        self.generator = generator
        self.distillation_model = distillation_model
        self.distillation_max_tokens = distillation_max_tokens
        self.candidate_limit = candidate_limit

    async def capture_verified(
        self,
        *,
        request: Any,
        repair: Any,
        candidate: Any,
        plan: Any,
        replay_receipt: Any,
        report: Any,
        prior_failures: tuple[str, ...] = (),
    ) -> LearningResult:
        """Stage a hard-VERIFIED repair while it waits for GitHub review."""

        raw_report = _dump(report)
        if raw_report.get("verdict") != VerificationVerdict.VERIFIED.value:
            raise ValueError("only hard-VERIFIED repairs may enter the learning queue")
        raw_request = _dump(request)
        raw_plan = _dump(plan)
        raw_receipt = _dump(replay_receipt)
        raw_incident = raw_request["incident"]
        incident_digest = canonical_digest(raw_incident)
        if raw_report.get("incident_digest") != incident_digest:
            raise ValueError("verified report does not match the captured incident")
        plan_digest = getattr(plan, "digest", None) or raw_plan.get("digest")
        replay_digest = getattr(replay_receipt, "digest", None) or raw_receipt.get(
            "digest"
        )
        if not isinstance(plan_digest, str) or raw_report.get("plan_digest") != plan_digest:
            raise ValueError("verified report does not match the captured plan")
        if (
            not isinstance(replay_digest, str)
            or raw_report.get("replay_digest") != replay_digest
        ):
            raise ValueError("verified report does not match the replay receipt")

        report_digest = canonical_digest(raw_report)
        report_payload = redact_sensitive(raw_report)
        request_payload = redact_sensitive(raw_request)
        repair_payload = redact_sensitive(_dump(repair))
        candidate_payload = redact_sensitive(_dump(candidate))
        plan_payload = redact_sensitive(raw_plan)
        replay_payload = redact_sensitive(raw_receipt)
        failure_payload = tuple(redact_sensitive(prior_failures))
        archive_payload_digest = repair_archive_payload_digest(
            incident=request_payload["incident"],
            repair=repair_payload,
            candidate=candidate_payload,
            verification_plan=plan_payload,
            replay_receipt=replay_payload,
            verification_report=report_payload,
            prior_failures=failure_payload,
        )
        pending = PendingRepairTrajectory(
            run_id=str(request_payload["run_id"]),
            incident_id=str(request_payload["incident"]["incident_id"]),
            cycle=int(repair_payload.get("cycle") or report_payload["cycle"]),
            trajectory_path=repair_payload.get("trajectory_path"),
            incident_digest=incident_digest,
            plan_digest=plan_digest,
            replay_digest=replay_digest,
            report_digest=report_digest,
            archive_payload_digest=archive_payload_digest,
            incident=request_payload["incident"],
            repair=repair_payload,
            candidate=candidate_payload,
            verification_plan=plan_payload,
            replay_receipt=replay_payload,
            verification_report=report_payload,
            prior_failures=failure_payload,
        )
        archive_path = self.archive.save_pending(pending)
        return LearningResult(
            run_id=pending.run_id,
            review_status=ReviewStatus.PENDING,
            archive_path=archive_path,
            action="pending",
        )

    async def bind_release(self, receipt: Any) -> str:
        """Bind the exact PR/head receipt to the staged trajectory."""

        payload = _dump(receipt)
        return self.archive.bind_release(str(payload["verification_run_id"]), payload)

    async def process_review(
        self, decision: HumanReviewDecision
    ) -> LearningResult:
        """Apply one trusted review decision idempotently."""

        decision = HumanReviewDecision.model_validate(
            redact_sensitive(decision.model_dump(mode="json"))
        )
        # Serialize the terminal check, model call, catalog mutation, and terminal
        # commit across worker processes. Provenance repairs the narrow crash window
        # between the catalog write and terminal receipt write.
        async with self.archive.run_lock(decision.run_id):
            return await self._process_review_locked(decision)

    async def _process_review_locked(
        self, decision: HumanReviewDecision
    ) -> LearningResult:
        existing_result = self.archive.load_result(decision.run_id, decision.digest)
        if existing_result is not None:
            return existing_result

        pending = self.archive.load_pending(decision.run_id)
        self._validate_review_binding(pending, decision)
        terminal_result = self.archive.load_terminal_result(decision.run_id)
        if terminal_result is not None:
            return terminal_result
        applied_result = self._recover_applied_result(decision.run_id)
        if applied_result is not None:
            # A prior worker may have committed the catalog mutation and crashed
            # before writing the archive receipt. Catalog provenance is the durable
            # write-ahead evidence that the earlier approved decision already won.
            self.archive.save_terminal_result(applied_result)
            self.archive.save_result(applied_result, decision.digest)
            return applied_result
        self.archive.save_review(decision)
        if decision.status is ReviewStatus.PENDING:
            result = LearningResult(
                run_id=decision.run_id,
                review_status=decision.status,
                action="pending",
                reason=decision.reason,
            )
            self.archive.save_result(result, decision.digest)
            return result

        compressed = self.archive.load_compressed(decision)
        if compressed is None:
            trajectory = self.archive.load_sharegpt(pending)
            try:
                compressed = await self.compressor.compress(
                    trajectory, review=decision
                )
            except Exception as exc:
                if decision.status in {
                    ReviewStatus.REJECTED,
                    ReviewStatus.STALE_HEAD,
                }:
                    default_reason = (
                        "verified pull-request head changed before approval"
                        if decision.status is ReviewStatus.STALE_HEAD
                        else "human review rejected the repair"
                    )
                    result = LearningResult(
                        run_id=decision.run_id,
                        review_status=decision.status,
                        archive_path=pending.trajectory_path,
                        action="archived_failed",
                        reason=(
                            (decision.reason or default_reason)
                            + "; compression unavailable: "
                            + f"{type(exc).__name__}: {exc}"
                        ),
                    )
                    self.archive.save_terminal_result(result)
                    self.archive.save_result(result, decision.digest)
                    return result
                if isinstance(exc, TrajectoryCompressionError):
                    result = LearningResult(
                        run_id=decision.run_id,
                        review_status=decision.status,
                        archive_path=pending.trajectory_path,
                        action="noop",
                        reason=f"trajectory archived but cannot fit target: {exc}",
                    )
                    self.archive.save_terminal_result(result)
                    self.archive.save_result(result, decision.digest)
                    return result
                raise
            compressed_path = self.archive.save_compressed(compressed)
        else:
            compressed_path = self.archive.compressed_path(decision)
        if decision.status in {ReviewStatus.REJECTED, ReviewStatus.STALE_HEAD}:
            default_reason = (
                "verified pull-request head changed before approval"
                if decision.status is ReviewStatus.STALE_HEAD
                else "human review rejected the repair"
            )
            result = LearningResult(
                run_id=decision.run_id,
                review_status=decision.status,
                archive_path=compressed_path,
                action="archived_failed",
                reason=decision.reason or default_reason,
            )
            self.archive.save_terminal_result(result)
            self.archive.save_result(result, decision.digest)
            return result

        if not compressed.eligible_for_skill:
            result = LearningResult(
                run_id=decision.run_id,
                review_status=decision.status,
                archive_path=compressed_path,
                action="noop",
                reason=(
                    "trajectory was archived but not distilled: it was incomplete or "
                    "contained no provider-visible reasoning"
                ),
            )
            self.archive.save_terminal_result(result)
            self.archive.save_result(result, decision.digest)
            return result

        result = await self._distill(
            pending=pending,
            compressed=compressed,
            compressed_path=compressed_path,
        )
        self.archive.save_terminal_result(result)
        self.archive.save_result(result, decision.digest)
        return result

    def _validate_review_binding(
        self,
        pending: PendingRepairTrajectory,
        decision: HumanReviewDecision,
    ) -> None:
        receipt = pending.release_receipt
        if receipt is None:
            raise ValueError("pending trajectory has no bound pull-request receipt")
        durable_receipt = self.archive.load_release_receipt(pending.run_id)
        if durable_receipt is None:
            raise ValueError("pending trajectory has no immutable release receipt")
        if canonical_digest(durable_receipt) != canonical_digest(receipt):
            raise ValueError("pending trajectory release receipt does not match archive")
        expected = {
            "verification_run_id": pending.run_id,
            "verification_incident_id": pending.incident_id,
            "verification_cycle": pending.cycle,
            "verification_incident_digest": pending.incident_digest,
            "candidate_digest": pending.candidate.get("candidate_digest"),
            "verification_report_digest": pending.report_digest,
            "repository": decision.repository,
            "pull_request_number": decision.pull_request_number,
            "commit_sha": decision.commit_sha,
        }
        mismatches = [
            key for key, value in expected.items() if receipt.get(key) != value
        ]
        if mismatches:
            raise ValueError(
                "review does not match the frozen release receipt: "
                + ", ".join(sorted(mismatches))
            )
        configured = {str(item).lower() for item in receipt.get("reviewers", ())}
        if (
            decision.status in {ReviewStatus.APPROVED, ReviewStatus.REJECTED}
            and decision.decision_source == "review"
            and (decision.reviewer or "").lower() not in configured
        ):
            raise ValueError("reviewer is not configured for this release")

    async def _distill(
        self,
        *,
        pending: PendingRepairTrajectory,
        compressed,
        compressed_path: str,
    ) -> LearningResult:
        match = self._trusted_match(pending)
        query = {
            "matched_rule": match.matched_rule,
            "signature_code": match.signature_code,
            "error_type": match.error_type,
            "event_code": match.event_code,
            "message": match.message_pattern or "",
            "source_paths": match.source_paths,
        }
        searched = list(self.catalog.search(query=query, limit=self.candidate_limit))
        loaded_names = compressed.loaded_skill_names
        loaded = [
            skill
            for name in loaded_names
            if name.startswith(self.catalog.name_prefix)
            if (skill := self.catalog.get(name)) is not None
        ]
        candidates: list[ExperienceSkill] = []
        for skill in (*loaded, *searched):
            if skill.name not in {item.name for item in candidates}:
                candidates.append(skill)
        candidates = candidates[: self.candidate_limit]

        raw = await self.generator.generate(
            system=SKILL_DISTILLATION_SYSTEM_PROMPT,
            prompt=build_skill_distillation_prompt(
                trajectory=compressed,
                pending=self._distillation_context(pending, match),
                candidates=candidates,
            ),
            model=self.distillation_model,
            max_tokens=self.distillation_max_tokens,
        )
        try:
            proposal = SkillMutationProposal.model_validate(
                parse_final_json(raw, label="Repair Skill distiller")
            )
        except (ValidationError, RuntimeError) as exc:
            raise ValueError(f"invalid Repair Skill mutation proposal: {exc}") from exc

        if proposal.action == "noop":
            return LearningResult(
                run_id=pending.run_id,
                review_status=compressed.review.status,
                archive_path=compressed_path,
                action="noop",
                reason=proposal.rationale,
            )

        exact_family = [
            item
            for item in candidates
            if item.match.matched_rule == match.matched_rule
            and item.match.signature_code == match.signature_code
        ]
        if proposal.action == "update":
            target = next(
                (item for item in exact_family if item.name == proposal.target_name),
                None,
            )
            if target is None:
                raise ValueError(
                    "distiller update target is not an exact-family candidate"
                )
        else:
            target = None
            if exact_family:
                raise ValueError(
                    "distiller cannot create a duplicate exact-family Skill; "
                    "update a compatible candidate or return noop"
                )

        provenance = SkillProvenance(
            run_id=pending.run_id,
            incident_id=pending.incident_id,
            candidate_digest=str(pending.candidate["candidate_digest"]),
            report_digest=pending.report_digest,
            review_digest=compressed.review.digest,
            compressed_trajectory_path=compressed_path,
        )
        now = utc_now()
        if target is None:
            family_digest = canonical_digest(
                {
                    # One deterministic namespace per trusted failure family.
                    # Content must not create parallel Skills for the same family.
                    "matched_rule": match.matched_rule,
                    "signature_code": match.signature_code,
                }
            )
            name = self.catalog.deterministic_name(
                signature_code=match.signature_code,
                family_digest=family_digest,
            )
            skill = ExperienceSkill(
                name=name,
                description=proposal.description or "",
                match=match,
                applicable_when=proposal.applicable_when,
                diagnosis_steps=proposal.diagnosis_steps,
                repair_steps=proposal.repair_steps,
                pitfalls=proposal.pitfalls,
                provenance=(provenance,),
                created_at=now,
                updated_at=now,
            )
            action = "created"
            expected_revision = 0
        else:
            current = self.catalog.get(target.name)
            if current is None or current.revision != target.revision:
                raise RuntimeError("learned Skill changed after it was selected")
            skill = current.model_copy(
                update={
                    "description": proposal.description or "",
                    # The prompt requires a complete improved body. Replace mutable
                    # guidance so an approved update can correct obsolete advice;
                    # provenance, identity, and revision remain host-owned.
                    "applicable_when": proposal.applicable_when,
                    "diagnosis_steps": proposal.diagnosis_steps,
                    "repair_steps": proposal.repair_steps,
                    "pitfalls": proposal.pitfalls,
                    "provenance": (*current.provenance, provenance),
                    "revision": current.revision + 1,
                    "updated_at": now,
                }
            )
            action = "updated"
            expected_revision = current.revision

        digest = self.catalog.write(skill, expected_revision=expected_revision)
        return LearningResult(
            run_id=pending.run_id,
            review_status=compressed.review.status,
            archive_path=compressed_path,
            action=action,
            skill_name=skill.name,
            skill_digest=digest,
            reason=proposal.rationale,
        )

    def _recover_applied_result(self, run_id: str) -> LearningResult | None:
        matches = [
            (skill, index, provenance)
            for skill in self.catalog.list()
            for index, provenance in enumerate(skill.provenance)
            if provenance.run_id == run_id
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise RuntimeError("one learning run appears in multiple catalog Skills")
        skill, provenance_index, provenance = matches[0]
        return LearningResult(
            run_id=run_id,
            review_status=ReviewStatus.APPROVED,
            archive_path=provenance.compressed_trajectory_path,
            action="created" if provenance_index == 0 else "updated",
            skill_name=skill.name,
            skill_digest=self.catalog.digest(skill),
            reason="recovered an already-applied catalog mutation",
        )

    @staticmethod
    def _trusted_match(pending: PendingRepairTrajectory) -> SkillMatch:
        signature = pending.incident["failure_signature"]
        return SkillMatch(
            matched_rule=str(pending.incident["matched_rule"]),
            signature_code=str(signature["code"]),
            error_type=signature.get("error_type"),
            event_code=signature.get("event_code"),
            message_pattern=signature.get("message_pattern"),
            source_paths=tuple(
                str(item["path"]) for item in pending.incident["source_locations"]
            ),
        )

    @staticmethod
    def _distillation_context(
        pending: PendingRepairTrajectory, match: SkillMatch
    ) -> dict[str, Any]:
        """Bounded trusted facts; large diff/plan/report bodies stay in the archive."""

        changed_files = []
        for item in pending.candidate.get("changed_files", ()):
            if isinstance(item, dict):
                changed_files.append(
                    {"path": item.get("path"), "kind": item.get("kind")}
                )
        return {
            "run_id": pending.run_id,
            "incident_id": pending.incident_id,
            "cycle": pending.cycle,
            "match": match.model_dump(mode="json"),
            "root_cause": pending.incident.get("root_cause"),
            "risk_tags": pending.incident.get("risk_tags", ()),
            "implementation_summary": pending.repair.get("implementation_summary"),
            "test_entrypoints": pending.repair.get("test_entrypoints", ()),
            "unresolved_risks": pending.repair.get("unresolved_risks", ()),
            "candidate_digest": pending.candidate.get("candidate_digest"),
            "changed_files": changed_files,
            "verification_verdict": pending.verification_report.get("verdict"),
        }


__all__ = ["RepairLearningService"]
