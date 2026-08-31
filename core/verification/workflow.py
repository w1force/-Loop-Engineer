"""Frozen inputs shared by Diagnose, Repair, replay, and hard verification.

The models in this module are deliberately narrower than an Agent transcript.
They carry only traceable incident facts and immutable verification contracts.
"""

from __future__ import annotations

import difflib
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Literal, Self

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from .generation_skill import (
    GenerationSkillAdvertisement,
    GenerationSkillChoice,
    GenerationSkillSelection,
    VerificationGenerationSkillCatalog,
    validate_generation_skill_selection,
)
from .models import (
    ARTICLE_MAX_VERIFICATION_ATTEMPTS,
    FrozenVerificationSkill,
    ScenarioSpec,
    ScenarioAssertionContract,
    VerificationModel,
    VerificationPolicy,
    VerificationRunRequest,
    VerificationSkillSpec,
)
from .runner import workspace_digest, workspace_manifest
from .skill import VerificationSkillLoader


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_DIFF_BYTES = 512 * 1024
_MAX_DIFF_FILE_BYTES = 256 * 1024


def canonical_json_digest(value: Any) -> str:
    """Hash a finite JSON value using the execution-window normalization."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be a finite JSON value") from exc
    return sha256(encoded).hexdigest()


def _model_digest(model: VerificationModel) -> str:
    return canonical_json_digest(model.model_dump(mode="json"))


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be relative to the repository")
    return value


class ArtifactReference(VerificationModel):
    """Immutable reference to raw evidence retained outside the conversation."""

    uri: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256.pattern)
    media_type: str = Field(default="application/octet-stream", min_length=1)


class SourceLocation(VerificationModel):
    path: str
    start_line: StrictInt = Field(ge=1)
    end_line: StrictInt | None = Field(default=None, ge=1)
    revision: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _relative_path(value)

    @model_validator(mode="after")
    def _ordered_lines(self) -> Self:
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
        return self


class FailureSignature(VerificationModel):
    """Stable incident signature emitted by the trusted scenario harness."""

    code: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
    error_type: str | None = Field(default=None, min_length=1)
    message_pattern: str | None = Field(default=None, min_length=1, max_length=512)
    event_code: str | None = Field(default=None, min_length=1)

    @field_validator("message_pattern")
    @classmethod
    def _valid_pattern(cls, value: str | None) -> str | None:
        if value is not None:
            compiled = re.compile(value)
            if compiled.search("") is not None:
                raise ValueError("message_pattern cannot match an empty string")
        return value

    @model_validator(mode="after")
    def _has_matcher(self) -> Self:
        if not any((self.error_type, self.message_pattern, self.event_code)):
            raise ValueError("failure_signature requires a concrete matcher")
        return self


class IncidentBundle(VerificationModel):
    """Diagnose output consumed by later stages instead of chat history."""

    schema_version: Literal["incident-bundle/v1"] = "incident-bundle/v1"
    incident_id: str
    requirement: str = Field(min_length=1)
    matched_rule: str = Field(min_length=1)
    error_logs: tuple[ArtifactReference, ...] = Field(min_length=1)
    original_trace: ArtifactReference
    source_locations: tuple[SourceLocation, ...] = Field(min_length=1)
    root_cause: str = Field(min_length=1)
    risk_tags: tuple[str, ...] = ()
    control_ref: str = Field(min_length=1)
    original_input: Any
    failure_signature: FailureSignature
    diagnosis_reproducer: Any | None = None
    diagnosis_reproducer_digest: str | None = Field(
        default=None, pattern=_SHA256.pattern
    )
    primary_signal_digest: str | None = Field(default=None, pattern=_SHA256.pattern)
    evidence_bundle_digest: str | None = Field(default=None, pattern=_SHA256.pattern)

    @field_validator("incident_id")
    @classmethod
    def _safe_incident_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("incident_id is not a safe identifier")
        return value

    @field_validator("risk_tags")
    @classmethod
    def _valid_risk_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value) or len(value) != len(set(value)):
            raise ValueError("risk_tags must be non-empty and unique")
        return value

    @field_validator("original_input")
    @classmethod
    def _json_input(cls, value: Any) -> Any:
        canonical_json_digest(value)
        return value

    @model_validator(mode="after")
    def _source_revisions_match_control(self) -> Self:
        if any(item.revision != self.control_ref for item in self.source_locations):
            raise ValueError(
                "every source location revision must match the frozen control_ref"
            )
        if (self.diagnosis_reproducer is None) != (
            self.diagnosis_reproducer_digest is None
        ):
            raise ValueError(
                "diagnosis reproducer and its digest must be supplied together"
            )
        if (
            self.diagnosis_reproducer is not None
            and canonical_json_digest(self.diagnosis_reproducer)
            != self.diagnosis_reproducer_digest
        ):
            raise ValueError("diagnosis reproducer digest mismatch")
        return self

    @property
    def digest(self) -> str:
        return _model_digest(self)


class FileChangeKind(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    TYPE_CHANGED = "type_changed"


class CandidateFileChange(VerificationModel):
    path: str
    kind: FileChangeKind
    before_sha256: str | None = Field(default=None, pattern=_SHA256.pattern)
    after_sha256: str | None = Field(default=None, pattern=_SHA256.pattern)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _relative_path(value)


class RepairResult(VerificationModel):
    """Small hand-off returned by a Repair Agent."""

    workspace: str = Field(min_length=1)
    candidate_ref: str = Field(min_length=1)
    implementation_summary: str = Field(min_length=1)
    test_entrypoints: tuple[str, ...] = Field(min_length=1)


class CandidateSnapshot(VerificationModel):
    workspace: str = Field(min_length=1)
    candidate_ref: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=_SHA256.pattern)
    changed_files: tuple[CandidateFileChange, ...] = Field(min_length=1)
    unified_diff: str = Field(min_length=1)
    implementation_summary: str = Field(min_length=1)
    test_entrypoints: tuple[str, ...] = Field(min_length=1)


def _read_snapshot_bytes(root: Path, relative: str) -> bytes | None:
    path = root / relative
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        return ("symlink:" + path.readlink().as_posix()).encode("utf-8")
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return path.read_bytes()


def _render_diff(
    control: Path,
    candidate: Path,
    changes: tuple[CandidateFileChange, ...],
) -> str:
    parts: list[str] = []
    size = 0
    for change in changes:
        before = _read_snapshot_bytes(control, change.path)
        after = _read_snapshot_bytes(candidate, change.path)
        if (
            (before is not None and len(before) > _MAX_DIFF_FILE_BYTES)
            or (after is not None and len(after) > _MAX_DIFF_FILE_BYTES)
        ):
            chunk = f"Binary or large file changed: {change.path}\n"
        else:
            try:
                before_lines = (before or b"").decode("utf-8").splitlines(True)
                after_lines = (after or b"").decode("utf-8").splitlines(True)
            except UnicodeDecodeError:
                chunk = f"Binary file changed: {change.path}\n"
            else:
                chunk = "".join(
                    difflib.unified_diff(
                        before_lines,
                        after_lines,
                        fromfile=f"a/{change.path}",
                        tofile=f"b/{change.path}",
                    )
                )
                if not chunk:
                    chunk = f"Metadata changed: {change.path}\n"
        encoded = chunk.encode("utf-8")
        if size + len(encoded) > _MAX_DIFF_BYTES:
            parts.append("... candidate diff truncated by Coordinator ...\n")
            break
        parts.append(chunk)
        size += len(encoded)
    return "".join(parts)


def capture_candidate_snapshot(
    *,
    control_workspace: str | Path,
    repair: RepairResult,
    workspace_ignore: tuple[str, ...],
) -> CandidateSnapshot:
    """Compute the candidate hand-off; never trust Agent-supplied diff or digest."""

    control = Path(control_workspace).resolve()
    candidate = Path(repair.workspace).resolve()
    if control == candidate:
        raise ValueError("control and candidate workspaces must be distinct")
    control_before = workspace_digest(control, workspace_ignore)
    candidate_before = workspace_digest(candidate, workspace_ignore)
    control_manifest = workspace_manifest(control, workspace_ignore)
    candidate_manifest = workspace_manifest(candidate, workspace_ignore)
    paths = sorted(set(control_manifest) | set(candidate_manifest))
    changes: list[CandidateFileChange] = []
    for relative in paths:
        before = control_manifest.get(relative)
        after = candidate_manifest.get(relative)
        if before == after:
            continue
        if before is None:
            kind = FileChangeKind.ADDED
        elif after is None:
            kind = FileChangeKind.DELETED
        elif before["type"] != after["type"]:
            kind = FileChangeKind.TYPE_CHANGED
        else:
            kind = FileChangeKind.MODIFIED
        changes.append(
            CandidateFileChange(
                path=relative,
                kind=kind,
                before_sha256=(str(before["sha256"]) if before else None),
                after_sha256=(str(after["sha256"]) if after else None),
            )
        )
    if not changes:
        raise ValueError("repair produced no candidate file changes")
    frozen_changes = tuple(changes)
    rendered = _render_diff(control, candidate, frozen_changes)
    if not rendered:
        raise ValueError("candidate diff is empty")
    if (
        workspace_digest(control, workspace_ignore) != control_before
        or workspace_digest(candidate, workspace_ignore) != candidate_before
    ):
        raise RuntimeError("workspace changed while the candidate snapshot was captured")
    return CandidateSnapshot(
        workspace=str(candidate),
        candidate_ref=repair.candidate_ref,
        candidate_digest=candidate_before,
        changed_files=frozen_changes,
        unified_diff=rendered,
        implementation_summary=repair.implementation_summary,
        test_entrypoints=repair.test_entrypoints,
    )


class RepairCycleRequest(VerificationModel):
    run_id: str
    cycle: StrictInt = Field(ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    incident: IncidentBundle
    candidate_workspace: str = Field(min_length=1)
    previous_failures: tuple[str, ...] = ()


class LightweightVerificationRequest(VerificationModel):
    run_id: str
    cycle: StrictInt = Field(ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    incident: IncidentBundle
    candidate: CandidateSnapshot


class LightweightVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"
    ERROR = "error"


class LightweightVerificationResult(VerificationModel):
    verdict: LightweightVerdict
    report: str = Field(min_length=1)

    @model_validator(mode="after")
    def _text_matches_verdict(self) -> Self:
        marker = f"VERDICT: {self.verdict.value.upper()}"
        lines = [line.strip().upper() for line in self.report.splitlines() if line.strip()]
        verdict_lines = [line for line in lines if line.startswith("VERDICT:")]
        if not lines or lines[-1] != marker or verdict_lines != [marker]:
            raise ValueError(
                f"lightweight report must end with exactly one literal {marker}"
            )
        return self


class AvailableVerificationSkill(VerificationModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    digest: str = Field(pattern=_SHA256.pattern)
    scenario_ids: tuple[str, ...] = Field(min_length=1)
    spec: VerificationSkillSpec

    @model_validator(mode="after")
    def _spec_matches_advertisement(self) -> Self:
        expected_ids = tuple(
            f"{self.name}:{scenario.id}"
            for scenario in (*self.spec.integration, *self.spec.ui)
        )
        if self.spec.name != self.name:
            raise ValueError("available Skill name does not match its frozen spec")
        if self.description != self.spec.description:
            raise ValueError("available Skill description does not match its frozen spec")
        if self.scenario_ids != expected_ids:
            raise ValueError("available Skill scenario ids do not match its frozen spec")
        return self


class VerificationPlanningRequest(VerificationModel):
    run_id: str
    cycle: StrictInt = Field(ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    incident: IncidentBundle
    control_workspace: str = Field(min_length=1)
    candidate: CandidateSnapshot
    policy: VerificationPolicy
    policy_digest: str = Field(pattern=_SHA256.pattern)
    available_skills: tuple[AvailableVerificationSkill, ...] = Field(min_length=1)
    available_generation_skills: tuple[GenerationSkillAdvertisement, ...] = ()

    @model_validator(mode="after")
    def _trusted_inputs_match_digests(self) -> Self:
        if self.policy.digest != self.policy_digest:
            raise ValueError("planning policy does not match policy_digest")
        names = [item.name for item in self.available_skills]
        if len(names) != len(set(names)):
            raise ValueError("available_skills cannot contain duplicate names")
        if self.policy.required_skills_by_rule:
            required = self.policy.required_skills_by_rule.get(
                self.incident.matched_rule
            )
            if required is None:
                raise ValueError(
                    "planning policy has no required Skills for matched_rule "
                    + self.incident.matched_rule
                )
            if tuple(names) != required:
                raise ValueError(
                    "available_skills do not match the trusted matched_rule mapping"
                )
        generation_names = [item.name for item in self.available_generation_skills]
        if len(generation_names) != len(set(generation_names)):
            raise ValueError(
                "available_generation_skills cannot contain duplicate names"
            )
        return self


class ReproductionSpec(VerificationModel):
    """Agent-proposed scenario parameters later checked against trusted policy."""

    scenario_id: str = Field(min_length=1)
    skill_name: str | None = Field(default=None, min_length=1)
    input_payload: Any
    input_digest: str = Field(pattern=_SHA256.pattern)
    reproducer: StrictBool = False
    failure_signature: FailureSignature | None = None
    expected_control_outcome: Literal["success", "failure"] | None = None
    expected_candidate_outcome: Literal["success"] = "success"
    allowed_changed_paths: tuple[str, ...] = ()
    required_changed_paths: tuple[str, ...] = ()
    forbidden_changed_paths: tuple[str, ...] = ()
    regression_assertions: tuple[str, ...] = ()
    boundary_assertions: tuple[str, ...] = ()
    side_effect_assertions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_reproduction(self) -> Self:
        if canonical_json_digest(self.input_payload) != self.input_digest:
            raise ValueError("input_payload does not match input_digest")
        if self.skill_name is not None and not self.scenario_id.startswith(
            self.skill_name + ":"
        ):
            raise ValueError("skill scenario_id must be prefixed by skill_name")
        if self.reproducer and (
            self.expected_control_outcome != "failure"
            or self.expected_candidate_outcome != "success"
            or self.failure_signature is None
        ):
            raise ValueError(
                "a reproducer requires control=failure, candidate=success and a signature"
            )
        if not set(self.required_changed_paths).issubset(self.allowed_changed_paths):
            raise ValueError("required_changed_paths must be allowed")
        overlap = set(self.allowed_changed_paths) & set(self.forbidden_changed_paths)
        if overlap:
            raise ValueError("changed paths cannot be both allowed and forbidden")
        return self

    @property
    def assertion_contract(self) -> ScenarioAssertionContract:
        return ScenarioAssertionContract(
            scenario_id=self.scenario_id,
            skill_name=self.skill_name,
            forbidden_changed_paths=self.forbidden_changed_paths,
            regression_assertions=self.regression_assertions,
            boundary_assertions=self.boundary_assertions,
            side_effect_assertions=self.side_effect_assertions,
        )


class VerificationPlanProposal(VerificationModel):
    skill_names: tuple[str, ...] = Field(min_length=1)
    generation_skill_names: tuple[str, ...] = ()
    generation_skill_digests: dict[str, str] = Field(default_factory=dict)
    generation_skill_choices: tuple[GenerationSkillChoice, ...] = ()
    reproductions: tuple[ReproductionSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_items(self) -> Self:
        if len(self.skill_names) != len(set(self.skill_names)):
            raise ValueError("skill_names cannot contain duplicates")
        if len(self.generation_skill_names) != len(set(self.generation_skill_names)):
            raise ValueError("generation_skill_names cannot contain duplicates")
        if set(self.generation_skill_digests) != set(self.generation_skill_names):
            raise ValueError(
                "generation_skill_digests must exactly match generation_skill_names"
            )
        choice_names = tuple(choice.skill_name for choice in self.generation_skill_choices)
        if choice_names != self.generation_skill_names:
            raise ValueError(
                "generation_skill_choices must exactly match generation_skill_names"
            )
        if any(
            not _SHA256.fullmatch(digest)
            for digest in self.generation_skill_digests.values()
        ):
            raise ValueError("generation_skill_digests must contain SHA-256 values")
        scenario_ids = [item.scenario_id for item in self.reproductions]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("scenario_id cannot contain duplicates")
        return self


class VerificationPlan(VerificationModel):
    schema_version: Literal["verification-plan/v1"] = "verification-plan/v1"
    run_id: str
    cycle: StrictInt = Field(ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    incident_id: str
    incident_digest: str = Field(pattern=_SHA256.pattern)
    workspace: str = Field(min_length=1)
    control_ref: str = Field(min_length=1)
    control_digest: str = Field(pattern=_SHA256.pattern)
    candidate_ref: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=_SHA256.pattern)
    policy: VerificationPolicy
    policy_digest: str = Field(pattern=_SHA256.pattern)
    skill_names: tuple[str, ...] = Field(min_length=1)
    skill_digests: dict[str, str] = Field(min_length=1)
    skill_contracts: tuple[FrozenVerificationSkill, ...] = Field(min_length=1)
    generation_skill_names: tuple[str, ...] = ()
    generation_skill_digests: dict[str, str] = Field(default_factory=dict)
    generation_skill_choices: tuple[GenerationSkillChoice, ...] = ()
    reproductions: tuple[ReproductionSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _bindings_match(self) -> Self:
        if not _SAFE_ID.fullmatch(self.run_id):
            raise ValueError("run_id is not a safe identifier")
        if self.policy.digest != self.policy_digest:
            raise ValueError("policy_digest does not match the frozen policy")
        names = tuple(item.name for item in self.skill_contracts)
        digests = {item.name: item.digest for item in self.skill_contracts}
        if names != self.skill_names or digests != self.skill_digests:
            raise ValueError("skill names/digests do not match frozen contracts")
        if len(self.generation_skill_names) != len(set(self.generation_skill_names)):
            raise ValueError("generation Skill names cannot contain duplicates")
        if set(self.generation_skill_digests) != set(self.generation_skill_names):
            raise ValueError(
                "generation Skill names/digests do not match the frozen plan"
            )
        choice_names = tuple(choice.skill_name for choice in self.generation_skill_choices)
        if choice_names != self.generation_skill_names:
            raise ValueError(
                "generation Skill choices do not match the frozen plan"
            )
        if any(
            not _SHA256.fullmatch(digest)
            for digest in self.generation_skill_digests.values()
        ):
            raise ValueError("generation Skill digests must be SHA-256 values")
        scenario_ids = [item.scenario_id for item in self.reproductions]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("frozen plan scenario_id cannot contain duplicates")
        if any(
            item.skill_name is not None and item.skill_name not in self.skill_names
            for item in self.reproductions
        ):
            raise ValueError("frozen plan assertion contract references an unselected Skill")
        # Construction validates tuple contents even before the Plan is converted
        # into its mandatory RunRequest representation.
        self.assertion_contracts
        return self

    @property
    def digest(self) -> str:
        return _model_digest(self)

    @property
    def scenario_input_digests(self) -> dict[str, str]:
        return {
            reproduction.scenario_id: reproduction.input_digest
            for reproduction in self.reproductions
        }

    @property
    def assertion_contracts(self) -> tuple[ScenarioAssertionContract, ...]:
        return tuple(
            reproduction.assertion_contract for reproduction in self.reproductions
        )

    def to_run_request(
        self, *, replay_receipt: Any | None = None, replay_digest: str | None = None
    ) -> VerificationRunRequest:
        if replay_receipt is not None:
            receipt_digest = replay_receipt.digest
            if replay_digest is not None and replay_digest != receipt_digest:
                raise ValueError("replay receipt digest 与显式 replay_digest 不一致")
            replay_digest = receipt_digest
            replay_manifest = replay_receipt.replay_manifest
        else:
            replay_manifest = None
        return VerificationRunRequest(
            run_id=self.run_id,
            cycle=self.cycle,
            incident_id=self.incident_id,
            incident_digest=self.incident_digest,
            plan_digest=self.digest,
            replay_digest=replay_digest,
            replay_manifest=replay_manifest,
            scenario_input_digests=self.scenario_input_digests,
            assertion_contracts=self.assertion_contracts,
            workspace=self.workspace,
            control_ref=self.control_ref,
            control_digest=self.control_digest,
            candidate_ref=self.candidate_ref,
            expected_candidate_digest=self.candidate_digest,
            expected_policy_digest=self.policy_digest,
            skill_names=self.skill_names,
            expected_skill_digests=self.skill_digests,
        )


class VerificationPlanFreezer:
    """Turn an untrusted Agent proposal into a trusted immutable plan."""

    def __init__(
        self,
        *,
        policy: VerificationPolicy,
        skill_loader: VerificationSkillLoader,
        allowed_skill_names: tuple[str, ...],
        generation_skill_catalog: VerificationGenerationSkillCatalog | None = None,
    ):
        if not allowed_skill_names or len(allowed_skill_names) != len(
            set(allowed_skill_names)
        ):
            raise ValueError("allowed_skill_names must be non-empty and unique")
        self.policy = VerificationPolicy.model_validate_json(policy.model_dump_json())
        self.skill_loader = skill_loader
        self.allowed_skill_names = allowed_skill_names
        self.generation_skill_catalog = generation_skill_catalog
        configured_skill_names = {
            name
            for names in self.policy.required_skills_by_rule.values()
            for name in names
        }
        unknown = sorted(configured_skill_names - set(self.allowed_skill_names))
        if unknown:
            raise ValueError(
                "required Skills are absent from allowed_skill_names: "
                + ", ".join(unknown)
            )

    def required_skill_names(self, matched_rule: str) -> tuple[str, ...]:
        """Resolve the policy-owned Skill set for one diagnosed rule.

        An empty mapping keeps old configurations safe by requiring the entire
        allowlist.  Once an explicit mapping exists, an unmapped incident fails
        closed instead of letting the planning Agent choose a fallback.
        """

        if not self.policy.required_skills_by_rule:
            return self.allowed_skill_names
        required = self.policy.required_skills_by_rule.get(matched_rule)
        if required is None:
            raise ValueError(
                "no required Skills configured for matched_rule " + matched_rule
            )
        return required

    def available_skills(
        self, matched_rule: str | None = None
    ) -> tuple[AvailableVerificationSkill, ...]:
        skill_names = (
            self.allowed_skill_names
            if matched_rule is None
            else self.required_skill_names(matched_rule)
        )
        skills = self.skill_loader.load_many(skill_names)
        return tuple(
            AvailableVerificationSkill(
                name=item.name,
                description=item.spec.description,
                digest=item.digest,
                scenario_ids=tuple(
                    f"{item.name}:{scenario.id}"
                    for scenario in (*item.spec.integration, *item.spec.ui)
                ),
                spec=item.spec,
            )
            for item in skills
        )

    def available_generation_skills(
        self,
    ) -> tuple[GenerationSkillAdvertisement, ...]:
        if self.generation_skill_catalog is None:
            return ()
        return self.generation_skill_catalog.discover()

    def freeze(
        self,
        proposal: VerificationPlanProposal,
        *,
        run_id: str,
        cycle: int,
        incident: IncidentBundle,
        candidate: CandidateSnapshot,
        control_digest: str,
    ) -> VerificationPlan:
        proposal = VerificationPlanProposal.model_validate_json(
            proposal.model_dump_json()
        )
        current_candidate_digest = workspace_digest(
            candidate.workspace, self.policy.workspace_ignore
        )
        if current_candidate_digest != candidate.candidate_digest:
            raise ValueError(
                "candidate workspace changed after the snapshot was captured"
            )
        generation_skill_digests: dict[str, str] = {}
        if self.generation_skill_catalog is None:
            if (
                proposal.generation_skill_names
                or proposal.generation_skill_digests
                or proposal.generation_skill_choices
            ):
                raise ValueError(
                    "proposal selected generation Skills but no trusted catalog is configured"
                )
        else:
            if not proposal.generation_skill_names:
                raise ValueError(
                    "a configured generation Skill catalog requires a non-empty selection"
                )
            advertisements = self.generation_skill_catalog.discover()
            advertised = {item.name: item for item in advertisements}
            unknown_generation = sorted(
                set(proposal.generation_skill_names) - set(advertised)
            )
            if unknown_generation:
                raise ValueError(
                    "proposal selected unadvertised generation Skills: "
                    + ", ".join(unknown_generation)
                )
            selected_generation_skills = self.generation_skill_catalog.load_selected(
                proposal.generation_skill_names,
                expected_metadata_digests={
                    name: advertised[name].metadata_digest
                    for name in proposal.generation_skill_names
                },
            )
            generation_skill_digests = {
                item.name: item.digest for item in selected_generation_skills
            }
            if generation_skill_digests != proposal.generation_skill_digests:
                raise ValueError(
                    "generation Skill content changed or proposal digest is untrusted"
                )
            validate_generation_skill_selection(
                GenerationSkillSelection(choices=proposal.generation_skill_choices),
                advertisements,
                matched_rule=incident.matched_rule,
                changed_paths=tuple(item.path for item in candidate.changed_files),
                risk_tags=incident.risk_tags,
            )
        disallowed = sorted(set(proposal.skill_names) - set(self.allowed_skill_names))
        if disallowed:
            raise ValueError("proposal selected unapproved skills: " + ", ".join(disallowed))
        required_skill_names = self.required_skill_names(incident.matched_rule)
        if set(proposal.skill_names) != set(required_skill_names):
            missing = sorted(set(required_skill_names) - set(proposal.skill_names))
            unexpected = sorted(set(proposal.skill_names) - set(required_skill_names))
            raise ValueError(
                "proposal Skill selection does not match trusted matched_rule mapping; "
                f"required={list(required_skill_names)}, missing={missing}, "
                f"unexpected={unexpected}"
            )
        skills = self.skill_loader.load_many(required_skill_names)
        contracts = tuple(
            FrozenVerificationSkill(name=item.name, spec=item.spec, digest=item.digest)
            for item in skills
        )
        command_scenario_entries: list[tuple[str, str | None, ScenarioSpec]] = [
            (f"{item.name}:{scenario.id}", item.name, scenario)
            for item in skills
            for scenario in (*item.spec.integration, *item.spec.ui)
        ]
        if self.policy.ui is not None and self.policy.ui.mode == "required":
            command_scenario_entries.extend(
                (f"global:{scenario.id}", None, scenario)
                for scenario in self.policy.ui.global_scenarios
            )
        command_scenarios = {
            scenario_id: (skill_name, scenario)
            for scenario_id, skill_name, scenario in command_scenario_entries
        }
        if len(command_scenarios) != len(command_scenario_entries):
            raise ValueError("trusted command scenario ids must be globally unique")
        expected_scenarios = {
            f"{item.name}:{scenario.id}"
            for item in skills
            for scenario in (*item.spec.integration, *item.spec.ui)
        }
        if self.policy.ui is not None and self.policy.ui.mode == "required":
            expected_scenarios.update(
                f"global:{scenario.id}" for scenario in self.policy.ui.global_scenarios
            )
        if self.policy.behavior is not None:
            expected_scenarios.update(
                item.scenario_id for item in self.policy.behavior.scenarios
            )
        actual_scenarios = {item.scenario_id for item in proposal.reproductions}
        if actual_scenarios != expected_scenarios:
            missing = sorted(expected_scenarios - actual_scenarios)
            extra = sorted(actual_scenarios - expected_scenarios)
            raise ValueError(
                f"proposal scenario coverage mismatch; missing={missing}, extra={extra}"
            )

        behavior_by_id = {
            item.scenario_id: item
            for item in (self.policy.behavior.scenarios if self.policy.behavior else ())
        }
        trusted_assertions_by_scenario: dict[
            str, dict[str, tuple[str, ...]]
        ] = {}
        for reproduction in proposal.reproductions:
            behavior = behavior_by_id.get(reproduction.scenario_id)
            command_binding = command_scenarios.get(reproduction.scenario_id)
            if command_binding is not None:
                expected_skill_name, scenario = command_binding
                if reproduction.skill_name != expected_skill_name:
                    raise ValueError(
                        f"scenario {reproduction.scenario_id} has an invalid Skill binding"
                    )
                step_ids = {step.id for step in scenario.steps}
                assertion_groups = {
                    "regression_assertions": reproduction.regression_assertions,
                    "boundary_assertions": reproduction.boundary_assertions,
                    "side_effect_assertions": reproduction.side_effect_assertions,
                }
                if not any(assertion_groups.values()):
                    raise ValueError(
                        f"scenario {reproduction.scenario_id} must bind at least "
                        "one regression, boundary or side-effect assertion step"
                    )
                for field_name, assertions in assertion_groups.items():
                    unknown = sorted(set(assertions) - step_ids)
                    if unknown:
                        raise ValueError(
                            f"scenario {reproduction.scenario_id} {field_name} "
                            "contains unknown or cross-scenario step ids: "
                            + ", ".join(unknown)
                        )
                trusted_assertions_by_scenario[reproduction.scenario_id] = (
                    scenario.trusted_assertion_groups
                )
            elif any(
                (
                    reproduction.regression_assertions,
                    reproduction.boundary_assertions,
                    reproduction.side_effect_assertions,
                )
            ):
                raise ValueError(
                    f"scenario {reproduction.scenario_id} assertions cannot bind "
                    "to a trusted command scenario"
                )
            if reproduction.forbidden_changed_paths and behavior is None:
                raise ValueError(
                    f"scenario {reproduction.scenario_id} forbidden_changed_paths "
                    "requires a trusted behavior scenario"
                )
            # Materialize once during freezing so malformed duplicate/empty values
            # cannot survive as merely agent-authored text in a frozen plan.
            reproduction.assertion_contract
            if behavior is None:
                if reproduction.reproducer:
                    raise ValueError("reproducer scenario is absent from trusted policy")
                continue
            expected = (
                behavior.reproducer,
                behavior.expected_control_outcome,
                behavior.expected_candidate_outcome,
                behavior.allowed_changed_paths,
                behavior.required_changed_paths,
                behavior.forbidden_changed_paths,
            )
            actual = (
                reproduction.reproducer,
                reproduction.expected_control_outcome,
                reproduction.expected_candidate_outcome,
                reproduction.allowed_changed_paths,
                reproduction.required_changed_paths,
                reproduction.forbidden_changed_paths,
            )
            if actual != expected:
                raise ValueError(
                    f"scenario {reproduction.scenario_id} weakens or changes trusted policy"
                )

        missing_assertion_groups = [
            field_name
            for field_name in (
                "regression_assertions",
                "boundary_assertions",
                "side_effect_assertions",
            )
            if not any(
                getattr(reproduction, field_name)
                for reproduction in proposal.reproductions
            )
        ]
        if missing_assertion_groups:
            raise ValueError(
                "plan must freeze all assertion categories; missing="
                + ", ".join(missing_assertion_groups)
            )

        for reproduction in proposal.reproductions:
            expected_assertions = trusted_assertions_by_scenario.get(
                reproduction.scenario_id
            )
            if expected_assertions is None:
                continue
            actual_assertions = {
                field_name: getattr(reproduction, field_name)
                for field_name in expected_assertions
            }
            if actual_assertions != expected_assertions:
                raise ValueError(
                    f"scenario {reproduction.scenario_id} assertion categories "
                    "do not match the trusted Skill step contract"
                )

        original_input_digest = canonical_json_digest(incident.original_input)
        matching = [
            item
            for item in proposal.reproductions
            if item.reproducer
            and item.input_digest == original_input_digest
            and item.failure_signature == incident.failure_signature
        ]
        if not matching:
            raise ValueError(
                "plan must reproduce the incident's frozen input and failure signature"
            )

        frozen_reproductions = tuple(
            reproduction.model_copy(
                update=trusted_assertions_by_scenario.get(
                    reproduction.scenario_id, {}
                )
            )
            for reproduction in proposal.reproductions
        )
        return VerificationPlan(
            run_id=run_id,
            cycle=cycle,
            incident_id=incident.incident_id,
            incident_digest=incident.digest,
            workspace=candidate.workspace,
            control_ref=incident.control_ref,
            control_digest=control_digest,
            candidate_ref=candidate.candidate_ref,
            candidate_digest=candidate.candidate_digest,
            policy=self.policy,
            policy_digest=self.policy.digest,
            skill_names=required_skill_names,
            skill_digests={item.name: item.digest for item in contracts},
            skill_contracts=contracts,
            generation_skill_names=proposal.generation_skill_names,
            generation_skill_digests=generation_skill_digests,
            generation_skill_choices=proposal.generation_skill_choices,
            reproductions=frozen_reproductions,
        )


__all__ = [
    "ArtifactReference",
    "AvailableVerificationSkill",
    "CandidateFileChange",
    "CandidateSnapshot",
    "FailureSignature",
    "FileChangeKind",
    "IncidentBundle",
    "LightweightVerificationRequest",
    "LightweightVerificationResult",
    "LightweightVerdict",
    "RepairCycleRequest",
    "RepairResult",
    "ReproductionSpec",
    "ScenarioAssertionContract",
    "SourceLocation",
    "VerificationPlan",
    "VerificationPlanFreezer",
    "VerificationPlanProposal",
    "VerificationPlanningRequest",
    "canonical_json_digest",
    "capture_candidate_snapshot",
]
