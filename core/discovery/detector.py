"""Versioned, deterministic detection rules (no LLM in the detection path).

A ``RuleSet`` has a version and an ordered list of rules; the first matching rule
wins. Rules match on the log record ``kind`` and optional regex predicates over the
payload's error type / message / tool name. Rules are data — the DEFAULT_RULESET
below is the built-in Agent/MCP band, and a RuleSet can equally be built from config
dicts (future versioned YAML) without code changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from core.connectors.logs import LogRecord

from .contracts import Detection, Eligibility, Severity


def _payload_error_type(payload: dict[str, Any]) -> str | None:
    for key in ("error_type", "type", "exception", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _payload_message(payload: dict[str, Any]) -> str:
    for key in ("message", "error", "reason", "detail", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _payload_tool(payload: dict[str, Any]) -> str | None:
    for key in ("tool", "tool_name", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@dataclass(frozen=True)
class DetectionRule:
    name: str
    kinds: tuple[str, ...]
    severity: Severity
    eligibility: Eligibility
    error_type_pattern: str | None = None
    message_pattern: str | None = None
    tool_pattern: str | None = None

    def matches(self, record: LogRecord) -> bool:
        if record.kind not in self.kinds:
            return False
        payload = record.payload
        if self.error_type_pattern is not None:
            etype = _payload_error_type(payload) or ""
            if not re.search(self.error_type_pattern, etype, re.IGNORECASE):
                return False
        if self.message_pattern is not None:
            if not re.search(self.message_pattern, _payload_message(payload), re.IGNORECASE):
                return False
        if self.tool_pattern is not None:
            tool = _payload_tool(payload) or ""
            if not re.search(self.tool_pattern, tool, re.IGNORECASE):
                return False
        return True


@dataclass(frozen=True)
class RuleSet:
    version: str
    rules: tuple[DetectionRule, ...] = field(default_factory=tuple)

    def detect(self, record: LogRecord) -> Detection | None:
        for rule in self.rules:
            if rule.matches(record):
                return Detection(
                    matched_rule=rule.name,
                    rule_version=self.version,
                    severity=rule.severity,
                    eligibility=rule.eligibility,
                    error_type=_payload_error_type(record.payload),
                    message=_payload_message(record.payload)[:2000],
                )
        return None


# Built-in Agent/MCP detection band. Order matters (first match wins): the specific
# MCP-timeout rule precedes the generic run_error catch.
DEFAULT_RULESET = RuleSet(
    version="agent-mcp/v1",
    rules=(
        DetectionRule(
            name="mcp.timeout.no_fallback",
            kinds=("provider_error", "tool_exec_end", "run_error"),
            severity=Severity.HIGH,
            eligibility=Eligibility.AUTO_FIX_ELIGIBLE,
            error_type_pattern=r"timeout|timederror|deadline",
        ),
        DetectionRule(
            name="mcp.tool_input_malformed",
            kinds=("tool_input_malformed",),
            severity=Severity.MEDIUM,
            eligibility=Eligibility.AUTO_FIX_ELIGIBLE,
        ),
        DetectionRule(
            name="provider.error",
            kinds=("provider_error",),
            severity=Severity.HIGH,
            eligibility=Eligibility.DIAGNOSE_ONLY,
        ),
        DetectionRule(
            name="run.uncaught_error",
            kinds=("run_error",),
            severity=Severity.HIGH,
            eligibility=Eligibility.AUTO_FIX_ELIGIBLE,
        ),
    ),
)


__all__ = ["DEFAULT_RULESET", "DetectionRule", "RuleSet"]
