"""Untrusted structured output of the read-only Diagnosis stage.

The diagnosis agent produces a ``DiagnosisProposal`` — deliberately lenient because
it is agent-authored. The trusted ``IncidentFreezer``
(``core.stages.diagnosis.freezer``) validates it against the frozen control ref and
the trusted rule resolver before minting an ``IncidentBundle`` for later stages.
Analysis is an internal step of diagnosis, not a separate artifact.
"""

from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Any, Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from .base import Contract


class ProposedSourceLocation(Contract):
    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)
    revision: str = Field(min_length=1)


class ProposedFailureSignature(Contract):
    code: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
    error_type: str | None = Field(default=None, min_length=1, max_length=255)
    message_pattern: str | None = Field(default=None, min_length=1, max_length=512)
    event_code: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("message_pattern")
    @classmethod
    def _safe_message_pattern(cls, value: str | None) -> str | None:
        if value is None:
            return None
        compiled = re.compile(value)
        if compiled.search("") is not None:
            raise ValueError("message_pattern cannot match an empty string")
        return value

    @model_validator(mode="after")
    def _has_matcher(self):
        if not any((self.error_type, self.message_pattern, self.event_code)):
            raise ValueError("failure_signature requires a concrete matcher")
        return self


class DiagnosisReproducerSpec(Contract):
    """Machine-executable control reproducer proposed during diagnosis.

    The proposal is still untrusted.  ``IncidentFreezer`` content-binds it and
    ``CommandControlReproducer`` executes it through the trusted sandboxed runner.
    Inline interpreters and shells are deliberately excluded: the command must
    point at an existing repository test/harness or a named package task.
    """

    schema_version: Literal["diagnosis-reproducer/v1"] = "diagnosis-reproducer/v1"
    id: str = Field(
        default="diagnosis-control-reproducer",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    argv: tuple[str, ...] = Field(min_length=1, max_length=128)
    cwd: str = "."
    timeout_ms: StrictInt = Field(default=120_000, ge=100, le=900_000)
    expected_exit_code: StrictInt = Field(default=0, ge=-255, le=255)
    stdout_contains: tuple[str, ...] = ()
    stderr_contains: tuple[str, ...] = ()

    @field_validator("argv")
    @classmethod
    def _safe_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not token or "\x00" in token for token in value):
            raise ValueError("reproducer argv cannot contain blank tokens or NUL")
        inline_flags = {
            "python": {"-c"},
            "python3": {"-c"},
            "node": {"-e", "--eval", "-p", "--print"},
            "ruby": {"-e"},
            "perl": {"-e", "-E"},
            "bun": {"-e", "--eval", "-p", "--print"},
        }
        shells = {"bash", "dash", "fish", "sh", "zsh"}

        def is_inline_flag(argument: str, flags: set[str]) -> bool:
            return any(
                argument == flag
                or argument.startswith(flag + "=")
                or (
                    flag.startswith("-")
                    and not flag.startswith("--")
                    and argument.startswith(flag)
                    and len(argument) > len(flag)
                )
                for flag in flags
            )

        for index, token in enumerate(value):
            executable = PurePosixPath(token).name.lower()
            if executable in shells:
                raise ValueError("reproducer cannot use a shell entrypoint or wrapper")
            flags = inline_flags.get(executable)
            if flags and any(is_inline_flag(arg, flags) for arg in value[index + 1 :]):
                raise ValueError("reproducer cannot execute inline program text")
            if executable == "deno" and any(
                arg.casefold() in {"eval", "repl"} for arg in value[index + 1 :]
            ):
                raise ValueError("reproducer cannot execute inline program text")
            wrapper_args = value[index + 1 :]
            npm_exec = executable == "npm" and any(
                arg.casefold() in {"exec", "x"} for arg in wrapper_args
            )
            if (executable == "npx" or npm_exec) and any(
                is_inline_flag(arg, {"-c", "--call"}) for arg in wrapper_args
            ):
                raise ValueError("reproducer cannot execute an inline wrapper command")
        return value

    @field_validator("cwd")
    @classmethod
    def _safe_cwd(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError("reproducer cwd must stay inside the control workspace")
        return value

    @field_validator("stdout_contains", "stderr_contains")
    @classmethod
    def _non_blank_markers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError("reproducer output markers must be non-empty and unique")
        return value


class DiagnosisProposal(Contract):
    schema_version: Literal["diagnosis-proposal/v1"] = "diagnosis-proposal/v1"
    symptom_summary: str = Field(min_length=1)
    affected_components: tuple[str, ...] = ()
    risk_tags: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    confirmed_facts: tuple[str, ...] = ()
    counterevidence: tuple[str, ...] = ()
    source_locations: tuple[ProposedSourceLocation, ...] = ()
    root_cause: str = Field(min_length=1)
    reproducer: DiagnosisReproducerSpec | None = None
    original_input: Any = None
    failure_signature: ProposedFailureSignature
    missing_evidence: tuple[str, ...] = ()
    unresolved_unknowns: tuple[str, ...] = ()
    evidence_bundle_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


__all__ = [
    "DiagnosisProposal",
    "DiagnosisReproducerSpec",
    "ProposedFailureSignature",
    "ProposedSourceLocation",
]
