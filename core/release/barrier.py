"""Read-only, last-moment replay evidence validation for release."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from core.verification.models import ReplayEvidenceManifest


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def verify_live_replay_barriers(
    database: str | Path,
    *,
    run_id: str,
    cycle: int,
    manifest: ReplayEvidenceManifest,
) -> None:
    """Re-read the sealed OTLP windows without creating or mutating the DB."""

    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise ValueError("observability database 不存在")
    expected = {
        (item.scenario_id, item.variant.value): item for item in manifest.windows
    }
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValueError(f"无法只读打开 observability database: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        windows = connection.execute(
            "SELECT * FROM execution_windows WHERE run_id = ? AND cycle = ?",
            (run_id, cycle),
        ).fetchall()
        actual = {(row["scenario_id"], row["variant"]): row for row in windows}
        if len(actual) != len(windows) or set(actual) != set(expected):
            raise ValueError("execution window 与 receipt manifest 覆盖不一致")

        for key, binding in expected.items():
            window = actual[key]
            fields = {
                "input_digest": binding.input_digest,
                "collection_id": binding.collection_id,
                "oracle_digest": binding.oracle_digest,
                "result_sha256": binding.result_sha256,
            }
            mismatches = [
                name for name, value in fields.items() if window[name] != value
            ]
            if mismatches:
                raise ValueError(
                    f"{key}: execution window 摘要漂移: {', '.join(mismatches)}"
                )

            barriers = connection.execute(
                "SELECT * FROM otlp_flush_barriers WHERE collection_id = ?",
                (binding.collection_id,),
            ).fetchall()
            if len(barriers) != 1:
                raise ValueError(f"{key}: OTLP barrier 缺失或不唯一")
            barrier = barriers[0]
            barrier_fields = {
                "collection_id": binding.collection_id,
                "run_id": run_id,
                "cycle": cycle,
                "scenario_id": binding.scenario_id,
                "variant": binding.variant.value,
                "input_digest": binding.input_digest,
            }
            if any(barrier[name] != value for name, value in barrier_fields.items()):
                raise ValueError(f"{key}: OTLP barrier 绑定漂移")
            try:
                signals = json.loads(barrier["signals_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{key}: OTLP barrier signals 非法") from exc
            if (
                signals != ["logs", "traces"]
                or barrier["timed_out"]
                or int(barrier["late_arrival_count"])
                or barrier["flush_started_at_ns"] < window["started_at_ns"]
                or barrier["flush_completed_at_ns"] < barrier["flush_started_at_ns"]
                or barrier["flush_completed_at_ns"] > barrier["deadline_ns"]
                or barrier["received_at_ns"] > barrier["deadline_ns"]
            ):
                raise ValueError(f"{key}: OTLP barrier 已失效")

            watermark = int(barrier["watermark_sequence"])
            clauses = (
                "collection_id = ? AND run_id = ? AND cycle = ? "
                "AND scenario_id = ? AND variant = ? AND input_digest = ?"
            )
            params = (
                binding.collection_id,
                run_id,
                cycle,
                binding.scenario_id,
                binding.variant.value,
                binding.input_digest,
            )
            trace = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN ingest_sequence > ? THEN 1 ELSE 0 END) AS late "
                "FROM trace_spans WHERE " + clauses,
                (watermark, *params),
            ).fetchone()
            logs = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN ingest_sequence > ? THEN 1 ELSE 0 END) AS late "
                "FROM log_records WHERE source = 'otlp' AND " + clauses,
                (watermark, *params),
            ).fetchone()
            if (
                not trace["total"]
                or not logs["total"]
                or int(trace["late"] or 0)
                or int(logs["late"] or 0)
            ):
                raise ValueError(f"{key}: OTLP watermark 后数据不完整或存在晚到")

            payload = {
                name: barrier[name]
                for name in (
                    "flush_id",
                    "collection_id",
                    "run_id",
                    "cycle",
                    "scenario_id",
                    "variant",
                    "input_digest",
                    "signals_json",
                    "flush_started_at_ns",
                    "flush_completed_at_ns",
                    "deadline_ns",
                    "received_at_ns",
                    "watermark_sequence",
                    "timed_out",
                    "late_arrival_count",
                )
            }
            digest = sha256(_json(payload).encode("utf-8")).hexdigest()
            if digest != binding.otlp_barrier_digest:
                raise ValueError(f"{key}: OTLP barrier digest 已变化")
    except sqlite3.Error as exc:
        raise ValueError(f"observability database 复核失败: {exc}") from exc
    finally:
        connection.close()


__all__ = ["verify_live_replay_barriers"]
