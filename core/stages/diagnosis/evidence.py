"""Trusted source-backed evidence planning and bounded log retrieval.

The planner is an untrusted, read-only Agent.  The freezer proves every proposed
log template exists in the frozen control source.  Retrieval then uses one of two
paths: deterministic correlation-ID lookup, or bounded BM25 discovery followed by
an ID proof and deterministic lookup.  Scores select context; they never decide a
repair or release verdict.
"""

from __future__ import annotations

import ast
import json
import math
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Protocol

from pydantic import ValidationError

from core.agents.verification import (
    build_verification_can_use_tool,
    select_verification_tools,
)
from core.contracts.evidence import (
    CorrelationIdentity,
    CorrelationProof,
    DiagnosisEvidenceBundle,
    EvidenceLogPlan,
    EvidenceLogPlanProposal,
    EvidenceMatchStatus,
    EvidenceObservation,
    EvidenceResult,
    FrozenEvidenceLog,
    IncidentSignal,
    PrimarySignal,
    TemporalRelation,
)
from core.forked_agent import run_subagent
from core.stages.common import (
    FrozenStageSkill,
    build_stage_system_prompt,
    freeze_stage_skill,
    parse_final_json,
)
from core.verification.workflow import SourceLocation


EVIDENCE_PLANNER_AGENT_TYPE = "diagnosis-evidence-planning"
_DEFAULT_EVIDENCE_SKILL = "skills/diagnosis-evidence/SKILL.md"
_DYNAMIC_PART = re.compile(
    r"(\{[^{}]+\}|\$\{[^{}]+\}|%(?:\([^)]+\))?[#0 +\-.0-9]*[a-zA-Z]|"
    r"<(?:n|uuid|hex)>|\\[nrt])"
)
_SEARCH_TERM = re.compile(r"[A-Za-z0-9_:.\-/]{2,}|[\u3400-\u9fff]{2,}")
_STOP_TERMS = {
    "debug",
    "error",
    "exception",
    "failed",
    "failure",
    "info",
    "log",
    "none",
    "null",
    "trace",
    "warn",
}


class EvidencePlanningError(RuntimeError):
    """The Agent proposal cannot be frozen or evidence cannot be retrieved safely."""


class EvidenceLogStore(Protocol):
    def search_logs_exact(self, **kwargs: Any) -> list[dict[str, Any]]: ...

    def search_logs_bm25(self, **kwargs: Any) -> list[dict[str, Any]]: ...


class DiagnosisEvidencePlanner:
    """Fresh-context source auditor that proposes prioritized log evidence sites."""

    def __init__(self, *, skill_path: str | Path = _DEFAULT_EVIDENCE_SKILL):
        self.frozen_skill: FrozenStageSkill = freeze_stage_skill(skill_path)

    async def propose(
        self,
        *,
        primary_signal: PrimarySignal,
        related_signals: tuple[IncidentSignal, ...] = (),
        control_ref: str,
        control_workspace: str,
        requirement: str,
        parent_agent_state,
        parent_params,
        tracer,
        max_turns: int = 12,
    ) -> EvidenceLogPlanProposal:
        control = Path(control_workspace).resolve()
        if not control.is_dir():
            raise EvidencePlanningError("control workspace does not exist")
        tools = select_verification_tools(parent_params.tools)
        missing = {"Read", "Glob", "Grep"} - {tool.name for tool in tools}
        if missing:
            raise EvidencePlanningError(
                "evidence planner missing read-only tools: "
                + ", ".join(sorted(missing))
            )
        prompt = (
            "Audit the frozen source call chain for the primary signal and propose "
            "the concrete log emission sites that would answer the diagnosis "
            "questions. Return only one EvidenceLogPlanProposal JSON object. "
            "Do not search a log database, edit files, or diagnose the root cause.\n\n"
            "CONTROL_REF:\n"
            + control_ref
            + "\n\nREQUIREMENT:\n"
            + requirement
            + "\n\nPRIMARY_SIGNAL_JSON:\n"
            + json.dumps(
                primary_signal.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n\nRELATED_SIGNALS_JSON:\n"
            + json.dumps(
                [item.model_dump(mode="json") for item in related_signals],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n\nOUTPUT_JSON_SCHEMA:\n"
            + json.dumps(
                EvidenceLogPlanProposal.model_json_schema(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        result = await run_subagent(
            parent_agent_state=parent_agent_state,
            parent_params=parent_params,
            task_prompt=prompt,
            tracer=tracer.child(agent_type=EVIDENCE_PLANNER_AGENT_TYPE, depth=1),
            context_mode="fresh",
            system_override=build_stage_system_prompt(frozen=self.frozen_skill),
            tools_override=tools,
            cwd_override=str(control),
            can_use_tool=build_verification_can_use_tool(parent_params.can_use_tool),
            max_turns=max_turns,
            abort_signal=parent_params.abort_signal,
            propagate_errors=False,
        )
        parent_agent_state.total_input_tokens += result.usage.input_tokens
        parent_agent_state.total_output_tokens += result.usage.output_tokens
        if not result.successful:
            raise EvidencePlanningError(
                result.error
                or result.terminal.error
                or f"evidence planner terminated: {result.terminal.reason.value}"
            )
        try:
            return EvidenceLogPlanProposal.model_validate(
                parse_final_json(result.final_text, label="Evidence Planner")
            )
        except (ValidationError, RuntimeError) as exc:
            raise EvidencePlanningError(
                f"invalid EvidenceLogPlanProposal: {exc}"
            ) from exc


class EvidencePlanFreezer:
    """Verify Agent-proposed templates against immutable source bytes."""

    def __init__(self, *, max_source_bytes: int = 2 * 1024 * 1024):
        self.max_source_bytes = max_source_bytes

    def freeze(
        self,
        proposal: EvidenceLogPlanProposal,
        *,
        primary_signal: PrimarySignal,
        control_workspace: str | Path,
        control_ref: str,
        planner_skill_digest: str,
    ) -> EvidenceLogPlan:
        proposal = EvidenceLogPlanProposal.model_validate_json(
            proposal.model_dump_json()
        )
        root = Path(control_workspace).resolve()
        frozen: list[FrozenEvidenceLog] = []
        for item in proposal.evidence_logs:
            if item.source_revision is not None and item.source_revision != control_ref:
                raise EvidencePlanningError(
                    f"{item.evidence_id}: source revision differs from control_ref"
                )
            source, relative = self._resolve_source(root, item.source_path)
            raw = source.read_bytes()
            if len(raw) > self.max_source_bytes:
                raise EvidencePlanningError(
                    f"{item.evidence_id}: source file exceeds safety limit"
                )
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise EvidencePlanningError(
                    f"{item.evidence_id}: source file is not UTF-8"
                ) from exc
            lines = text.splitlines()
            end_line = item.end_line or item.start_line
            if end_line - item.start_line > 200:
                raise EvidencePlanningError(
                    f"{item.evidence_id}: source range exceeds 200 lines"
                )
            if item.start_line > len(lines) or end_line > len(lines):
                raise EvidencePlanningError(
                    f"{item.evidence_id}: source line is outside the file"
                )
            snippet = "\n".join(lines[item.start_line - 1 : end_line])
            if not _template_is_source_backed(item.template, snippet):
                raise EvidencePlanningError(
                    f"{item.evidence_id}: proposed template is absent from source range"
                )
            if not _is_log_emission_site(
                source=source,
                source_text=text,
                snippet=snippet,
                start_line=item.start_line,
                end_line=end_line,
            ):
                raise EvidencePlanningError(
                    f"{item.evidence_id}: cited source range is not a log emission site"
                )
            template_pattern = _compile_template_pattern(item.template)
            template_id = _canonical_digest(
                {
                    "service": item.service or primary_signal.service,
                    "event_name": item.event_name,
                    "template": _normalize_space(item.template),
                }
            )
            frozen.append(
                FrozenEvidenceLog(
                    evidence_id=item.evidence_id,
                    priority=item.priority,
                    question=item.question,
                    reason=item.reason,
                    source=SourceLocation(
                        path=relative,
                        start_line=item.start_line,
                        end_line=item.end_line,
                        revision=control_ref,
                    ),
                    source_sha256=sha256(raw).hexdigest(),
                    service=item.service or primary_signal.service,
                    logger=item.logger,
                    level=item.level,
                    template=item.template,
                    template_id=template_id,
                    template_pattern=template_pattern,
                    event_name=item.event_name,
                    error_code=item.error_code,
                    relation=item.relation,
                    max_time_delta_seconds=item.max_time_delta_seconds,
                    expected_presence=item.expected_presence,
                    extract_fields=item.extract_fields,
                    extract_patterns=item.extract_patterns,
                )
            )
        frozen.sort(key=lambda item: (-item.priority, item.evidence_id))
        return EvidenceLogPlan(
            primary_signal_digest=primary_signal.digest,
            control_ref=control_ref,
            planner_skill_digest=planner_skill_digest,
            evidence_logs=tuple(frozen),
        )

    @staticmethod
    def _resolve_source(root: Path, value: str) -> tuple[Path, str]:
        proposed = Path(value)
        source = proposed.resolve() if proposed.is_absolute() else (root / proposed).resolve()
        try:
            relative = source.relative_to(root)
        except ValueError as exc:
            raise EvidencePlanningError("evidence source escapes control workspace") from exc
        if not source.is_file():
            raise EvidencePlanningError(f"evidence source does not exist: {relative}")
        return source, relative.as_posix()


class DiagnosisEvidenceRetriever:
    """Retrieve and context-pack evidence without allowing ranking to become truth."""

    def __init__(
        self,
        store: EvidenceLogStore,
        *,
        confidence_threshold: float = 0.58,
        ambiguity_margin: float = 0.08,
        max_total_tokens: int = 24_576,
        max_observations: int = 20,
        full_priority_count: int = 5,
        full_log_tokens: int = 4_096,
        tail_log_tokens: int = 1_024,
    ):
        if not 0 <= confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be in 0..1")
        if not 0 <= ambiguity_margin <= 1:
            raise ValueError("ambiguity_margin must be in 0..1")
        if min(
            max_total_tokens,
            max_observations,
            full_priority_count,
            full_log_tokens,
            tail_log_tokens,
        ) < 1:
            raise ValueError("evidence context limits must be positive")
        self.store = store
        self.confidence_threshold = confidence_threshold
        self.ambiguity_margin = ambiguity_margin
        self.max_total_tokens = max_total_tokens
        self.max_observations = max_observations
        self.full_priority_count = full_priority_count
        self.full_log_tokens = full_log_tokens
        self.tail_log_tokens = tail_log_tokens

    def retrieve(
        self,
        *,
        primary_signal: PrimarySignal,
        plan: EvidenceLogPlan,
        collection_complete: bool = True,
    ) -> DiagnosisEvidenceBundle:
        if plan.primary_signal_digest != primary_signal.digest:
            raise EvidencePlanningError("evidence plan is bound to another primary signal")
        if primary_signal.timestamp_ns is None:
            return self._unavailable_bundle(
                primary_signal=primary_signal,
                plan=plan,
                reason="primary signal has no trusted timestamp; unbounded scan refused",
            )

        strongest = primary_signal.correlation.strongest
        proof: CorrelationProof | None = None
        if strongest is not None and primary_signal.correlation.has_strong_id:
            raw_results = self._retrieve_exact(
                primary_signal, plan, strongest, collection_complete=collection_complete
            )
        else:
            fuzzy_results, anchor_candidates = self._retrieve_fuzzy(
                primary_signal, plan, collection_complete=collection_complete
            )
            proof = self._select_correlation_proof(anchor_candidates)
            if proof is None:
                raw_results = fuzzy_results
            else:
                identity = proof.identity.strongest
                assert identity is not None
                raw_results = self._retrieve_exact(
                    primary_signal,
                    plan,
                    identity,
                    collection_complete=collection_complete,
                )

        results, total_tokens = self._pack_results(raw_results)
        return DiagnosisEvidenceBundle(
            primary_signal_digest=primary_signal.digest,
            plan_digest=plan.digest,
            collection_complete=collection_complete,
            correlation_proof=proof,
            results=results,
            total_tokens=total_tokens,
        )

    def _retrieve_exact(
        self,
        primary: PrimarySignal,
        plan: EvidenceLogPlan,
        identity: tuple[str, str],
        *,
        collection_complete: bool,
    ) -> list[dict[str, Any]]:
        if primary.timestamp_ns is None:
            raise EvidencePlanningError("exact retrieval requires a primary timestamp")
        field, value = identity
        results: list[dict[str, Any]] = []
        for item in plan.evidence_logs:
            start_ns, end_ns = _time_bounds(primary.timestamp_ns, item)
            rows = self.store.search_logs_exact(
                identity_field=field,
                identity_value=value,
                service_name=item.service,
                start_time_ns=start_ns,
                end_time_ns=end_ns,
                anchor_time_ns=primary.timestamp_ns,
                environment=primary.environment,
                deployment_version=primary.deployment_version,
                logger=item.logger,
                level=item.level,
                event_name=item.event_name,
                event_code=item.error_code,
                limit=50,
            )
            matched = [row for row in rows if _row_matches_plan(row, item)]
            results.append(
                {
                    "item": item,
                    "status": (
                        EvidenceMatchStatus.EXACT_MATCH
                        if matched
                        else _absent_status(collection_complete)
                    ),
                    "rows": matched[:3],
                    "mode": "exact",
                    "collection_complete": collection_complete,
                    "reason": None if matched else _absence_reason(collection_complete),
                }
            )
        return results

    def _retrieve_fuzzy(
        self,
        primary: PrimarySignal,
        plan: EvidenceLogPlan,
        *,
        collection_complete: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if primary.timestamp_ns is None:
            raise EvidencePlanningError("fuzzy retrieval requires a primary timestamp")
        results: list[dict[str, Any]] = []
        anchors: list[dict[str, Any]] = []
        for item in plan.evidence_logs:
            start_ns, end_ns = _time_bounds(primary.timestamp_ns, item)
            query: dict[str, Any] = dict(
                terms=_search_terms(item),
                service_name=item.service,
                start_time_ns=start_ns,
                end_time_ns=end_ns,
                environment=primary.environment,
                deployment_version=primary.deployment_version,
                logger=item.logger,
                level=item.level,
                limit=100,
            )
            if primary.correlation.erp is not None:
                query["erp"] = primary.correlation.erp
            rows = self.store.search_logs_bm25(**query)
            scored = self._score_rows(rows, primary, item)
            confident = [entry for entry in scored if entry[0] >= self.confidence_threshold]
            status = (
                EvidenceMatchStatus.FUZZY_FALLBACK
                if confident
                else _absent_status(collection_complete)
            )
            result = {
                "item": item,
                "status": status,
                "rows": [dict(row, _evidence_score=score) for score, row in confident[:3]],
                "mode": "fuzzy",
                "collection_complete": collection_complete,
                "reason": None if confident else _absence_reason(collection_complete),
            }
            results.append(result)
            for score, row in confident:
                identity = _extract_identity(row, item)
                if identity.has_strong_id:
                    anchors.append(
                        {
                            "score": score,
                            "row": row,
                            "identity": identity,
                            "result": result,
                        }
                    )

        anchors.sort(
            key=lambda entry: (
                -entry["score"],
                abs(
                    int(entry["row"].get("timestamp_ns") or 0)
                    - int(primary.timestamp_ns or 0)
                ),
                str(entry["row"].get("observation_id") or ""),
            )
        )
        if anchors:
            runner_up = next(
                (
                    candidate
                    for candidate in anchors[1:]
                    if candidate["identity"].strongest
                    != anchors[0]["identity"].strongest
                ),
                None,
            )
            margin = anchors[0]["score"] - (
                runner_up["score"] if runner_up is not None else 0.0
            )
            if runner_up is not None and margin < self.ambiguity_margin:
                anchors[0]["result"]["status"] = (
                    EvidenceMatchStatus.CORRELATION_AMBIGUOUS
                )
                anchors[0]["result"]["reason"] = (
                    "top correlation candidates are too close to choose safely"
                )
        return results, anchors

    def _score_rows(
        self,
        rows: list[dict[str, Any]],
        primary: PrimarySignal,
        item: FrozenEvidenceLog,
    ) -> list[tuple[float, dict[str, Any]]]:
        if not rows:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        window_ns = max(1, item.max_time_delta_seconds * 1_000_000_000)
        search_terms = _search_terms(item)
        for row in rows:
            # FTS uses OR semantics only to produce a bounded candidate set.  A
            # candidate must still match the source-frozen log template; otherwise
            # one generic term can never become a high-confidence correlation.
            if not _row_matches_template(row, item):
                continue
            timestamp_ns = int(row.get("timestamp_ns") or 0)
            delta = abs(timestamp_ns - int(primary.timestamp_ns or 0))
            time_score = math.exp(-delta / window_ns)
            lexical_score = _term_coverage(row, search_terms)
            instance_score = float(
                bool(primary.instance_id)
                and row.get("instance_id") == primary.instance_id
            )
            logger_score = float(bool(item.logger) and row.get("logger") == item.logger)
            execution_score = max(
                float(bool(primary.thread_id) and row.get("thread_id") == primary.thread_id),
                float(bool(primary.task_id) and row.get("task_id") == primary.task_id),
            )
            metadata_score = max(
                float(bool(item.event_name) and row.get("event_name") == item.event_name),
                float(bool(item.error_code) and row.get("event_code") == item.error_code),
                float(_row_matches_plan(row, item)),
            )
            score = min(
                1.0,
                0.32
                + 0.25 * lexical_score
                + 0.25 * time_score
                + 0.08 * instance_score
                + 0.05 * logger_score
                + 0.03 * execution_score
                + 0.02 * metadata_score,
            )
            scored.append((score, row))
        scored.sort(
            key=lambda pair: (
                -pair[0],
                float(pair[1].get("bm25_rank") or 0.0),
                abs(int(pair[1].get("timestamp_ns") or 0) - int(primary.timestamp_ns or 0)),
                str(pair[1].get("observation_id") or ""),
            )
        )
        return scored

    def _select_correlation_proof(
        self, anchors: list[dict[str, Any]]
    ) -> CorrelationProof | None:
        if not anchors or anchors[0]["score"] < self.confidence_threshold:
            return None
        best = anchors[0]
        runner_up = next(
            (
                candidate
                for candidate in anchors[1:]
                if candidate["identity"].strongest != best["identity"].strongest
            ),
            None,
        )
        margin = best["score"] - (runner_up["score"] if runner_up else 0.0)
        if runner_up is not None and margin < self.ambiguity_margin:
            return None
        return CorrelationProof(
            anchor_observation_id=str(best["row"]["observation_id"]),
            identity=best["identity"],
            score=best["score"],
            runner_up_margin=max(0.0, min(1.0, margin)),
            factors={"combined_score": best["score"]},
        )

    def _pack_results(
        self, raw_results: list[dict[str, Any]]
    ) -> tuple[tuple[EvidenceResult, ...], int]:
        remaining_tokens = self.max_total_tokens
        remaining_logs = self.max_observations
        packed: list[EvidenceResult] = []
        for result_rank, raw_result in enumerate(raw_results):
            item: FrozenEvidenceLog = raw_result["item"]
            observations: list[EvidenceObservation] = []
            for row in raw_result["rows"]:
                if remaining_logs <= 0 or remaining_tokens <= 0:
                    break
                cap = (
                    self.full_log_tokens
                    if result_rank < self.full_priority_count
                    else self.tail_log_tokens
                )
                allowed = min(cap, remaining_tokens)
                body = str(row.get("body") or "")
                included, ranges, original_tokens = _truncate_log(
                    body, max_tokens=allowed, match_pattern=item.template_pattern
                )
                used = _estimate_tokens(included)
                observations.append(
                    EvidenceObservation(
                        observation_id=str(row.get("observation_id") or "unknown"),
                        timestamp_ns=max(0, int(row.get("timestamp_ns") or 0)),
                        service=str(row.get("service_name") or item.service),
                        level=str(row.get("severity_text") or "UNKNOWN"),
                        body=included,
                        raw_ref="sqlite-log://" + str(row.get("observation_id") or "unknown"),
                        raw_sha256=sha256(
                            str(row.get("raw_json") or body).encode("utf-8")
                        ).hexdigest(),
                        match_mode=raw_result["mode"],
                        score=(
                            float(row["_evidence_score"])
                            if row.get("_evidence_score") is not None
                            else None
                        ),
                        extracted_identity=_extract_identity(row, item),
                        truncated=included != body,
                        original_tokens=original_tokens,
                        included_ranges=ranges,
                    )
                )
                remaining_tokens -= used
                remaining_logs -= 1
            status = raw_result["status"]
            reason = raw_result["reason"]
            if raw_result["rows"] and not observations:
                status = EvidenceMatchStatus.CONTEXT_OMITTED
                reason = "matching observations omitted because the context budget is exhausted"
            elif len(observations) < len(raw_result["rows"]):
                reason = "additional matching observations omitted by the context budget"
            packed.append(
                EvidenceResult(
                    evidence_id=item.evidence_id,
                    priority=item.priority,
                    question=item.question,
                    source=item.source,
                    expected_presence=item.expected_presence,
                    status=status,
                    collection_complete=raw_result["collection_complete"],
                    observations=tuple(observations),
                    reason=reason,
                )
            )
        return tuple(packed), self.max_total_tokens - remaining_tokens

    @staticmethod
    def _unavailable_bundle(
        *, primary_signal: PrimarySignal, plan: EvidenceLogPlan, reason: str
    ) -> DiagnosisEvidenceBundle:
        return DiagnosisEvidenceBundle(
            primary_signal_digest=primary_signal.digest,
            plan_digest=plan.digest,
            collection_complete=False,
            results=tuple(
                EvidenceResult(
                    evidence_id=item.evidence_id,
                    priority=item.priority,
                    question=item.question,
                    source=item.source,
                    expected_presence=item.expected_presence,
                    status=EvidenceMatchStatus.COLLECTION_INCOMPLETE,
                    collection_complete=False,
                    reason=reason,
                )
                for item in plan.evidence_logs
            ),
            total_tokens=0,
        )


def _canonical_digest(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _template_is_source_backed(template: str, source: str) -> bool:
    normalized_template = _normalize_space(template)
    normalized_source = _normalize_space(source)
    if normalized_template in normalized_source:
        return True
    fragments = [
        _normalize_space(part)
        for part in _DYNAMIC_PART.split(template)
        if part and not _DYNAMIC_PART.fullmatch(part) and _normalize_space(part)
    ]
    meaningful = [fragment for fragment in fragments if len(fragment) >= 3]
    if not meaningful or sum(map(len, meaningful)) < max(4, len(template) // 3):
        return False
    position = 0
    for fragment in meaningful:
        found = normalized_source.find(fragment, position)
        if found < 0:
            return False
        position = found + len(fragment)
    return True


def _is_log_emission_site(
    *, source: Path, source_text: str, snippet: str, start_line: int, end_line: int
) -> bool:
    log_names = {
        "critical",
        "debug",
        "emit",
        "error",
        "event",
        "exception",
        "fatal",
        "info",
        "log",
        "record",
        "trace",
        "warn",
        "warning",
    }
    if source.suffix == ".py":
        try:
            tree = ast.parse(source_text)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            node_end = getattr(node, "end_lineno", node.lineno)
            if node_end < start_line or node.lineno > end_line:
                continue
            function = node.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id
                if isinstance(function, ast.Name)
                else ""
            )
            if name.lower() in log_names:
                return True
        return False
    return re.search(
        r"(?i)\b(?:console|log|logger|logging|trace|tracing|event|emit|record)"
        r"[A-Za-z0-9_.:-]*\s*(?:\.|\()",
        snippet,
    ) is not None


def _compile_template_pattern(template: str) -> str:
    pieces: list[str] = []
    position = 0
    for match in _DYNAMIC_PART.finditer(template):
        pieces.append(re.escape(template[position : match.start()]))
        token = match.group(0)
        if token == r"\n":
            pieces.append(r"\s*")
        elif token == r"\t":
            pieces.append(r"\s+")
        else:
            pieces.append(r".+?")
        position = match.end()
    pieces.append(re.escape(template[position:]))
    pattern = "".join(pieces)
    pattern = pattern.replace(r"\ ", r"\s+")
    try:
        re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error as exc:
        raise EvidencePlanningError("cannot compile source log template") from exc
    return pattern


def _time_bounds(timestamp_ns: int, item: FrozenEvidenceLog) -> tuple[int, int]:
    delta = item.max_time_delta_seconds * 1_000_000_000
    if item.relation is TemporalRelation.BEFORE_PRIMARY:
        return max(0, timestamp_ns - delta), timestamp_ns
    if item.relation is TemporalRelation.AFTER_PRIMARY:
        return timestamp_ns, timestamp_ns + delta
    return max(0, timestamp_ns - delta), timestamp_ns + delta


def _search_terms(item: FrozenEvidenceLog) -> tuple[str, ...]:
    static_template = _DYNAMIC_PART.sub(" ", item.template)
    values = [static_template, item.event_name or "", item.error_code or "", item.logger or ""]
    terms: list[str] = []
    for value in values:
        for match in _SEARCH_TERM.findall(value):
            term = match.lower().strip("._:-/")
            if len(term) >= 2 and term not in _STOP_TERMS and term not in terms:
                terms.append(term)
    return tuple(terms[:32])


def _row_matches_plan(row: dict[str, Any], item: FrozenEvidenceLog) -> bool:
    if item.event_name and row.get("event_name") == item.event_name:
        return True
    if item.error_code and row.get("event_code") == item.error_code:
        return True
    return _row_matches_template(row, item)


def _row_matches_template(row: dict[str, Any], item: FrozenEvidenceLog) -> bool:
    searchable = "\n".join(
        str(row.get(field) or "") for field in ("body", "message_template")
    )
    return re.search(
        item.template_pattern, searchable, re.IGNORECASE | re.DOTALL
    ) is not None


def _term_coverage(row: dict[str, Any], terms: tuple[str, ...]) -> float:
    if not terms:
        return 0.0
    searchable = "\n".join(
        str(row.get(field) or "")
        for field in (
            "body",
            "message_template",
            "event_name",
            "error_type",
            "event_code",
            "logger",
        )
    ).casefold()
    matched = sum(term.casefold() in searchable for term in terms)
    return matched / len(terms)


def _extract_identity(
    row: dict[str, Any], item: FrozenEvidenceLog
) -> CorrelationIdentity:
    values: dict[str, str | None] = {}
    searchable = "\n".join(
        str(row.get(field) or "")
        for field in ("body", "attributes_json", "resource_json", "raw_json")
    )[:1_000_000]
    for field in ("trace_id", "request_id", "run_id", "session_id", "erp"):
        direct = row.get(field)
        if direct:
            values[field] = str(direct)
        elif field in item.extract_fields and field in item.extract_patterns:
            match = re.search(item.extract_patterns[field], searchable)
            if match:
                values[field] = (
                    match.groupdict().get("value")
                    if "value" in match.groupdict()
                    else match.group(1)
                )
        else:
            values[field] = None
    return CorrelationIdentity(**values)


def _absent_status(collection_complete: bool) -> EvidenceMatchStatus:
    return (
        EvidenceMatchStatus.NOT_FOUND
        if collection_complete
        else EvidenceMatchStatus.COLLECTION_INCOMPLETE
    )


def _absence_reason(collection_complete: bool) -> str:
    if collection_complete:
        return "no matching observation in the bounded query window"
    return "collection is incomplete; absence is not evidence that the event did not occur"


def _estimate_tokens(value: str) -> int:
    # Without binding the runtime to one model tokenizer, UTF-8 bytes are a
    # conservative upper bound for modern byte-backed tokenizers.  Under-counting
    # here would let an Agent-proposed evidence plan overflow the diagnosis prompt.
    return len(value.encode("utf-8"))


def _truncate_log(
    body: str, *, max_tokens: int, match_pattern: str
) -> tuple[str, tuple[tuple[int, int], ...], int]:
    original_tokens = _estimate_tokens(body)
    if original_tokens <= max_tokens:
        return body, ((0, len(body)),), original_tokens
    if not body or max_tokens <= 0:
        return "", (), original_tokens

    separator = "\n…[truncated]…\n"
    anchors: list[tuple[str, int, int]] = []
    match = re.search(match_pattern, body, re.IGNORECASE | re.DOTALL)
    if match:
        anchors.append(("center", match.start(), match.end()))
    caused = re.search(r"(?im)^.*caused by:.*$", body)
    if caused:
        anchors.append(("center", caused.start(), caused.end()))
    anchors.extend((("start", 0, 0), ("end", len(body), len(body))))
    unique: list[tuple[str, int, int]] = []
    seen: set[tuple[str, int, int]] = set()
    for anchor in anchors:
        if anchor not in seen:
            unique.append(anchor)
            seen.add(anchor)

    separator_bytes = _estimate_tokens(separator)
    while len(unique) > 1 and (
        separator_bytes * (len(unique) - 1) + len(unique) > max_tokens
    ):
        unique.pop()
    content_budget = max_tokens - separator_bytes * (len(unique) - 1)
    per_range = max(1, content_budget // len(unique))
    candidate_ranges: list[tuple[int, int]] = []
    for kind, anchor_start, anchor_end in unique:
        if kind == "start":
            candidate_ranges.append(
                (0, _utf8_prefix_end(body, 0, per_range))
            )
        elif kind == "end":
            candidate_ranges.append(
                (_utf8_suffix_start(body, len(body), per_range), len(body))
            )
        else:
            candidate_ranges.append(
                _utf8_range_around(body, anchor_start, anchor_end, per_range)
            )
    ranges = _merge_ranges(candidate_ranges)
    rendered = separator.join(body[start:end] for start, end in ranges)
    if not rendered or _estimate_tokens(rendered) > max_tokens:
        end = _utf8_prefix_end(body, 0, max_tokens)
        rendered = body[:end]
        ranges = ((0, end),) if end else ()
    return rendered, ranges, original_tokens


def _utf8_prefix_end(value: str, start: int, byte_budget: int) -> int:
    used = 0
    index = start
    while index < len(value):
        width = len(value[index].encode("utf-8"))
        if used + width > byte_budget:
            break
        used += width
        index += 1
    return index


def _utf8_suffix_start(value: str, end: int, byte_budget: int) -> int:
    used = 0
    index = end
    while index > 0:
        width = len(value[index - 1].encode("utf-8"))
        if used + width > byte_budget:
            break
        used += width
        index -= 1
    return index


def _utf8_range_around(
    value: str, start: int, end: int, byte_budget: int
) -> tuple[int, int]:
    if end < start:
        start, end = end, start
    core = value[start:end]
    if _estimate_tokens(core) >= byte_budget:
        return start, _utf8_prefix_end(value, start, byte_budget)
    remaining = byte_budget - _estimate_tokens(core)
    left_budget = remaining // 2
    range_start = _utf8_suffix_start(value, start, left_budget)
    left_used = _estimate_tokens(value[range_start:start])
    range_end = _utf8_prefix_end(
        value, end, remaining - left_used
    )
    return range_start, range_end


def _merge_ranges(ranges) -> tuple[tuple[int, int], ...]:
    merged: list[list[int]] = []
    # Anchors are collected by importance (template, cause, head, tail), not by
    # source position. Sort before merging so an early range is not swallowed by
    # a later one and the rendered excerpts retain source order.
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


__all__ = [
    "DiagnosisEvidencePlanner",
    "DiagnosisEvidenceRetriever",
    "EVIDENCE_PLANNER_AGENT_TYPE",
    "EvidencePlanFreezer",
    "EvidencePlanningError",
]
