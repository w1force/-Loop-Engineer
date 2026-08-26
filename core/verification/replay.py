"""Trusted Docker control/candidate replay for one frozen verification scenario.

The launcher owns Docker invocation, execution identity, input mounting and window
closure.  A caller can choose only schema-validated data; it cannot inject a custom
executor or a shell command.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Literal, Protocol, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from core.observability.store import (
    ExecutionWindow,
    LocalObservabilityStore,
    normalized_input_digest,
)

from .models import (
    ReplayEvidenceManifest,
    ReplayWindowBinding,
    ToolCallObservation,
    Variant,
)
from .replay_oracle import (
    HOST_REPLAY_ORACLE_DIGEST,
    HostReplayOracle,
    RawReplayLog,
    RawReplayResponse,
    ReplayOracleDecision,
)
from .runner import workspace_digest, workspace_manifest
from .workflow import FailureSignature


OCI_REVISION_LABEL = "org.opencontainers.image.revision"
WORKSPACE_DIGEST_LABEL = "io.loop-engineer.workspace-digest"
INPUT_PATH = "/loop-engineer/input/input.json"
RESULT_PATH = "/loop-engineer/output/result.json"
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_DOCKER_STREAM_BYTES = 4 * 1024 * 1024
MAX_DOCKERFILE_BYTES = 1024 * 1024
_DOCKER_READ_CHUNK_BYTES = 64 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9._:/-]*$")
_SHELLS = frozenset(
    {"bash", "cmd", "dash", "fish", "powershell", "pwsh", "sh", "zsh"}
)


class ReplayError(RuntimeError):
    """Replay could not produce trustworthy, complete machine evidence."""


class _DockerOutputLimitExceeded(RuntimeError):
    def __init__(self, stream: str, limit: int):
        super().__init__(f"Docker {stream} 超过 {limit} 字节硬上限")


class ReplayModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _validate_json_value(value: Any, path: str = "$") -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError(f"{path} 包含非有限浮点数")
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json_value(child, f"{path}[{index}]")
        return value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{path} 的对象键必须是字符串")
        for key, child in value.items():
            _validate_json_value(child, f"{path}.{key}")
        return value
    raise ValueError(f"{path} 不是 JSON 值")


def _canonical_json(value: Any) -> bytes:
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _valid_digest_map(value: dict[str, str]) -> dict[str, str]:
    if not value:
        raise ValueError("skill_digests 不能为空")
    for name, digest in value.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError("skill_digests 包含非法名称")
        if not _DIGEST.fullmatch(digest):
            raise ValueError("skill_digests 必须是 SHA-256")
    return value


class ReplayLimits(ReplayModel):
    timeout_ms: StrictInt = Field(default=300_000, ge=1_000, le=900_000)
    inspect_timeout_ms: StrictInt = Field(default=30_000, ge=1_000, le=120_000)
    otlp_barrier_timeout_ms: StrictInt = Field(default=30_000, ge=100, le=120_000)
    memory_bytes: StrictInt = Field(
        default=1_073_741_824, ge=134_217_728, le=17_179_869_184
    )
    cpus_millis: StrictInt = Field(default=1_000, ge=100, le=16_000)
    pids_limit: StrictInt = Field(default=256, ge=16, le=4096)
    tmpfs_bytes: StrictInt = Field(default=67_108_864, ge=1_048_576, le=1_073_741_824)
    network_mode: Literal["none", "bridge"] = "none"


class ReplayVariantSpec(ReplayModel):
    image: str = Field(min_length=1, max_length=512)
    source_ref: str = Field(min_length=1, max_length=512)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    command: tuple[str, ...] = Field(min_length=1, max_length=128)

    @field_validator("image")
    @classmethod
    def _digest_pinned_image(cls, value: str) -> str:
        if value.count("@") != 1:
            raise ValueError("Docker image 必须使用 name@sha256:<digest>")
        name, digest = value.rsplit("@", 1)
        if (
            not _IMAGE_NAME.fullmatch(name)
            or "//" in name
            or name.endswith(("/", ":"))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            raise ValueError("Docker image 必须使用合法的 name@sha256:<64hex>")
        return value

    @field_validator("source_ref")
    @classmethod
    def _safe_source_ref(cls, value: str) -> str:
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("source_ref 包含非法控制字符")
        return value

    @field_validator("command")
    @classmethod
    def _no_shell_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not token or "\x00" in token for token in value):
            raise ValueError("container command 不能包含空 token 或 NUL")
        if Path(value[0]).name.lower() in _SHELLS:
            raise ValueError("container command 禁止 shell 入口")
        return value


class ReplayVariantResolution(ReplayModel):
    """Trusted, per-plan image contracts returned by a variant resolver."""

    control: ReplayVariantSpec
    candidate: ReplayVariantSpec


class ReplayVariantResolver(Protocol):
    """Resolve digest-pinned replay images for one frozen verification plan."""

    async def resolve(
        self, plan: "VerificationPlan"
    ) -> ReplayVariantResolution: ...


class ReplayRequest(ReplayModel):
    schema_version: Literal["verification-replay-request/v1"] = (
        "verification-replay-request/v1"
    )
    run_id: str = Field(min_length=1, max_length=128)
    cycle: StrictInt = Field(ge=1, le=3)
    scenario_id: str = Field(min_length=1, max_length=256)
    input_payload: Any
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproducer: StrictBool = True
    failure_signature: FailureSignature | None = None
    expected_control_outcome: Literal["success", "failure"] | None = "failure"
    expected_candidate_outcome: Literal["success"] = "success"
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_digests: dict[str, str] = Field(min_length=1)
    control: ReplayVariantSpec
    candidate: ReplayVariantSpec
    limits: ReplayLimits = Field(default_factory=ReplayLimits)

    @field_validator("run_id", "scenario_id")
    @classmethod
    def _safe_identity(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("run_id/scenario_id 包含非法字符")
        return value

    @field_validator("input_payload")
    @classmethod
    def _json_input(cls, value: Any) -> Any:
        return _validate_json_value(value)

    @field_validator("skill_digests")
    @classmethod
    def _skill_digest_map(cls, value: dict[str, str]) -> dict[str, str]:
        return _valid_digest_map(value)

    @model_validator(mode="after")
    def _input_is_frozen(self) -> Self:
        if normalized_input_digest(self.input_payload) != self.input_digest:
            raise ValueError("input_payload 与冻结 input_digest 不一致")
        if self.control.image == self.candidate.image:
            raise ValueError("control 与 candidate 必须使用不同的 digest-pinned image")
        if self.reproducer and (
            self.expected_control_outcome != "failure"
            or self.failure_signature is None
        ):
            raise ValueError("reproducer 必须声明 control=failure 和 failure_signature")
        return self

    @classmethod
    def from_plan(
        cls,
        plan: "VerificationPlan",
        *,
        control: ReplayVariantSpec,
        candidate: ReplayVariantSpec,
        scenario_id: str | None = None,
        limits: ReplayLimits | None = None,
    ) -> "ReplayRequest":
        """Build a replay request without re-declaring any frozen plan field."""

        from .workflow import VerificationPlan

        frozen = VerificationPlan.model_validate_json(plan.model_dump_json())
        if control.source_ref != frozen.control_ref or control.source_digest != frozen.control_digest:
            raise ValueError("control image contract 与 VerificationPlan 不一致")
        if (
            candidate.source_ref != frozen.candidate_ref
            or candidate.source_digest != frozen.candidate_digest
        ):
            raise ValueError("candidate image contract 与 VerificationPlan 不一致")
        reproductions = list(frozen.reproductions)
        if scenario_id is not None:
            reproductions = [
                item for item in reproductions if item.scenario_id == scenario_id
            ]
        else:
            reproductions = [item for item in reproductions if item.reproducer]
        if len(reproductions) != 1:
            raise ValueError("from_plan 必须唯一选中一个冻结场景")
        reproduction = reproductions[0]
        return cls(
            run_id=frozen.run_id,
            cycle=frozen.cycle,
            scenario_id=reproduction.scenario_id,
            input_payload=reproduction.input_payload,
            input_digest=reproduction.input_digest,
            reproducer=reproduction.reproducer,
            failure_signature=reproduction.failure_signature,
            expected_control_outcome=reproduction.expected_control_outcome,
            expected_candidate_outcome=reproduction.expected_candidate_outcome,
            plan_digest=frozen.digest,
            policy_digest=frozen.policy_digest,
            skill_digests=dict(frozen.skill_digests),
            control=control,
            candidate=candidate,
            limits=limits or ReplayLimits(),
        )


class ReplayResult(ReplayModel):
    """Raw capture contract; it deliberately contains no semantic verdict fields."""

    schema_version: Literal["verification-replay-result/v2"] = (
        "verification-replay-result/v2"
    )
    run_id: str = Field(min_length=1)
    cycle: StrictInt = Field(ge=1, le=3)
    scenario_id: str = Field(min_length=1)
    collection_id: str = Field(min_length=1, max_length=512)
    variant: Variant
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_digests: dict[str, str] = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_response: RawReplayResponse
    raw_logs: tuple[RawReplayLog, ...] = ()
    request_id: str | None = Field(default=None, min_length=1)
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    session_id: str | None = Field(default=None, min_length=1)
    model: str = Field(min_length=1)
    tool_calls: tuple[ToolCallObservation, ...]

    @field_validator("skill_digests")
    @classmethod
    def _skill_digest_map(cls, value: dict[str, str]) -> dict[str, str]:
        return _valid_digest_map(value)

    @model_validator(mode="after")
    def _has_correlation_identity(self) -> Self:
        if not self.request_id and not self.session_id:
            raise ValueError("result 必须包含 request_id 或 session_id")
        return self


class VariantReplayReceipt(ReplayModel):
    variant: Variant
    source_ref: str
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    image: str
    image_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    image_config_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    launch_config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_id: str = Field(min_length=1)
    otlp_barrier_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    docker_stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    docker_stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exit_code: StrictInt
    finished: Literal[True]
    started_at_ns: StrictInt = Field(ge=0)
    ended_at_ns: StrictInt = Field(ge=0)
    oracle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    oracle_decision: ReplayOracleDecision
    outcome: Literal["success", "failure"]
    failure_signatures: tuple[str, ...]

    @model_validator(mode="after")
    def _time_order(self) -> Self:
        if self.ended_at_ns < self.started_at_ns:
            raise ValueError("replay receipt 结束时间早于开始时间")
        if self.oracle_digest != HOST_REPLAY_ORACLE_DIGEST:
            raise ValueError("replay receipt 未绑定当前 host-side semantic oracle")
        if self.outcome != self.oracle_decision.outcome:
            raise ValueError("outcome 未绑定 host-side oracle decision")
        if self.failure_signatures != self.oracle_decision.failure_signatures:
            raise ValueError("failure_signatures 未绑定 host-side oracle decision")
        return self


class ReplayReceipt(ReplayModel):
    schema_version: Literal["verification-replay-receipt/v1"] = (
        "verification-replay-receipt/v1"
    )
    run_id: str
    cycle: StrictInt = Field(ge=1, le=3)
    scenario_id: str
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproducer: StrictBool = True
    failure_signature: FailureSignature | None = None
    expected_control_outcome: Literal["success", "failure"] | None = "failure"
    expected_candidate_outcome: Literal["success"] = "success"
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_digests: dict[str, str] = Field(min_length=1)
    control_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_ref: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    control: VariantReplayReceipt
    candidate: VariantReplayReceipt
    passed: StrictBool
    status: Literal["passed", "rejected"]
    failures: tuple[str, ...] = ()

    @field_validator("skill_digests")
    @classmethod
    def _skill_digest_map(cls, value: dict[str, str]) -> dict[str, str]:
        return _valid_digest_map(value)

    @model_validator(mode="after")
    def _consistent_status(self) -> Self:
        if self.control.variant is not Variant.CONTROL:
            raise ValueError("control receipt variant 不正确")
        if self.candidate.variant is not Variant.CANDIDATE:
            raise ValueError("candidate receipt variant 不正确")
        if self.control.input_digest != self.input_digest or self.candidate.input_digest != self.input_digest:
            raise ValueError("receipt 未绑定同一冻结输入")
        if self.control.source_digest != self.control_digest:
            raise ValueError("control_digest 与 control receipt 不一致")
        if self.candidate.source_ref != self.candidate_ref:
            raise ValueError("candidate_ref 与 candidate receipt 不一致")
        if self.candidate.source_digest != self.candidate_digest:
            raise ValueError("candidate_digest 与 candidate receipt 不一致")
        expected_failures: list[str] = []
        if (
            self.expected_control_outcome is not None
            and self.control.outcome != self.expected_control_outcome
        ):
            expected_failures.append(
                f"control outcome={self.control.outcome}, "
                f"expected={self.expected_control_outcome}"
            )
        if self.candidate.outcome != self.expected_candidate_outcome:
            expected_failures.append(
                f"candidate outcome={self.candidate.outcome}, "
                f"expected={self.expected_candidate_outcome}"
            )
        if self.reproducer:
            if self.failure_signature is None:
                raise ValueError("reproducer receipt 缺少 failure_signature")
            if self.failure_signature.code not in self.control.failure_signatures:
                expected_failures.append("control 未匹配冻结 failure_signature")
            if self.failure_signature.code in self.candidate.failure_signatures:
                expected_failures.append("candidate 仍包含原始 failure_signature")
        if self.candidate.failure_signatures:
            expected_failures.append("candidate 成功结果仍声明 failure signature")
        if self.failures != tuple(expected_failures):
            raise ValueError("replay failures 未由原始回放结果重算")
        if (self.status == "passed") != (not self.failures):
            raise ValueError("replay status 与 failures 不一致")
        if self.passed != (self.status == "passed"):
            raise ValueError("replay passed 与 status 不一致")
        return self

    @property
    def digest(self) -> str:
        return sha256(_canonical_json(self.model_dump(mode="json"))).hexdigest()


class ReplayBatchReceipt(ReplayModel):
    """All frozen scenarios for one plan; consumed by the Coordinator."""

    schema_version: Literal["verification-replay-batch/v1"] = (
        "verification-replay-batch/v1"
    )
    run_id: str
    cycle: StrictInt = Field(ge=1, le=3)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_ref: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_digests: dict[str, str] = Field(min_length=1)
    scenario_receipts: tuple[ReplayReceipt, ...] = Field(min_length=1)
    passed: StrictBool
    failures: tuple[str, ...] = ()

    @field_validator("skill_digests")
    @classmethod
    def _skill_digest_map(cls, value: dict[str, str]) -> dict[str, str]:
        return _valid_digest_map(value)

    @model_validator(mode="after")
    def _recompute_batch(self) -> Self:
        scenario_ids = [item.scenario_id for item in self.scenario_receipts]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("batch replay scenario_id 不能重复")
        bindings = (
            "run_id",
            "cycle",
            "plan_digest",
            "control_digest",
            "candidate_ref",
            "candidate_digest",
            "policy_digest",
            "skill_digests",
        )
        for receipt in self.scenario_receipts:
            mismatches = [
                name
                for name in bindings
                if getattr(receipt, name) != getattr(self, name)
            ]
            if mismatches:
                raise ValueError(
                    "scenario receipt 与 batch 绑定不一致: " + ", ".join(mismatches)
                )
        expected = tuple(
            f"{receipt.scenario_id}: {failure}"
            for receipt in self.scenario_receipts
            for failure in receipt.failures
        )
        if self.failures != expected or self.passed != (not expected):
            raise ValueError("batch replay verdict 未从 scenario receipts 重算")
        return self

    @property
    def digest(self) -> str:
        return sha256(_canonical_json(self.model_dump(mode="json"))).hexdigest()

    @property
    def replay_manifest(self) -> ReplayEvidenceManifest:
        return ReplayEvidenceManifest(
            windows=tuple(
                ReplayWindowBinding(
                    scenario_id=receipt.scenario_id,
                    variant=variant.variant,
                    input_digest=variant.input_digest,
                    collection_id=variant.collection_id,
                    otlp_barrier_digest=variant.otlp_barrier_digest,
                    oracle_digest=variant.oracle_digest,
                    result_sha256=variant.result_sha256,
                )
                for receipt in self.scenario_receipts
                for variant in (receipt.control, receipt.candidate)
            )
        )


@dataclass(frozen=True)
class _DockerResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class _InspectedImage:
    image: str
    manifest_digest: str
    config_digest: str
    revision: str
    workspace_digest: str


def _docker_environment() -> dict[str, str]:
    inherited = (
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
    )
    return {key: os.environ[key] for key in inherited if key in os.environ}


def _run_docker(argv: tuple[str, ...], *, timeout_seconds: float) -> _DockerResult:
    """Single non-shell Docker process boundary, monkeypatchable by focused tests."""

    if not argv or argv[0] != "docker":
        raise ValueError("_run_docker 只允许 docker argv")
    if timeout_seconds <= 0:
        raise ValueError("Docker timeout 必须大于零")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_docker_environment(),
        shell=False,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise RuntimeError("Docker 子进程未创建输出管道")

    captured = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout_seconds
    try:
        for stream_name, stream in (
            ("stdout", process.stdout),
            ("stderr", process.stderr),
        ):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, stream_name)

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(
                    argv,
                    timeout_seconds,
                    output=bytes(captured["stdout"]),
                    stderr=bytes(captured["stderr"]),
                )
            events = selector.select(remaining)
            if not events:
                continue
            for key, _ in events:
                stream_name = key.data
                buffer = captured[stream_name]
                remaining_capacity = MAX_DOCKER_STREAM_BYTES - len(buffer)
                read_size = min(
                    _DOCKER_READ_CHUNK_BYTES,
                    remaining_capacity + 1,
                )
                try:
                    chunk = os.read(key.fd, max(read_size, 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(chunk) > remaining_capacity:
                    buffer.extend(chunk[:remaining_capacity])
                    raise _DockerOutputLimitExceeded(
                        stream_name, MAX_DOCKER_STREAM_BYTES
                    )
                buffer.extend(chunk)

        remaining = deadline - time.monotonic()
        returncode = process.wait(timeout=max(remaining, 0))
        return _DockerResult(
            returncode=returncode,
            stdout=bytes(captured["stdout"]),
            stderr=bytes(captured["stderr"]),
        )
    except BaseException:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _strict_json_loads(raw: bytes) -> Any:
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复 JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError(f"非法 JSON 常量: {value}")

    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=no_duplicates,
            parse_constant=invalid_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReplayError(f"无法解析严格 JSON: {exc}") from exc


def _read_regular_file(path: Path, limit: int = MAX_RESULT_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReplayError(f"result.json 不存在、不可读或是符号链接: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReplayError("result.json 必须是普通文件")
        if before.st_size > limit:
            raise ReplayError(f"result.json 超过 {limit} 字节")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > limit:
            raise ReplayError(f"result.json 超过 {limit} 字节")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ReplayError("result.json 在读取期间发生变化")
        return raw
    finally:
        os.close(descriptor)


def _stderr_text(result: _DockerResult) -> str:
    return result.stderr.decode("utf-8", errors="replace").strip()[:2000]


class ReplayLauncher:
    """Launch digest-pinned variants and close SQLite execution windows."""

    def __init__(self, database: str | Path):
        self.store = LocalObservabilityStore(database)

    def replay(self, request: ReplayRequest) -> ReplayReceipt:
        request = ReplayRequest.model_validate_json(request.model_dump_json())
        control_image = self._inspect(
            request.control, timeout_ms=request.limits.inspect_timeout_ms
        )
        candidate_image = self._inspect(
            request.candidate, timeout_ms=request.limits.inspect_timeout_ms
        )

        canonical_input = _canonical_json(request.input_payload)
        if sha256(canonical_input).hexdigest() != request.input_digest:
            raise ReplayError("冻结输入在 launcher 内重算不一致")

        with tempfile.TemporaryDirectory(prefix="loop-replay-") as directory:
            root = Path(directory)
            input_path = root / "input.json"
            input_path.write_bytes(canonical_input)
            input_path.chmod(0o400)
            control = self._execute(
                request,
                Variant.CONTROL,
                request.control,
                control_image,
                input_path,
                root / "control-output",
            )
            candidate = self._execute(
                request,
                Variant.CANDIDATE,
                request.candidate,
                candidate_image,
                input_path,
                root / "candidate-output",
            )

        failures: list[str] = []
        if (
            request.expected_control_outcome is not None
            and control.outcome != request.expected_control_outcome
        ):
            failures.append(
                f"control outcome={control.outcome}, "
                f"expected={request.expected_control_outcome}"
            )
        if candidate.outcome != request.expected_candidate_outcome:
            failures.append(
                f"candidate outcome={candidate.outcome}, "
                f"expected={request.expected_candidate_outcome}"
            )
        if request.reproducer:
            assert request.failure_signature is not None
            if request.failure_signature.code not in control.failure_signatures:
                failures.append("control 未匹配冻结 failure_signature")
            if request.failure_signature.code in candidate.failure_signatures:
                failures.append("candidate 仍包含原始 failure_signature")
        if candidate.failure_signatures:
            failures.append("candidate 成功结果仍声明 failure signature")

        return ReplayReceipt(
            run_id=request.run_id,
            cycle=request.cycle,
            scenario_id=request.scenario_id,
            input_digest=request.input_digest,
            reproducer=request.reproducer,
            failure_signature=request.failure_signature,
            expected_control_outcome=request.expected_control_outcome,
            expected_candidate_outcome=request.expected_candidate_outcome,
            plan_digest=request.plan_digest,
            policy_digest=request.policy_digest,
            skill_digests=dict(request.skill_digests),
            control_digest=request.control.source_digest,
            candidate_ref=request.candidate.source_ref,
            candidate_digest=request.candidate.source_digest,
            control=control,
            candidate=candidate,
            passed=not failures,
            status="passed" if not failures else "rejected",
            failures=tuple(failures),
        )

    @staticmethod
    def _inspect(spec: ReplayVariantSpec, *, timeout_ms: int) -> _InspectedImage:
        try:
            result = _run_docker(
                ("docker", "image", "inspect", spec.image),
                timeout_seconds=timeout_ms / 1000,
            )
        except (OSError, subprocess.TimeoutExpired, _DockerOutputLimitExceeded) as exc:
            raise ReplayError(f"Docker image inspect 失败: {type(exc).__name__}: {exc}") from exc
        if result.returncode != 0:
            raise ReplayError(
                f"Docker image inspect 失败(exit={result.returncode}): {_stderr_text(result)}"
            )
        payload = _strict_json_loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise ReplayError("Docker image inspect 必须返回唯一对象")
        image = payload[0]
        config_digest = image.get("Id")
        if not isinstance(config_digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", config_digest
        ):
            raise ReplayError("Docker image inspect 缺少合法 image config digest")
        repo_digests = image.get("RepoDigests")
        if not isinstance(repo_digests, list) or spec.image not in repo_digests:
            raise ReplayError("Docker image inspect 未绑定请求的 manifest digest")
        config = image.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict):
            raise ReplayError("Docker image 缺少受信 OCI labels")
        if labels.get(OCI_REVISION_LABEL) != spec.source_ref:
            raise ReplayError("OCI revision label 与冻结 source_ref 不一致")
        if labels.get(WORKSPACE_DIGEST_LABEL) != spec.source_digest:
            raise ReplayError("workspace-digest label 与冻结 source_digest 不一致")
        return _InspectedImage(
            image=spec.image,
            manifest_digest=spec.image.rsplit("@", 1)[1],
            config_digest=config_digest,
            revision=spec.source_ref,
            workspace_digest=spec.source_digest,
        )

    def _execute(
        self,
        request: ReplayRequest,
        variant: Variant,
        spec: ReplayVariantSpec,
        image: _InspectedImage,
        input_path: Path,
        output_directory: Path,
    ) -> VariantReplayReceipt:
        output_directory.mkdir(mode=0o700)
        result_path = output_directory / "result.json"
        collection_id = f"{request.run_id}:{request.cycle}:{request.scenario_id}:{variant.value}:{uuid4().hex}"
        name_seed = f"{collection_id}:{image.manifest_digest}".encode("utf-8")
        container_name = f"loop-replay-{variant.value}-{sha256(name_seed).hexdigest()[:20]}"
        environment = {
            "LOOP_ENGINEER_VERIFICATION_RUN_ID": request.run_id,
            "LOOP_ENGINEER_VERIFICATION_CYCLE": str(request.cycle),
            "LOOP_ENGINEER_VERIFICATION_SCENARIO_ID": request.scenario_id,
            "LOOP_ENGINEER_VERIFICATION_VARIANT": variant.value,
            "LOOP_ENGINEER_VERIFICATION_COLLECTION_ID": collection_id,
            "LOOP_ENGINEER_VERIFICATION_INPUT_DIGEST": request.input_digest,
            "LOOP_ENGINEER_VERIFICATION_PLAN_DIGEST": request.plan_digest,
            "LOOP_ENGINEER_VERIFICATION_POLICY_DIGEST": request.policy_digest,
            "LOOP_ENGINEER_VERIFICATION_SKILL_DIGESTS": _canonical_json(
                request.skill_digests
            ).decode("utf-8"),
            "LOOP_ENGINEER_VERIFICATION_SOURCE_REF": spec.source_ref,
            "LOOP_ENGINEER_VERIFICATION_SOURCE_DIGEST": spec.source_digest,
            "LOOP_ENGINEER_VERIFICATION_INPUT": INPUT_PATH,
            "LOOP_ENGINEER_VERIFICATION_RESULT": RESULT_PATH,
        }
        user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "65534:65534"
        security_contract = {
            "read_only": True,
            "cap_drop": "ALL",
            "no_new_privileges": True,
            "network_mode": request.limits.network_mode,
            "memory_bytes": request.limits.memory_bytes,
            "cpus_millis": request.limits.cpus_millis,
            "pids_limit": request.limits.pids_limit,
            "tmpfs_bytes": request.limits.tmpfs_bytes,
            "timeout_ms": request.limits.timeout_ms,
            "user": user,
            "input_path": INPUT_PATH,
            "result_path": RESULT_PATH,
        }
        launch_contract = {
            "variant": variant.value,
            "image": image.image,
            "image_manifest_digest": image.manifest_digest,
            "image_config_digest": image.config_digest,
            "source_ref": spec.source_ref,
            "source_digest": spec.source_digest,
            "command": list(spec.command),
            "environment": environment,
            "security": security_contract,
        }
        launch_config_digest = sha256(_canonical_json(launch_contract)).hexdigest()
        argv: list[str] = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--network",
            request.limits.network_mode,
            "--pids-limit",
            str(request.limits.pids_limit),
            "--memory",
            str(request.limits.memory_bytes),
            "--cpus",
            f"{request.limits.cpus_millis / 1000:.3f}",
            "--user",
            user,
            "--init",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={request.limits.tmpfs_bytes}",
            "--mount",
            f"type=bind,src={input_path},dst={INPUT_PATH},readonly",
            "--mount",
            f"type=bind,src={output_directory},dst=/loop-engineer/output",
            "--label",
            f"io.loop-engineer.run-id={request.run_id}",
            "--label",
            f"io.loop-engineer.scenario-id={request.scenario_id}",
            "--label",
            f"io.loop-engineer.variant={variant.value}",
        ]
        for key, value in sorted(environment.items()):
            argv.extend(("--env", f"{key}={value}"))
        argv.extend(("--entrypoint", spec.command[0], spec.image, *spec.command[1:]))

        started_at_ns = time.time_ns()
        result: _DockerResult | None = None
        invocation_error: Exception | None = None
        try:
            result = _run_docker(
                tuple(argv), timeout_seconds=request.limits.timeout_ms / 1000
            )
        except Exception as exc:
            invocation_error = exc
        ended_at_ns = time.time_ns()

        cleanup_error: ReplayError | None = None
        try:
            self._remove_and_confirm(container_name, request.limits.inspect_timeout_ms)
        except ReplayError as exc:
            cleanup_error = exc

        if (
            cleanup_error is not None
            or invocation_error is not None
            or result is None
            or result.returncode != 0
        ):
            self._record_incomplete(
                request,
                variant,
                spec,
                collection_id,
                started_at_ns,
                ended_at_ns,
            )
            if cleanup_error is not None:
                raise ReplayError(
                    f"{variant.value} Docker replay 后无法确认容器已删除: "
                    f"{cleanup_error}"
                ) from cleanup_error
            if invocation_error is not None:
                raise ReplayError(
                    f"{variant.value} Docker replay 失败: "
                    f"{type(invocation_error).__name__}: {invocation_error}"
                ) from invocation_error
            assert result is not None
            raise ReplayError(
                f"{variant.value} Docker replay 未完整结束(exit={result.returncode}): "
                f"{_stderr_text(result)}"
            )

        try:
            raw_result = _read_regular_file(result_path)
            parsed = ReplayResult.model_validate(_strict_json_loads(raw_result))
            self._validate_result_binding(
                request, variant, spec, collection_id, parsed
            )
            oracle = HostReplayOracle.evaluate(
                response=parsed.raw_response,
                logs=parsed.raw_logs,
                signatures=(
                    (request.failure_signature,)
                    if request.failure_signature is not None
                    else ()
                ),
            )
        except Exception as exc:
            self._record_incomplete(
                request,
                variant,
                spec,
                collection_id,
                started_at_ns,
                ended_at_ns,
            )
            if isinstance(exc, ReplayError):
                raise
            raise ReplayError(f"{variant.value} result.json schema 非法: {exc}") from exc
        self.store.record_execution(
            ExecutionWindow(
                run_id=request.run_id,
                cycle=request.cycle,
                scenario_id=request.scenario_id,
                variant=variant.value,
                input_digest=request.input_digest,
                input_payload=request.input_payload,
                collection_id=collection_id,
                control_ref=request.control.source_ref,
                control_digest=request.control.source_digest,
                candidate_ref=request.candidate.source_ref,
                candidate_digest=request.candidate.source_digest,
                policy_digest=request.policy_digest,
                skill_digests=request.skill_digests,
                started_at_ns=started_at_ns,
                ended_at_ns=ended_at_ns,
                collection_complete=True,
                trace_id=parsed.trace_id,
                request_id=parsed.request_id,
                session_id=parsed.session_id,
                finished=True,
                outcome=oracle.outcome,
                payload=oracle.payload,
                model=parsed.model,
                tool_calls=tuple(
                    item.model_dump(mode="python") for item in parsed.tool_calls
                ),
                oracle_digest=HOST_REPLAY_ORACLE_DIGEST,
                result_sha256=sha256(raw_result).hexdigest(),
            )
        )
        otlp_barrier_digest = self._close_otlp_barrier(
            request, variant, collection_id
        )
        return VariantReplayReceipt(
            variant=variant,
            source_ref=spec.source_ref,
            source_digest=spec.source_digest,
            image=image.image,
            image_manifest_digest=image.manifest_digest,
            image_config_digest=image.config_digest,
            launch_config_digest=launch_config_digest,
            input_digest=request.input_digest,
            collection_id=collection_id,
            otlp_barrier_digest=otlp_barrier_digest,
            result_sha256=sha256(raw_result).hexdigest(),
            docker_stdout_sha256=sha256(result.stdout).hexdigest(),
            docker_stderr_sha256=sha256(result.stderr).hexdigest(),
            exit_code=result.returncode,
            finished=True,
            started_at_ns=started_at_ns,
            ended_at_ns=ended_at_ns,
            oracle_digest=HOST_REPLAY_ORACLE_DIGEST,
            oracle_decision=oracle,
            outcome=oracle.outcome,
            failure_signatures=oracle.failure_signatures,
        )

    def _close_otlp_barrier(
        self,
        request: ReplayRequest,
        variant: Variant,
        collection_id: str,
    ) -> str:
        try:
            self.store.wait_for_otlp_flush_barrier(
                collection_id,
                timeout_ms=request.limits.otlp_barrier_timeout_ms,
            )
            window_rows = [
                row
                for row in self.store.execution_windows(request.run_id, request.cycle)
                if row["collection_id"] == collection_id
            ]
            if len(window_rows) != 1:
                raise ReplayError("OTLP barrier 无法唯一绑定 execution window")
            return self.store.otlp_barrier_digest(window_rows[0])
        except Exception as exc:
            if isinstance(exc, ReplayError):
                raise
            raise ReplayError(
                f"{variant.value} OTLP flush/watermark barrier 无效: {exc}"
            ) from exc

    @staticmethod
    def _validate_result_binding(
        request: ReplayRequest,
        variant: Variant,
        spec: ReplayVariantSpec,
        collection_id: str,
        result: ReplayResult,
    ) -> None:
        expected = {
            "run_id": request.run_id,
            "cycle": request.cycle,
            "scenario_id": request.scenario_id,
            "collection_id": collection_id,
            "variant": variant,
            "input_digest": request.input_digest,
            "plan_digest": request.plan_digest,
            "policy_digest": request.policy_digest,
            "skill_digests": request.skill_digests,
            "source_ref": spec.source_ref,
            "source_digest": spec.source_digest,
        }
        mismatches = [
            name for name, value in expected.items() if getattr(result, name) != value
        ]
        if mismatches:
            raise ReplayError(
                f"{variant.value} result.json 与冻结回放契约不一致: "
                + ", ".join(mismatches)
            )

    def _record_incomplete(
        self,
        request: ReplayRequest,
        variant: Variant,
        spec: ReplayVariantSpec,
        collection_id: str,
        started_at_ns: int,
        ended_at_ns: int,
    ) -> None:
        try:
            self.store.record_execution(
                ExecutionWindow(
                    run_id=request.run_id,
                    cycle=request.cycle,
                    scenario_id=request.scenario_id,
                    variant=variant.value,
                    input_digest=request.input_digest,
                    input_payload=request.input_payload,
                    collection_id=collection_id,
                    control_ref=request.control.source_ref,
                    control_digest=request.control.source_digest,
                    candidate_ref=request.candidate.source_ref,
                    candidate_digest=request.candidate.source_digest,
                    policy_digest=request.policy_digest,
                    skill_digests=request.skill_digests,
                    started_at_ns=started_at_ns,
                    ended_at_ns=ended_at_ns,
                    collection_complete=False,
                    finished=False,
                )
            )
        except Exception as exc:
            raise ReplayError(
                f"{variant.value} replay 失败且无法持久化不完整窗口: {exc}"
            ) from exc

    @staticmethod
    def _remove_and_confirm(container_name: str, timeout_ms: int) -> None:
        removal_error: Exception | None = None
        try:
            removal = _run_docker(
                ("docker", "rm", "-f", container_name),
                timeout_seconds=timeout_ms / 1000,
            )
            if removal.returncode != 0:
                removal_error = ReplayError(
                    f"docker rm -f exit={removal.returncode}: {_stderr_text(removal)}"
                )
        except (OSError, subprocess.TimeoutExpired, _DockerOutputLimitExceeded) as exc:
            removal_error = exc

        try:
            probe = _run_docker(
                (
                    "docker",
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"name=^/{container_name}$",
                ),
                timeout_seconds=timeout_ms / 1000,
            )
        except (OSError, subprocess.TimeoutExpired, _DockerOutputLimitExceeded) as exc:
            detail = f"; rm={removal_error}" if removal_error is not None else ""
            raise ReplayError(f"容器删除状态查询失败: {exc}{detail}") from exc
        if probe.returncode != 0:
            detail = f"; rm={removal_error}" if removal_error is not None else ""
            raise ReplayError(
                f"容器删除状态查询失败(exit={probe.returncode}): "
                f"{_stderr_text(probe)}{detail}"
            )
        if probe.stdout.strip():
            detail = f"; rm={removal_error}" if removal_error is not None else ""
            raise ReplayError(f"容器仍然存在{detail}")


class DockerBuildReplayVariantResolver:
    """Build and resolve immutable replay images from plan-bound workspaces.

    The Dockerfile and image repositories are trusted constructor inputs.  A
    candidate plan may select only a workspace below ``candidate_workspace_root``;
    it cannot add Docker flags, labels, commands, or another Dockerfile.
    """

    def __init__(
        self,
        *,
        control_workspace: str | Path,
        candidate_workspace_root: str | Path,
        dockerfile: str,
        dockerfile_sha256: str,
        control_repository: str,
        candidate_repository: str,
        command: tuple[str, ...],
        build_timeout_ms: int = 900_000,
        inspect_timeout_ms: int = 120_000,
    ):
        self.control_workspace = self._existing_directory(
            control_workspace, "control_workspace"
        )
        self.candidate_workspace_root = self._existing_directory(
            candidate_workspace_root, "candidate_workspace_root"
        )
        self.dockerfile = self._relative_file(dockerfile)
        if not _DIGEST.fullmatch(dockerfile_sha256):
            raise ValueError("dockerfile_sha256 必须是 SHA-256")
        self.dockerfile_sha256 = dockerfile_sha256
        self.control_repository = self._repository(control_repository)
        self.candidate_repository = self._repository(candidate_repository)
        command_contract = ReplayVariantSpec(
            image="trusted/replay@sha256:" + "0" * 64,
            source_ref="trusted",
            source_digest="0" * 64,
            command=command,
        )
        self.command = command_contract.command
        self.build_timeout_ms = self._timeout(
            build_timeout_ms, "build_timeout_ms", maximum=3_600_000
        )
        self.inspect_timeout_ms = self._timeout(
            inspect_timeout_ms, "inspect_timeout_ms", maximum=300_000
        )

    async def resolve(self, plan: "VerificationPlan") -> ReplayVariantResolution:
        from .workflow import VerificationPlan

        frozen = VerificationPlan.model_validate_json(plan.model_dump_json())
        return await asyncio.to_thread(self._resolve_sync, frozen)

    def _resolve_sync(self, plan: "VerificationPlan") -> ReplayVariantResolution:
        candidate_workspace = self._candidate_workspace(plan.workspace)
        self._label_value(plan.control_ref, "control_ref")
        self._label_value(plan.candidate_ref, "candidate_ref")
        with tempfile.TemporaryDirectory(prefix="loop-replay-build-") as directory:
            root = Path(directory)
            control_context = self._snapshot_workspace(
                self.control_workspace,
                root / "control",
                expected_digest=plan.control_digest,
                ignore=plan.policy.workspace_ignore,
            )
            candidate_context = self._snapshot_workspace(
                candidate_workspace,
                root / "candidate",
                expected_digest=plan.candidate_digest,
                ignore=plan.policy.workspace_ignore,
            )
            control = self._build_variant(
                context=control_context,
                metadata_path=root / "control-metadata.json",
                repository=self.control_repository,
                source_ref=plan.control_ref,
                source_digest=plan.control_digest,
                variant=Variant.CONTROL,
            )
            candidate = self._build_variant(
                context=candidate_context,
                metadata_path=root / "candidate-metadata.json",
                repository=self.candidate_repository,
                source_ref=plan.candidate_ref,
                source_digest=plan.candidate_digest,
                variant=Variant.CANDIDATE,
            )
        return ReplayVariantResolution(control=control, candidate=candidate)

    def _build_variant(
        self,
        *,
        context: Path,
        metadata_path: Path,
        repository: str,
        source_ref: str,
        source_digest: str,
        variant: Variant,
    ) -> ReplayVariantSpec:
        dockerfile_path = context.joinpath(*self.dockerfile.parts)
        raw_dockerfile = _read_regular_file(
            dockerfile_path, limit=MAX_DOCKERFILE_BYTES
        )
        if sha256(raw_dockerfile).hexdigest() != self.dockerfile_sha256:
            raise ReplayError(
                f"{variant.value} Dockerfile 与受信 digest 不一致"
            )
        ignored_files = (
            context / ".dockerignore",
            dockerfile_path.with_name(dockerfile_path.name + ".dockerignore"),
        )
        if any(path.exists() or path.is_symlink() for path in ignored_files):
            raise ReplayError("replay build context 禁止 Docker ignore 文件")

        tag_contract = {
            "variant": variant.value,
            "source_ref": source_ref,
            "source_digest": source_digest,
            "dockerfile_sha256": self.dockerfile_sha256,
        }
        tag = f"{repository}:loop-{sha256(_canonical_json(tag_contract)).hexdigest()[:32]}"
        argv = (
            "docker",
            "buildx",
            "build",
            "--push",
            "--no-cache",
            "--network",
            "none",
            "--file",
            str(dockerfile_path),
            "--tag",
            tag,
            "--label",
            f"{OCI_REVISION_LABEL}={source_ref}",
            "--label",
            f"{WORKSPACE_DIGEST_LABEL}={source_digest}",
            "--metadata-file",
            str(metadata_path),
            str(context),
        )
        try:
            built = _run_docker(
                argv, timeout_seconds=self.build_timeout_ms / 1000
            )
        except (OSError, subprocess.TimeoutExpired, _DockerOutputLimitExceeded) as exc:
            raise ReplayError(
                f"{variant.value} Docker image build 失败: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if built.returncode != 0:
            raise ReplayError(
                f"{variant.value} Docker image build 失败(exit={built.returncode}): "
                f"{_stderr_text(built)}"
            )
        metadata = _strict_json_loads(
            _read_regular_file(metadata_path, limit=64 * 1024)
        )
        manifest_digest = (
            metadata.get("containerimage.digest")
            if isinstance(metadata, dict)
            else None
        )
        if not isinstance(manifest_digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", manifest_digest
        ):
            raise ReplayError("buildx metadata 缺少合法 containerimage.digest")
        spec = ReplayVariantSpec(
            image=f"{repository}@{manifest_digest}",
            source_ref=source_ref,
            source_digest=source_digest,
            command=self.command,
        )
        try:
            pulled = _run_docker(
                ("docker", "pull", spec.image),
                timeout_seconds=self.build_timeout_ms / 1000,
            )
        except (OSError, subprocess.TimeoutExpired, _DockerOutputLimitExceeded) as exc:
            raise ReplayError(
                f"{variant.value} digest-pinned Docker image pull 失败: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if pulled.returncode != 0:
            raise ReplayError(
                f"{variant.value} digest-pinned Docker image pull 失败"
                f"(exit={pulled.returncode}): {_stderr_text(pulled)}"
            )
        ReplayLauncher._inspect(spec, timeout_ms=self.inspect_timeout_ms)
        return spec

    @staticmethod
    def _existing_directory(path: str | Path, name: str) -> Path:
        raw = Path(path)
        if not raw.is_absolute():
            raise ValueError(f"{name} 必须是绝对路径")
        try:
            resolved = raw.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"{name} 不存在: {raw}") from exc
        if not resolved.is_dir():
            raise ValueError(f"{name} 必须是目录")
        return resolved

    @staticmethod
    def _relative_file(value: str) -> PurePosixPath:
        path = PurePosixPath(value)
        if (
            not value
            or "\\" in value
            or path.is_absolute()
            or ".." in path.parts
            or str(path) != value
            or value == "."
        ):
            raise ValueError("dockerfile 必须是规范化的 POSIX 相对路径")
        return path

    @staticmethod
    def _repository(value: str) -> str:
        final_component = value.rsplit("/", 1)[-1]
        if (
            not value
            or "@" in value
            or ":" in final_component
            or "//" in value
            or value.endswith("/")
            or not _IMAGE_NAME.fullmatch(value)
        ):
            raise ValueError("镜像仓库必须是不含 tag/digest 的合法小写名称")
        return value

    @staticmethod
    def _timeout(value: int, name: str, *, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} 必须是整数")
        if not 1_000 <= value <= maximum:
            raise ValueError(f"{name} 必须在 1000..{maximum} 范围内")
        return value

    @staticmethod
    def _label_value(value: str, name: str) -> None:
        if (
            value != value.strip()
            or not value
            or len(value) > 512
            or any(character in value for character in ("\x00", "\n", "\r"))
        ):
            raise ReplayError(f"{name} 不能安全写入 OCI label")

    def _candidate_workspace(self, value: str) -> Path:
        raw = Path(value)
        if not raw.is_absolute():
            raise ReplayError("VerificationPlan.workspace 必须是绝对路径")
        try:
            resolved = raw.resolve(strict=True)
        except OSError as exc:
            raise ReplayError(f"candidate workspace 不存在: {raw}") from exc
        if not resolved.is_dir():
            raise ReplayError("candidate workspace 必须是目录")
        try:
            resolved.relative_to(self.candidate_workspace_root)
        except ValueError as exc:
            raise ReplayError(
                "candidate workspace 超出受信 candidate_workspace_root"
            ) from exc
        return resolved

    @staticmethod
    def _snapshot_workspace(
        source: Path,
        target: Path,
        *,
        expected_digest: str,
        ignore: tuple[str, ...],
    ) -> Path:
        manifest = workspace_manifest(source, ignore)
        manifest_digest = sha256(_canonical_json(manifest)).hexdigest()
        if manifest_digest != expected_digest:
            raise ReplayError("workspace 内容与 VerificationPlan digest 不一致")
        unsupported = [
            relative
            for relative, entry in manifest.items()
            if entry["type"] not in {"file", "link"}
        ]
        if unsupported:
            raise ReplayError(
                "replay build context 包含特殊文件: " + ", ".join(unsupported[:10])
            )
        target.mkdir(mode=0o700)
        for relative, entry in manifest.items():
            relative_path = PurePosixPath(relative)
            destination_parent = target
            for part in relative_path.parts[:-1]:
                destination_parent /= part
                if destination_parent.is_symlink():
                    raise ReplayError("snapshot path 的父目录不能是符号链接")
                destination_parent.mkdir(mode=0o755, exist_ok=True)
                if not destination_parent.is_dir():
                    raise ReplayError("snapshot path 的父路径不是目录")
            source_path = source.joinpath(*relative_path.parts)
            destination = target.joinpath(*relative_path.parts)
            if entry["type"] == "link":
                try:
                    link_target = os.readlink(source_path)
                    destination.symlink_to(link_target)
                except OSError as exc:
                    raise ReplayError(f"无法复制 workspace 符号链接: {relative}") from exc
            else:
                try:
                    if not stat.S_ISREG(source_path.lstat().st_mode):
                        raise ReplayError(
                            f"workspace 文件在 snapshot 期间改变类型: {relative}"
                        )
                    shutil.copyfile(source_path, destination, follow_symlinks=False)
                    destination.chmod(int(entry["mode"]))
                    os.utime(destination, ns=(0, 0), follow_symlinks=False)
                except OSError as exc:
                    raise ReplayError(f"无法复制 workspace 文件: {relative}") from exc
        if workspace_digest(target) != expected_digest:
            raise ReplayError("workspace 在 snapshot 期间发生变化")
        return target.resolve()


class DockerReplayLauncher:
    """Coordinator adapter that replays every scenario in a frozen plan."""

    def __init__(
        self,
        database: str | Path,
        *,
        control: ReplayVariantSpec | None = None,
        candidate: ReplayVariantSpec | None = None,
        resolver: ReplayVariantResolver | None = None,
        limits: ReplayLimits | None = None,
    ):
        has_static_variant = control is not None or candidate is not None
        if resolver is not None and has_static_variant:
            raise ValueError("resolver 与静态 control/candidate 配置互斥")
        if resolver is None and (control is None or candidate is None):
            raise ValueError("必须同时配置静态 control/candidate，或提供 resolver")
        self.control = (
            ReplayVariantSpec.model_validate_json(control.model_dump_json())
            if control is not None
            else None
        )
        self.candidate = (
            ReplayVariantSpec.model_validate_json(candidate.model_dump_json())
            if candidate is not None
            else None
        )
        self.resolver = resolver
        self.limits = ReplayLimits.model_validate_json(
            (limits or ReplayLimits()).model_dump_json()
        )
        self._launcher = ReplayLauncher(database)

    async def replay(self, plan: "VerificationPlan") -> ReplayBatchReceipt:
        from .workflow import VerificationPlan

        frozen = VerificationPlan.model_validate_json(plan.model_dump_json())
        variants = await self._resolve_variants(frozen)
        receipts: list[ReplayReceipt] = []
        for reproduction in frozen.reproductions:
            request = ReplayRequest.from_plan(
                frozen,
                control=variants.control,
                candidate=variants.candidate,
                scenario_id=reproduction.scenario_id,
                limits=self.limits,
            )
            receipt = await asyncio.to_thread(self._launcher.replay, request)
            receipts.append(receipt)
        expected_ids = [item.scenario_id for item in frozen.reproductions]
        actual_ids = [item.scenario_id for item in receipts]
        if actual_ids != expected_ids:
            raise ReplayError("batch replay 未精确覆盖冻结场景")
        failures = tuple(
            f"{receipt.scenario_id}: {failure}"
            for receipt in receipts
            for failure in receipt.failures
        )
        return ReplayBatchReceipt(
            run_id=frozen.run_id,
            cycle=frozen.cycle,
            plan_digest=frozen.digest,
            control_digest=frozen.control_digest,
            candidate_ref=frozen.candidate_ref,
            candidate_digest=frozen.candidate_digest,
            policy_digest=frozen.policy_digest,
            skill_digests=dict(frozen.skill_digests),
            scenario_receipts=tuple(receipts),
            passed=not failures,
            failures=failures,
        )

    async def _resolve_variants(
        self, plan: "VerificationPlan"
    ) -> ReplayVariantResolution:
        from .workflow import VerificationPlan

        frozen = VerificationPlan.model_validate_json(plan.model_dump_json())
        if self.resolver is None:
            assert self.control is not None and self.candidate is not None
            resolved: object = ReplayVariantResolution(
                control=self.control,
                candidate=self.candidate,
            )
        else:
            resolver_plan = VerificationPlan.model_validate_json(
                frozen.model_dump_json()
            )
            resolved = await self.resolver.resolve(resolver_plan)
        try:
            validated = ReplayVariantResolution.model_validate(resolved)
            copied = ReplayVariantResolution.model_validate_json(
                validated.model_dump_json()
            )
        except Exception as exc:
            raise ReplayError(f"resolver 返回的 replay variant 配置非法: {exc}") from exc

        expected = {
            "control.source_ref": frozen.control_ref,
            "control.source_digest": frozen.control_digest,
            "candidate.source_ref": frozen.candidate_ref,
            "candidate.source_digest": frozen.candidate_digest,
        }
        actual = {
            "control.source_ref": copied.control.source_ref,
            "control.source_digest": copied.control.source_digest,
            "candidate.source_ref": copied.candidate.source_ref,
            "candidate.source_digest": copied.candidate.source_digest,
        }
        mismatches = [name for name, value in expected.items() if actual[name] != value]
        if mismatches:
            raise ReplayError(
                "resolved replay variants 与冻结 VerificationPlan 不一致: "
                + ", ".join(mismatches)
            )
        return copied


__all__ = [
    "DockerBuildReplayVariantResolver",
    "DockerReplayLauncher",
    "ReplayError",
    "ReplayBatchReceipt",
    "ReplayLauncher",
    "ReplayLimits",
    "ReplayReceipt",
    "ReplayRequest",
    "ReplayResult",
    "ReplayVariantResolution",
    "ReplayVariantResolver",
    "ReplayVariantSpec",
    "VariantReplayReceipt",
]
