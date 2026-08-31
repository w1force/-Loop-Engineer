"""Host-side semantic oracle for raw replay captures.

The replayed image may emit observations, but it is never allowed to declare its
own outcome, matched incident signatures, or behavior payload.  Those values are
derived here, in the coordinator process, from a strict raw response/log schema.
"""

from __future__ import annotations

import json
import math
import re
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from .workflow import FailureSignature


HOST_REPLAY_ORACLE_DIGEST = sha256(Path(__file__).read_bytes()).hexdigest()


class ReplayOracleError(RuntimeError):
    """Raw replay evidence cannot be interpreted safely."""


class _OracleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _validate_json(value: Any, path: str = "$") -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError(f"{path} contains a non-finite float")
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json(child, f"{path}[{index}]")
        return value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{path} object keys must be strings")
        for key, child in value.items():
            _validate_json(child, f"{path}.{key}")
        return value
    raise ValueError(f"{path} is not a JSON value")


class RawReplayResponse(_OracleModel):
    """Uninterpreted response captured by the frozen replay harness."""

    status_code: StrictInt = Field(ge=100, le=599)
    body: Any
    error_type: str | None = Field(default=None, min_length=1, max_length=255)
    message: str | None = Field(default=None, min_length=1, max_length=16_384)
    event_code: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("body")
    @classmethod
    def _json_body(cls, value: Any) -> Any:
        return _validate_json(value)


class RawReplayLog(_OracleModel):
    """One raw structured diagnostic captured by the replay harness."""

    level: Literal["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
    message: str = Field(min_length=1, max_length=16_384)
    error_type: str | None = Field(default=None, min_length=1, max_length=255)
    event_code: str | None = Field(default=None, min_length=1, max_length=255)


class ReplayOracleDecision(_OracleModel):
    """Semantic result computed outside the replayed image."""

    outcome: Literal["success", "failure"]
    failure_signatures: tuple[str, ...]
    payload: Any

    @field_validator("payload")
    @classmethod
    def _json_payload(cls, value: Any) -> Any:
        return _validate_json(value)

    @property
    def digest(self) -> str:
        return sha256(
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()


def _matches(signature: FailureSignature, diagnostic: RawReplayLog) -> bool:
    """Require every configured matcher to match the same raw diagnostic."""

    if signature.error_type is not None and diagnostic.error_type != signature.error_type:
        return False
    if (
        signature.message_pattern is not None
        and re.search(signature.message_pattern, diagnostic.message) is None
    ):
        return False
    if signature.event_code is not None and diagnostic.event_code != signature.event_code:
        return False
    return True


class HostReplayOracle:
    """Pure host-side evaluator; candidate-authored semantic verdicts are impossible."""

    @staticmethod
    def evaluate(
        *,
        response: RawReplayResponse,
        logs: tuple[RawReplayLog, ...],
        signatures: tuple[FailureSignature, ...] = (),
    ) -> ReplayOracleDecision:
        diagnostics = list(logs)
        if any(
            value is not None
            for value in (response.error_type, response.message, response.event_code)
        ):
            diagnostics.append(
                RawReplayLog(
                    level=(
                        "ERROR"
                        if response.status_code < 200 or response.status_code >= 300
                        else "INFO"
                    ),
                    message=response.message or json.dumps(
                        response.body,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    error_type=response.error_type,
                    event_code=response.event_code,
                )
            )

        error_logs = tuple(
            item for item in diagnostics if item.level in {"ERROR", "FATAL"}
        )
        outcome: Literal["success", "failure"] = (
            "success"
            if 200 <= response.status_code < 300 and not error_logs
            else "failure"
        )
        matched = tuple(
            signature.code
            for signature in signatures
            if any(_matches(signature, item) for item in error_logs)
        )
        return ReplayOracleDecision(
            outcome=outcome,
            failure_signatures=matched,
            payload=response.body,
        )


__all__ = [
    "HOST_REPLAY_ORACLE_DIGEST",
    "HostReplayOracle",
    "RawReplayLog",
    "RawReplayResponse",
    "ReplayOracleDecision",
    "ReplayOracleError",
]
