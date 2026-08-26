"""Verification evidence providers backed by the local observability database."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

from core.verification.models import (
    BehaviorEvidence,
    BehaviorObservation,
    LogEvidence,
    LogObservation,
    ScenarioCollection,
    TraceEvidence,
    TraceObservation,
    Variant,
)
from core.verification.providers import EvidenceCollectionContext

from .store import LocalObservabilityStore


def _window_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["skill_digests"] = json.loads(result.pop("skill_digests_json"))
    if result.get("payload_json") is not None:
        result["payload"] = json.loads(result["payload_json"])
    if result.get("input_json") is not None:
        result["input_payload"] = json.loads(result["input_json"])
    else:
        result["input_payload_missing"] = True
    if result.get("tool_calls_json") is not None:
        result["tool_calls"] = tuple(json.loads(result["tool_calls_json"]))
    result["collection_complete"] = bool(result["collection_complete"])
    if result.get("finished") is not None:
        result["finished"] = bool(result["finished"])
    return result


def _validate_window_binding(
    window: dict[str, Any], context: EvidenceCollectionContext
) -> str | None:
    expected = {
        "run_id": context.run_id,
        "cycle": context.cycle,
        "control_ref": context.control_ref,
        "control_digest": context.control_digest,
        "candidate_ref": context.candidate_ref,
        "candidate_digest": context.candidate_digest,
        "policy_digest": context.policy_digest,
        "skill_digests": context.skill_digests,
    }
    mismatches = [key for key, value in expected.items() if window.get(key) != value]
    if mismatches:
        return "execution window 绑定不一致: " + ", ".join(mismatches)
    if not window["collection_complete"]:
        return "execution window 尚未完整关闭"
    if window.get("input_payload_missing"):
        return "execution window 缺少可重算的冻结输入"
    return None


class _SQLiteProviderBase:
    def __init__(self, database: str):
        self.store = LocalObservabilityStore(database)

    def _windows(
        self, context: EvidenceCollectionContext
    ) -> tuple[dict[tuple[str, str], dict[str, Any]], str | None]:
        rows = [
            _window_dict(row)
            for row in self.store.execution_windows(context.run_id, context.cycle)
        ]
        windows = {(row["scenario_id"], row["variant"]): row for row in rows}
        if len(windows) != len(rows):
            return windows, "execution window 不唯一"
        manifest = {
            (item.scenario_id, item.variant.value): item
            for item in context.replay_manifest.windows
        }
        required = set(manifest)
        actual = set(windows)
        if actual != required:
            return windows, (
                f"execution window 覆盖不完整: missing={sorted(required - actual)}, "
                f"extra={sorted(actual - required)}"
            )
        for key, window in windows.items():
            binding = manifest[key]
            digest_fields = {
                "collection_id": binding.collection_id,
                "input_digest": binding.input_digest,
                "oracle_digest": binding.oracle_digest,
                "result_sha256": binding.result_sha256,
            }
            mismatches = [
                name for name, expected in digest_fields.items()
                if window.get(name) != expected
            ]
            if mismatches:
                return windows, (
                    f"{key}: execution window 与 replay receipt manifest 不一致: "
                    + ", ".join(mismatches)
                )
            error = _validate_window_binding(window, context)
            if error:
                return windows, f"{key}: {error}"
            try:
                barrier_digest = self.store.otlp_barrier_digest(window)
            except Exception as exc:
                return windows, f"{key}: {exc}"
            if barrier_digest != binding.otlp_barrier_digest:
                return windows, f"{key}: OTLP barrier digest 与 replay receipt 不一致"
        return windows, None

    def _trace_rows(
        self, window: dict[str, Any]
    ) -> tuple[str | None, list[sqlite3.Row], str | None]:
        rows = self.store.trace_rows_for_window(window)
        trace_ids = {row["trace_id"] for row in rows}
        if window.get("trace_id"):
            if not rows:
                return None, [], "execution window 指定的 trace_id 没有 Span"
            if trace_ids != {window["trace_id"]}:
                return None, rows, "execution window 混入其他 trace_id"
            trace_id = window["trace_id"]
        elif len(trace_ids) != 1:
            return None, rows, f"无法唯一确定 trace_id: {sorted(trace_ids)}"
        else:
            trace_id = next(iter(trace_ids))
        expected = {
            "run_id": window["run_id"],
            "cycle": window["cycle"],
            "scenario_id": window["scenario_id"],
            "variant": window["variant"],
            "input_digest": window["input_digest"],
            "collection_id": window["collection_id"],
        }
        mismatches = {
            key
            for row in rows
            for key, value in expected.items()
            if row[key] != value
        }
        if mismatches:
            return None, rows, (
                "Trace resource attributes 与执行窗口不一致: "
                + ", ".join(sorted(mismatches))
            )
        return trace_id, rows, None


def _span_attributes(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["attributes_json"])


def _span_is_error(row: sqlite3.Row) -> bool:
    attributes = _span_attributes(row)
    status = str(row["status_code"] or "").upper()
    events = json.loads(row["events_json"])
    return (
        status in {"2", "STATUS_CODE_ERROR", "ERROR"}
        or attributes.get("success") is False
        or bool(attributes.get("error"))
        or any(
            str(event.get("name", "")).lower() == "exception"
            for event in events
            if isinstance(event, dict)
        )
    )


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _as_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _explicit_fallback(attributes: dict[str, Any]) -> bool | None:
    for key in (
        "fallback_used",
        "fallback.used",
        "did_fall_back",
        "didFallBackToNonStreaming",
    ):
        parsed = _as_bool(attributes.get(key))
        if parsed is not None:
            return parsed
    return None


def _trace_metrics(
    trace_id: str,
    rows: list[sqlite3.Row],
    log_rows: list[sqlite3.Row],
    window: dict[str, Any],
) -> TraceObservation:
    api_requests: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    for row in log_rows:
        attributes = json.loads(row["attributes_json"])
        if row["source"] == "otlp" and row["event_name"] == "api_request":
            api_requests.append((row, attributes))
    api_requests.sort(
        key=lambda item: (item[0]["timestamp_ns"], item[0]["observation_id"])
    )
    models: list[str] = []
    input_tokens: list[int] = []
    fallback_values: list[bool] = []
    model_complete = bool(api_requests)
    tokens_complete = bool(api_requests)
    fallback_complete = bool(api_requests)
    for _, attributes in api_requests:
        model = attributes.get("model")
        if isinstance(model, str) and model.strip():
            models.append(model)
        else:
            model_complete = False

        components = [
            _as_nonnegative_int(attributes.get(name))
            for name in (
                "input_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
            )
        ]
        if all(item is not None for item in components):
            input_tokens.append(sum(item for item in components if item is not None))
        else:
            tokens_complete = False

        fallback = _explicit_fallback(attributes)
        if fallback is None:
            fallback_complete = False
        else:
            fallback_values.append(fallback)

    unique_models = tuple(dict.fromkeys(models))
    actual_model = None
    if model_complete:
        actual_model = (
            unique_models[0]
            if len(unique_models) == 1
            else "MULTIPLE:" + ",".join(unique_models)
        )
    sessions = {row["session_id"] for row in rows if row["session_id"]}
    session_id = window.get("session_id")
    if session_id is None and len(sessions) == 1:
        session_id = next(iter(sessions))

    explicit_finished: bool | None = None
    for row in rows:
        attrs = _span_attributes(row)
        for name in ("agent.finished", "finished", "claude_code.finished"):
            if isinstance(attrs.get(name), bool):
                explicit_finished = attrs[name]
    finished = window.get("finished")
    if finished is None:
        finished = explicit_finished

    return TraceObservation(
        trace_id=trace_id,
        request_id=window.get("request_id"),
        session_id=session_id,
        scenario_id=window["scenario_id"],
        input_digest=window["input_digest"],
        error_observations=(
            sum(_span_is_error(row) for row in rows)
            + sum(
                row["severity_text"].upper() in {"ERROR", "FATAL"}
                for row in log_rows
            )
        ),
        actual_model=actual_model,
        fallback_used=any(fallback_values) if fallback_complete else None,
        input_tokens=max(input_tokens) if tokens_complete else None,
        finished=finished,
    )


class SQLiteTraceEvidenceProvider(_SQLiteProviderBase):
    """Build the article's four Trace metrics from CCB OTLP spans."""

    async def collect_trace(
        self, context: EvidenceCollectionContext
    ) -> TraceEvidence:
        return await asyncio.to_thread(self._collect_trace_sync, context)

    def _collect_trace_sync(
        self, context: EvidenceCollectionContext
    ) -> TraceEvidence:
        windows, window_error = self._windows(context)
        observations: list[TraceObservation] = []
        errors: list[str] = []
        if window_error:
            errors.append(window_error)
        for scenario_id in context.scenario_ids:
            window = windows.get((scenario_id, "candidate"))
            if not window:
                continue
            trace_id, rows, trace_error = self._trace_rows(window)
            if trace_error or trace_id is None:
                errors.append(f"{scenario_id}: {trace_error}")
                continue
            log_rows = self.store.log_rows_for_window(window)
            observations.append(_trace_metrics(trace_id, rows, log_rows, window))
        return TraceEvidence(
            run_id=context.run_id,
            cycle=context.cycle,
            candidate_ref=context.candidate_ref,
            candidate_digest=context.candidate_digest,
            policy_digest=context.policy_digest,
            skill_digests=context.skill_digests,
            collection_complete=not errors,
            observations=tuple(observations),
            collector_error="; ".join(errors) if errors else None,
        )


class SQLiteLogEvidenceProvider(_SQLiteProviderBase):
    """Collect complete control/candidate log windows from SQLite."""

    async def collect_logs(
        self, context: EvidenceCollectionContext
    ) -> LogEvidence:
        return await asyncio.to_thread(self._collect_logs_sync, context)

    def _collect_logs_sync(self, context: EvidenceCollectionContext) -> LogEvidence:
        windows, window_error = self._windows(context)
        errors = [window_error] if window_error else []
        observations: list[LogObservation] = []
        collections: list[ScenarioCollection] = []
        for scenario_id in context.scenario_ids:
            for variant_name in ("control", "candidate"):
                window = windows.get((scenario_id, variant_name))
                if not window:
                    continue
                variant = Variant(variant_name)
                collections.append(
                    ScenarioCollection(
                        scenario_id=scenario_id,
                        variant=variant,
                        collection_id=window["collection_id"],
                        input_digest=window["input_digest"],
                    )
                )
                for row in self.store.log_rows_for_window(window):
                    if row["severity_text"].upper() not in {"ERROR", "FATAL"}:
                        continue
                    trace_id = row["trace_id"] or window.get("trace_id")
                    request_id = window.get("request_id")
                    session_id = row["session_id"] or window.get("session_id")
                    if not trace_id and not request_id and not session_id:
                        errors.append(
                            f"{scenario_id}/{variant_name}: 日志缺少关联身份"
                        )
                        continue
                    observations.append(
                        LogObservation(
                            observation_id=row["observation_id"],
                            scenario_id=scenario_id,
                            variant=variant,
                            service=row["service_name"],
                            level=row["severity_text"],
                            error_type=row["error_type"] or "UnclassifiedError",
                            event_code=row["event_code"],
                            message_template=row["message_template"],
                            business_frame=row["business_frame"],
                            request_id=request_id,
                            trace_id=trace_id,
                            session_id=session_id,
                        )
                    )
        return LogEvidence(
            run_id=context.run_id,
            cycle=context.cycle,
            control_ref=context.control_ref,
            control_digest=context.control_digest,
            candidate_ref=context.candidate_ref,
            candidate_digest=context.candidate_digest,
            policy_digest=context.policy_digest,
            skill_digests=context.skill_digests,
            collection_complete=not errors,
            collected_variants=(Variant.CONTROL, Variant.CANDIDATE),
            collected_scenarios=tuple(collections),
            observations=tuple(observations),
            collector_error="; ".join(error for error in errors if error) or None,
        )


class SQLiteBehaviorEvidenceProvider(_SQLiteProviderBase):
    """Read machine-recorded scenario outcomes; never infer payload equality from logs."""

    async def collect_behavior(
        self, context: EvidenceCollectionContext
    ) -> BehaviorEvidence:
        return await asyncio.to_thread(self._collect_behavior_sync, context)

    def _collect_behavior_sync(
        self, context: EvidenceCollectionContext
    ) -> BehaviorEvidence:
        windows, window_error = self._windows(context)
        errors = [window_error] if window_error else []
        observations: list[BehaviorObservation] = []
        collections: list[ScenarioCollection] = []
        for scenario_id in context.scenario_ids:
            for variant_name in ("control", "candidate"):
                window = windows.get((scenario_id, variant_name))
                if not window:
                    continue
                variant = Variant(variant_name)
                collections.append(
                    ScenarioCollection(
                        scenario_id=scenario_id,
                        variant=variant,
                        collection_id=window["collection_id"],
                        input_digest=window["input_digest"],
                    )
                )
                trace_id, rows, trace_error = self._trace_rows(window)
                missing = [
                    name
                    for name in ("outcome", "payload", "model", "tool_calls", "finished")
                    if window.get(name) is None
                ]
                if trace_error:
                    errors.append(f"{scenario_id}/{variant_name}: {trace_error}")
                if missing:
                    errors.append(
                        f"{scenario_id}/{variant_name}: 缺少行为字段 {', '.join(missing)}"
                    )
                if trace_error or trace_id is None or missing:
                    continue
                sessions = {row["session_id"] for row in rows if row["session_id"]}
                session_id = window.get("session_id")
                if session_id is None and len(sessions) == 1:
                    session_id = next(iter(sessions))
                if not window.get("request_id") and not session_id:
                    errors.append(
                        f"{scenario_id}/{variant_name}: 缺少 request_id/session_id"
                    )
                    continue
                observations.append(
                    BehaviorObservation(
                        observation_id=window["collection_id"],
                        scenario_id=scenario_id,
                        variant=variant,
                        outcome=window["outcome"],
                        payload=window["payload"],
                        input_digest=window["input_digest"],
                        model=window["model"],
                        tool_calls=window["tool_calls"],
                        finished=window["finished"],
                        request_id=window.get("request_id"),
                        trace_id=trace_id,
                        session_id=session_id,
                    )
                )
        return BehaviorEvidence(
            run_id=context.run_id,
            cycle=context.cycle,
            control_ref=context.control_ref,
            control_digest=context.control_digest,
            candidate_ref=context.candidate_ref,
            candidate_digest=context.candidate_digest,
            policy_digest=context.policy_digest,
            skill_digests=context.skill_digests,
            collection_complete=not errors,
            collected_variants=(Variant.CONTROL, Variant.CANDIDATE),
            collected_scenarios=tuple(collections),
            observations=tuple(observations),
            collector_error="; ".join(error for error in errors if error) or None,
        )


__all__ = [
    "SQLiteBehaviorEvidenceProvider",
    "SQLiteLogEvidenceProvider",
    "SQLiteTraceEvidenceProvider",
]
