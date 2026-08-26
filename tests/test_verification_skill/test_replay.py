from __future__ import annotations

from hashlib import sha256
import inspect
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

import core.verification.replay as replay_module
from core.observability import LocalObservabilityStore, normalized_input_digest
from core.verification import (
    CommandSpec,
    DockerBuildReplayVariantResolver,
    FrozenVerificationSkill,
    HOST_REPLAY_ORACLE_DIGEST,
    ReplayOracleDecision,
    ScenarioSpec,
    VerificationPolicy,
    VerificationSkillSpec,
    Variant,
    workspace_digest,
)
from core.verification.replay import (
    DockerReplayLauncher,
    ReplayError,
    ReplayLauncher,
    ReplayReceipt,
    ReplayRequest,
    ReplayVariantResolution,
    ReplayVariantSpec,
    VariantReplayReceipt,
)
from core.verification.workflow import (
    FailureSignature,
    ReproductionSpec,
    VerificationPlan,
)


CONTROL_IMAGE = "example/control@sha256:" + "1" * 64
CANDIDATE_IMAGE = "example/candidate@sha256:" + "2" * 64
CONTROL_DIGEST = "a" * 64
CANDIDATE_DIGEST = "b" * 64
POLICY_DIGEST = "c" * 64
PLAN_DIGEST = "d" * 64
SKILL_DIGESTS = {"checkout": "e" * 64}
INPUT = {"prompt": "reproduce checkout timeout", "seed": 7}
INPUT_DIGEST = normalized_input_digest(INPUT)
FAILURE_SIGNATURE = "checkout.timeout"


@pytest.fixture(autouse=True)
def _completed_otlp_barrier(monkeypatch: pytest.MonkeyPatch):
    """Docker unit tests isolate launcher logic from the OTLP integration tests."""

    monkeypatch.setattr(
        ReplayLauncher,
        "_close_otlp_barrier",
        lambda _self, _request, _variant, _collection_id: "4" * 64,
    )


def _variant(image: str, source_ref: str, source_digest: str) -> ReplayVariantSpec:
    return ReplayVariantSpec(
        image=image,
        source_ref=source_ref,
        source_digest=source_digest,
        command=("python3", "/app/replay.py"),
    )


def _request() -> ReplayRequest:
    return ReplayRequest(
        run_id="run-1",
        cycle=1,
        scenario_id="checkout:incident-reproducer",
        input_payload=INPUT,
        input_digest=INPUT_DIGEST,
        failure_signature=FailureSignature(
            code=FAILURE_SIGNATURE, error_type="TimeoutError"
        ),
        plan_digest=PLAN_DIGEST,
        policy_digest=POLICY_DIGEST,
        skill_digests=SKILL_DIGESTS,
        control=_variant(CONTROL_IMAGE, "control-ref", CONTROL_DIGEST),
        candidate=_variant(CANDIDATE_IMAGE, "candidate-ref", CANDIDATE_DIGEST),
    )


def _plan_for_candidate(
    *, cycle: int, candidate_ref: str, candidate_digest: str
) -> VerificationPlan:
    policy = VerificationPolicy()
    skill_spec = VerificationSkillSpec(
        name="checkout",
        version="1",
        description="checkout",
        integration=(
            ScenarioSpec(
                id="incident-reproducer",
                description="reproducer",
                steps=(CommandSpec(id="focused", argv=("pytest", "-q")),),
            ),
        ),
    )
    contract = FrozenVerificationSkill(
        name="checkout", spec=skill_spec, digest=SKILL_DIGESTS["checkout"]
    )
    return VerificationPlan(
        run_id="run-dynamic",
        cycle=cycle,
        incident_id="incident-1",
        incident_digest="f" * 64,
        workspace="/candidate",
        control_ref="control-ref",
        control_digest=CONTROL_DIGEST,
        candidate_ref=candidate_ref,
        candidate_digest=candidate_digest,
        policy=policy,
        policy_digest=policy.digest,
        skill_names=("checkout",),
        skill_digests=SKILL_DIGESTS,
        skill_contracts=(contract,),
        reproductions=(
            ReproductionSpec(
                scenario_id="checkout:incident-reproducer",
                skill_name="checkout",
                input_payload=INPUT,
                input_digest=INPUT_DIGEST,
                reproducer=True,
                failure_signature=FailureSignature(
                    code=FAILURE_SIGNATURE, error_type="TimeoutError"
                ),
                expected_control_outcome="failure",
            ),
        ),
    )


def _env_from_run(argv: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, token in enumerate(argv):
        if token == "--env":
            key, value = argv[index + 1].split("=", 1)
            result[key] = value
    return result


def _mount_source(argv: tuple[str, ...], destination: str) -> Path:
    for index, token in enumerate(argv):
        if token != "--mount":
            continue
        fields = argv[index + 1].split(",")
        values = dict(item.split("=", 1) for item in fields if "=" in item)
        if values.get("dst") == destination:
            return Path(values["src"])
    raise AssertionError(f"missing mount for {destination}")


class DockerHarness:
    def __init__(
        self,
        request: ReplayRequest,
        *,
        control_outcome: str = "failure",
        control_signatures: tuple[str, ...] = (FAILURE_SIGNATURE,),
        candidate_outcome: str = "success",
        candidate_signatures: tuple[str, ...] = (),
        bad_label: bool = False,
        control_exit_code: int = 0,
        result_mode: str = "valid",
        control_timeout: bool = False,
        control_exception: bool = False,
        cleanup_present: bool = False,
        cleanup_probe_error: bool = False,
    ):
        self.request = request
        self.control_outcome = control_outcome
        self.control_signatures = control_signatures
        self.candidate_outcome = candidate_outcome
        self.candidate_signatures = candidate_signatures
        self.bad_label = bad_label
        self.control_exit_code = control_exit_code
        self.result_mode = result_mode
        self.control_timeout = control_timeout
        self.control_exception = control_exception
        self.cleanup_present = cleanup_present
        self.cleanup_probe_error = cleanup_probe_error
        self.calls: list[tuple[str, ...]] = []
        self.input_bytes: list[bytes] = []

    def __call__(self, argv: tuple[str, ...], *, timeout_seconds: float):
        assert timeout_seconds > 0
        self.calls.append(argv)
        if argv[1:3] == ("image", "inspect"):
            spec = (
                self.request.control
                if argv[3] == self.request.control.image
                else self.request.candidate
            )
            labels = {
                replay_module.OCI_REVISION_LABEL: (
                    "wrong-ref" if self.bad_label else spec.source_ref
                ),
                replay_module.WORKSPACE_DIGEST_LABEL: spec.source_digest,
            }
            payload = [
                {
                    "Id": "sha256:" + ("9" if spec is self.request.control else "8") * 64,
                    "RepoDigests": [spec.image],
                    "Config": {"Labels": labels},
                }
            ]
            return replay_module._DockerResult(0, json.dumps(payload).encode(), b"")
        if argv[1:3] == ("rm", "-f"):
            return replay_module._DockerResult(1, b"", b"already removed")
        if argv[1:3] == ("container", "ls"):
            if self.cleanup_probe_error:
                return replay_module._DockerResult(1, b"", b"daemon unavailable")
            return replay_module._DockerResult(
                0,
                (b"container-id\n" if self.cleanup_present else b""),
                b"",
            )
        assert argv[1] == "run"
        env = _env_from_run(argv)
        variant = env["LOOP_ENGINEER_VERIFICATION_VARIANT"]
        input_path = _mount_source(argv, replay_module.INPUT_PATH)
        self.input_bytes.append(input_path.read_bytes())
        if variant == "control" and self.control_exit_code:
            return replay_module._DockerResult(
                self.control_exit_code, b"", b"container failed"
            )
        output = _mount_source(argv, "/loop-engineer/output")
        spec = self.request.control if variant == "control" else self.request.candidate
        if variant == "control" and self.control_timeout:
            raise subprocess.TimeoutExpired(argv, timeout_seconds)
        if variant == "control" and self.control_exception:
            raise RuntimeError("runner transport failed")
        result = {
            "schema_version": "verification-replay-result/v2",
            "run_id": self.request.run_id,
            "cycle": self.request.cycle,
            "scenario_id": self.request.scenario_id,
            "collection_id": env["LOOP_ENGINEER_VERIFICATION_COLLECTION_ID"],
            "variant": variant,
            "input_digest": self.request.input_digest,
            "plan_digest": self.request.plan_digest,
            "policy_digest": self.request.policy_digest,
            "skill_digests": self.request.skill_digests,
            "source_ref": spec.source_ref,
            "source_digest": spec.source_digest,
            "raw_response": {
                "status_code": (
                    500
                    if (
                        self.control_outcome
                        if variant == "control"
                        else self.candidate_outcome
                    ) == "failure"
                    else 200
                ),
                "body": {
                    "status": (
                        500
                        if (
                            self.control_outcome
                            if variant == "control"
                            else self.candidate_outcome
                        ) == "failure"
                        else 200
                    )
                },
            },
            "raw_logs": [
                {
                    "level": "ERROR",
                    "error_type": (
                        "TimeoutError"
                        if signature == FAILURE_SIGNATURE
                        else "NewFailure"
                    ),
                    "message": (
                        "checkout request timed out"
                        if signature == FAILURE_SIGNATURE
                        else "new replay failure"
                    ),
                    "event_code": signature,
                }
                for signature in (
                    self.control_signatures
                    if variant == "control"
                    else self.candidate_signatures
                )
            ],
            "request_id": f"request-{variant}",
            "trace_id": ("a" if variant == "control" else "b") * 32,
            "session_id": f"session-{variant}",
            "model": "model-a",
            "tool_calls": [],
        }
        if self.result_mode == "missing":
            return replay_module._DockerResult(0, b"adapter complete", b"")
        if self.result_mode == "invalid-json":
            (output / "result.json").write_text("{", encoding="utf-8")
            return replay_module._DockerResult(0, b"adapter complete", b"")
        if self.result_mode == "mismatched":
            result["input_digest"] = "f" * 64
        (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return replay_module._DockerResult(0, b"adapter complete", b"")


def test_request_requires_digest_pinned_images_frozen_input_and_no_shell() -> None:
    payload = _request().model_dump(mode="python")
    payload["control"]["image"] = "example/control:latest"
    with pytest.raises(ValidationError, match="sha256"):
        ReplayRequest.model_validate(payload)

    payload = _request().model_dump(mode="python")
    payload["control"]["command"] = ("sh", "-c", "echo forged")
    with pytest.raises(ValidationError, match="shell"):
        ReplayRequest.model_validate(payload)

    payload = _request().model_dump(mode="python")
    payload["input_digest"] = "f" * 64
    with pytest.raises(ValidationError, match="input_digest"):
        ReplayRequest.model_validate(payload)


def test_replay_uses_hardened_docker_argv_and_closes_sqlite_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    harness = DockerHarness(request)
    monkeypatch.setattr(replay_module, "_run_docker", harness)

    receipt = ReplayLauncher(tmp_path / "observability.sqlite3").replay(request)

    assert receipt.passed is True
    assert receipt.status == "passed"
    assert receipt.failures == ()
    assert receipt.plan_digest == PLAN_DIGEST
    assert receipt.control_digest == CONTROL_DIGEST
    assert receipt.candidate_digest == CANDIDATE_DIGEST
    assert len(receipt.digest) == 64
    assert harness.input_bytes == [
        replay_module._canonical_json(INPUT),
        replay_module._canonical_json(INPUT),
    ]

    runs = [argv for argv in harness.calls if len(argv) > 1 and argv[1] == "run"]
    assert len(runs) == 2
    probes = [
        argv for argv in harness.calls if argv[1:3] == ("container", "ls")
    ]
    assert len(probes) == 2
    input_mounts = [
        _mount_source(argv, replay_module.INPUT_PATH) for argv in runs
    ]
    assert input_mounts[0] == input_mounts[1]
    for argv in runs:
        assert "--read-only" in argv
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert argv[argv.index("--security-opt") + 1] == "no-new-privileges:true"
        assert "--pids-limit" in argv
        assert "--memory" in argv
        assert "--cpus" in argv
        assert "--tmpfs" in argv
        assert argv[argv.index("--entrypoint") + 1] == "python3"
        input_option = argv[argv.index("--mount") + 1]
        assert "readonly" in input_option

    rows = LocalObservabilityStore(
        tmp_path / "observability.sqlite3"
    ).execution_windows(request.run_id, request.cycle)
    assert [(row["variant"], row["collection_complete"], row["finished"]) for row in rows] == [
        ("candidate", 1, 1),
        ("control", 1, 1),
    ]
    assert {row["input_digest"] for row in rows} == {INPUT_DIGEST}


@pytest.mark.parametrize(
    ("control_outcome", "control_signatures", "candidate_outcome", "candidate_signatures"),
    [
        ("success", (), "success", ()),
        ("failure", (FAILURE_SIGNATURE,), "success", (FAILURE_SIGNATURE,)),
        ("failure", (FAILURE_SIGNATURE,), "failure", ("new.failure",)),
    ],
)
def test_semantic_replay_mismatch_returns_fail_closed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_outcome: str,
    control_signatures: tuple[str, ...],
    candidate_outcome: str,
    candidate_signatures: tuple[str, ...],
) -> None:
    request = _request()
    harness = DockerHarness(
        request,
        control_outcome=control_outcome,
        control_signatures=control_signatures,
        candidate_outcome=candidate_outcome,
        candidate_signatures=candidate_signatures,
    )
    monkeypatch.setattr(replay_module, "_run_docker", harness)

    receipt = ReplayLauncher(tmp_path / "observability.sqlite3").replay(request)

    assert receipt.passed is False
    assert receipt.status == "rejected"
    assert receipt.failures


def test_image_label_mismatch_blocks_before_container_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    harness = DockerHarness(request, bad_label=True)
    monkeypatch.setattr(replay_module, "_run_docker", harness)

    with pytest.raises(ReplayError, match="revision"):
        ReplayLauncher(tmp_path / "observability.sqlite3").replay(request)

    assert not any(len(argv) > 1 and argv[1] == "run" for argv in harness.calls)


def test_docker_failure_records_an_incomplete_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    harness = DockerHarness(request, control_exit_code=125)
    monkeypatch.setattr(replay_module, "_run_docker", harness)
    database = tmp_path / "observability.sqlite3"

    with pytest.raises(ReplayError, match="未完整结束"):
        ReplayLauncher(database).replay(request)

    rows = LocalObservabilityStore(database).execution_windows("run-1", 1)
    assert len(rows) == 1
    assert rows[0]["variant"] == "control"
    assert rows[0]["collection_complete"] == 0
    assert rows[0]["finished"] == 0


@pytest.mark.parametrize("result_mode", ["missing", "invalid-json", "mismatched"])
def test_untrusted_result_failure_records_an_incomplete_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result_mode: str,
) -> None:
    request = _request()
    harness = DockerHarness(request, result_mode=result_mode)
    monkeypatch.setattr(replay_module, "_run_docker", harness)
    database = tmp_path / "observability.sqlite3"

    with pytest.raises(ReplayError):
        ReplayLauncher(database).replay(request)

    rows = LocalObservabilityStore(database).execution_windows("run-1", 1)
    assert len(rows) == 1
    assert rows[0]["variant"] == "control"
    assert rows[0]["collection_complete"] == 0
    assert rows[0]["finished"] == 0


@pytest.mark.parametrize("failure_mode", ["timeout", "exception"])
def test_invocation_failure_with_unconfirmed_cleanup_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    request = _request()
    harness = DockerHarness(
        request,
        control_timeout=failure_mode == "timeout",
        control_exception=failure_mode == "exception",
        cleanup_probe_error=True,
    )
    monkeypatch.setattr(replay_module, "_run_docker", harness)
    database = tmp_path / "observability.sqlite3"

    with pytest.raises(ReplayError, match="无法确认容器已删除"):
        ReplayLauncher(database).replay(request)

    assert any(argv[1:3] == ("rm", "-f") for argv in harness.calls)
    assert any(argv[1:3] == ("container", "ls") for argv in harness.calls)
    rows = LocalObservabilityStore(database).execution_windows("run-1", 1)
    assert len(rows) == 1
    assert rows[0]["collection_complete"] == 0
    assert rows[0]["finished"] == 0


def test_residual_container_after_success_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    harness = DockerHarness(request, cleanup_present=True)
    monkeypatch.setattr(replay_module, "_run_docker", harness)
    database = tmp_path / "observability.sqlite3"

    with pytest.raises(ReplayError, match="容器仍然存在"):
        ReplayLauncher(database).replay(request)

    rows = LocalObservabilityStore(database).execution_windows("run-1", 1)
    assert len(rows) == 1
    assert rows[0]["collection_complete"] == 0
    assert rows[0]["finished"] == 0


def test_replay_request_can_be_derived_from_frozen_plan() -> None:
    policy = VerificationPolicy()
    skill_spec = VerificationSkillSpec(
        name="checkout",
        version="1",
        description="checkout",
        integration=(
            ScenarioSpec(
                id="incident-reproducer",
                description="reproducer",
                steps=(CommandSpec(id="focused", argv=("pytest", "-q")),),
            ),
        ),
    )
    contract = FrozenVerificationSkill(
        name="checkout", spec=skill_spec, digest=SKILL_DIGESTS["checkout"]
    )
    signature = FailureSignature(code=FAILURE_SIGNATURE, error_type="TimeoutError")
    plan = VerificationPlan(
        run_id="run-1",
        cycle=1,
        incident_id="incident-1",
        incident_digest="f" * 64,
        workspace="/candidate",
        control_ref="control-ref",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate-ref",
        candidate_digest=CANDIDATE_DIGEST,
        policy=policy,
        policy_digest=policy.digest,
        skill_names=("checkout",),
        skill_digests=SKILL_DIGESTS,
        skill_contracts=(contract,),
        reproductions=(
            ReproductionSpec(
                scenario_id="checkout:incident-reproducer",
                skill_name="checkout",
                input_payload=INPUT,
                input_digest=INPUT_DIGEST,
                reproducer=True,
                failure_signature=signature,
                expected_control_outcome="failure",
                expected_candidate_outcome="success",
            ),
        ),
    )

    request = ReplayRequest.from_plan(
        plan,
        control=_variant(CONTROL_IMAGE, "control-ref", CONTROL_DIGEST),
        candidate=_variant(CANDIDATE_IMAGE, "candidate-ref", CANDIDATE_DIGEST),
    )

    assert request.plan_digest == plan.digest
    assert request.failure_signature == signature
    assert request.input_digest == INPUT_DIGEST


def test_public_launcher_has_no_executor_injection_and_docker_never_uses_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tuple(inspect.signature(ReplayLauncher).parameters) == ("database",)
    captured = {}
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(b'ok')\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    monkeypatch.setattr(
        replay_module,
        "_docker_environment",
        lambda: {"PATH": str(tmp_path)},
    )
    real_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(replay_module.subprocess, "Popen", fake_popen)
    result = replay_module._run_docker(
        ("docker", "version"), timeout_seconds=1
    )

    assert result.returncode == 0
    assert captured["argv"] == ("docker", "version")
    assert captured["shell"] is False
    assert result.stdout == b"ok"


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_docker_output_streams_have_a_hard_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream_name: str,
) -> None:
    docker = tmp_path / "docker"
    docker.write_text(
        (
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.{stream_name}.buffer.write(b'x' * 65)\n"
            f"sys.{stream_name}.flush()\n"
        ),
        encoding="utf-8",
    )
    docker.chmod(0o755)
    monkeypatch.setattr(replay_module, "MAX_DOCKER_STREAM_BYTES", 64)
    monkeypatch.setattr(
        replay_module,
        "_docker_environment",
        lambda: {"PATH": str(tmp_path)},
    )

    with pytest.raises(
        replay_module._DockerOutputLimitExceeded,
        match=stream_name,
    ):
        replay_module._run_docker(("docker", "version"), timeout_seconds=1)


@pytest.mark.asyncio
async def test_coordinator_launcher_replays_every_frozen_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = VerificationPolicy()
    skill_spec = VerificationSkillSpec(
        name="checkout",
        version="1",
        description="checkout",
        integration=(
            ScenarioSpec(
                id="incident-reproducer",
                description="reproducer",
                steps=(CommandSpec(id="focused", argv=("pytest", "-q")),),
            ),
            ScenarioSpec(
                id="boundary",
                description="boundary",
                steps=(CommandSpec(id="boundary", argv=("pytest", "-q")),),
            ),
        ),
    )
    contract = FrozenVerificationSkill(
        name="checkout", spec=skill_spec, digest=SKILL_DIGESTS["checkout"]
    )
    signature = FailureSignature(code=FAILURE_SIGNATURE, error_type="TimeoutError")
    boundary_input = {"prompt": "checkout", "seed": 8}
    plan = VerificationPlan(
        run_id="run-batch",
        cycle=1,
        incident_id="incident-1",
        incident_digest="f" * 64,
        workspace="/candidate",
        control_ref="control-ref",
        control_digest=CONTROL_DIGEST,
        candidate_ref="candidate-ref",
        candidate_digest=CANDIDATE_DIGEST,
        policy=policy,
        policy_digest=policy.digest,
        skill_names=("checkout",),
        skill_digests=SKILL_DIGESTS,
        skill_contracts=(contract,),
        reproductions=(
            ReproductionSpec(
                scenario_id="checkout:incident-reproducer",
                skill_name="checkout",
                input_payload=INPUT,
                input_digest=INPUT_DIGEST,
                reproducer=True,
                failure_signature=signature,
                expected_control_outcome="failure",
            ),
            ReproductionSpec(
                scenario_id="checkout:boundary",
                skill_name="checkout",
                input_payload=boundary_input,
                input_digest=normalized_input_digest(boundary_input),
                expected_control_outcome="success",
            ),
        ),
    )
    calls: list[str] = []

    def variant_receipt(
        request: ReplayRequest, variant: Variant, outcome: str
    ) -> VariantReplayReceipt:
        spec = request.control if variant is Variant.CONTROL else request.candidate
        signatures = (
            (FAILURE_SIGNATURE,)
            if request.reproducer and variant is Variant.CONTROL
            else ()
        )
        decision = ReplayOracleDecision(
            outcome=outcome,
            failure_signatures=signatures,
            payload={"status": outcome},
        )
        return VariantReplayReceipt(
            variant=variant,
            source_ref=spec.source_ref,
            source_digest=spec.source_digest,
            image=spec.image,
            image_manifest_digest=spec.image.rsplit("@", 1)[1],
            image_config_digest="sha256:" + "9" * 64,
            launch_config_digest="8" * 64,
            input_digest=request.input_digest,
            collection_id=f"{request.scenario_id}-{variant.value}",
            otlp_barrier_digest="4" * 64,
            result_sha256="7" * 64,
            docker_stdout_sha256="6" * 64,
            docker_stderr_sha256="5" * 64,
            exit_code=0,
            finished=True,
            started_at_ns=1,
            ended_at_ns=2,
            oracle_digest=HOST_REPLAY_ORACLE_DIGEST,
            oracle_decision=decision,
            outcome=outcome,
            failure_signatures=signatures,
        )

    def fake_single_replay(_self, request: ReplayRequest) -> ReplayReceipt:
        calls.append(request.scenario_id)
        control_outcome = request.expected_control_outcome or "success"
        control = variant_receipt(request, Variant.CONTROL, control_outcome)
        candidate = variant_receipt(request, Variant.CANDIDATE, "success")
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
            skill_digests=request.skill_digests,
            control_digest=request.control.source_digest,
            candidate_ref=request.candidate.source_ref,
            candidate_digest=request.candidate.source_digest,
            control=control,
            candidate=candidate,
            passed=True,
            status="passed",
        )

    monkeypatch.setattr(ReplayLauncher, "replay", fake_single_replay)
    launcher = DockerReplayLauncher(
        tmp_path / "observability.sqlite3",
        control=_variant(CONTROL_IMAGE, "control-ref", CONTROL_DIGEST),
        candidate=_variant(CANDIDATE_IMAGE, "candidate-ref", CANDIDATE_DIGEST),
    )

    receipt = await launcher.replay(plan)

    assert receipt.passed is True
    assert calls == ["checkout:incident-reproducer", "checkout:boundary"]
    assert tuple(item.scenario_id for item in receipt.scenario_receipts) == tuple(calls)


@pytest.mark.asyncio
async def test_coordinator_launcher_resolves_variants_for_each_plan_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_digest = "b" * 64
    second_digest = "4" * 64
    first_plan = _plan_for_candidate(
        cycle=1,
        candidate_ref="candidate-ref-1",
        candidate_digest=first_digest,
    )
    second_plan = _plan_for_candidate(
        cycle=2,
        candidate_ref="candidate-ref-2",
        candidate_digest=second_digest,
    )
    candidate_images = {
        "candidate-ref-1": "example/candidate-one@sha256:" + "2" * 64,
        "candidate-ref-2": "example/candidate-two@sha256:" + "3" * 64,
    }

    class PerPlanResolver:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str, str]] = []

        async def resolve(self, plan: VerificationPlan) -> ReplayVariantResolution:
            self.calls.append(
                (plan.cycle, plan.candidate_ref, plan.candidate_digest)
            )
            return ReplayVariantResolution(
                control=_variant(
                    CONTROL_IMAGE, plan.control_ref, plan.control_digest
                ),
                candidate=_variant(
                    candidate_images[plan.candidate_ref],
                    plan.candidate_ref,
                    plan.candidate_digest,
                ),
            )

    replay_requests: list[ReplayRequest] = []

    def variant_receipt(
        request: ReplayRequest, variant: Variant, outcome: str
    ) -> VariantReplayReceipt:
        spec = request.control if variant is Variant.CONTROL else request.candidate
        signatures = (
            (FAILURE_SIGNATURE,) if variant is Variant.CONTROL else ()
        )
        decision = ReplayOracleDecision(
            outcome=outcome,
            failure_signatures=signatures,
            payload={"status": outcome},
        )
        return VariantReplayReceipt(
            variant=variant,
            source_ref=spec.source_ref,
            source_digest=spec.source_digest,
            image=spec.image,
            image_manifest_digest=spec.image.rsplit("@", 1)[1],
            image_config_digest="sha256:" + "9" * 64,
            launch_config_digest="8" * 64,
            input_digest=request.input_digest,
            collection_id=f"{request.cycle}-{variant.value}",
            otlp_barrier_digest="4" * 64,
            result_sha256="7" * 64,
            docker_stdout_sha256="6" * 64,
            docker_stderr_sha256="5" * 64,
            exit_code=0,
            finished=True,
            started_at_ns=1,
            ended_at_ns=2,
            oracle_digest=HOST_REPLAY_ORACLE_DIGEST,
            oracle_decision=decision,
            outcome=outcome,
            failure_signatures=signatures,
        )

    def fake_single_replay(_self, request: ReplayRequest) -> ReplayReceipt:
        replay_requests.append(request)
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
            skill_digests=request.skill_digests,
            control_digest=request.control.source_digest,
            candidate_ref=request.candidate.source_ref,
            candidate_digest=request.candidate.source_digest,
            control=variant_receipt(request, Variant.CONTROL, "failure"),
            candidate=variant_receipt(request, Variant.CANDIDATE, "success"),
            passed=True,
            status="passed",
        )

    monkeypatch.setattr(ReplayLauncher, "replay", fake_single_replay)
    resolver = PerPlanResolver()
    launcher = DockerReplayLauncher(
        tmp_path / "observability.sqlite3", resolver=resolver
    )

    first_receipt = await launcher.replay(first_plan)
    second_receipt = await launcher.replay(second_plan)

    assert resolver.calls == [
        (1, "candidate-ref-1", first_digest),
        (2, "candidate-ref-2", second_digest),
    ]
    assert [request.candidate.source_ref for request in replay_requests] == [
        "candidate-ref-1",
        "candidate-ref-2",
    ]
    assert [request.candidate.source_digest for request in replay_requests] == [
        first_digest,
        second_digest,
    ]
    assert first_receipt.candidate_ref == "candidate-ref-1"
    assert second_receipt.candidate_ref == "candidate-ref-2"


@pytest.mark.asyncio
async def test_dynamic_variant_resolution_rejects_plan_binding_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan_for_candidate(
        cycle=2,
        candidate_ref="candidate-ref-2",
        candidate_digest="4" * 64,
    )

    class StaleResolver:
        async def resolve(self, plan: VerificationPlan) -> ReplayVariantResolution:
            return ReplayVariantResolution(
                control=_variant(
                    CONTROL_IMAGE, plan.control_ref, plan.control_digest
                ),
                candidate=_variant(
                    CANDIDATE_IMAGE, "candidate-ref-1", CANDIDATE_DIGEST
                ),
            )

    def unexpected_replay(_self, _request: ReplayRequest) -> ReplayReceipt:
        raise AssertionError("binding mismatch must fail before Docker replay")

    monkeypatch.setattr(ReplayLauncher, "replay", unexpected_replay)
    launcher = DockerReplayLauncher(
        tmp_path / "observability.sqlite3", resolver=StaleResolver()
    )

    with pytest.raises(
        ReplayError,
        match=r"candidate\.source_ref, candidate\.source_digest",
    ):
        await launcher.replay(plan)


def test_dynamic_resolver_and_static_variants_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    class Resolver:
        async def resolve(self, plan: VerificationPlan) -> ReplayVariantResolution:
            raise AssertionError(plan)

    with pytest.raises(ValueError, match="互斥"):
        DockerReplayLauncher(
            tmp_path / "observability.sqlite3",
            control=_variant(CONTROL_IMAGE, "control-ref", CONTROL_DIGEST),
            candidate=_variant(
                CANDIDATE_IMAGE, "candidate-ref", CANDIDATE_DIGEST
            ),
            resolver=Resolver(),
        )


@pytest.mark.asyncio
async def test_docker_build_resolver_builds_and_resolves_each_plan_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dockerfile_bytes = b"FROM scratch\nCOPY . /app\n"
    control_workspace = tmp_path / "control"
    candidate_root = tmp_path / "candidates"
    first_workspace = candidate_root / "cycle-1"
    second_workspace = candidate_root / "cycle-2"
    for workspace, payload in (
        (control_workspace, "control"),
        (first_workspace, "candidate one"),
        (second_workspace, "candidate two"),
    ):
        workspace.mkdir(parents=True)
        (workspace / "Dockerfile.verification").write_bytes(dockerfile_bytes)
        (workspace / "application.txt").write_text(payload, encoding="utf-8")

    template = _plan_for_candidate(
        cycle=1,
        candidate_ref="candidate-ref-1",
        candidate_digest=workspace_digest(first_workspace),
    )

    def bound_plan(
        *, cycle: int, candidate_ref: str, workspace: Path
    ) -> VerificationPlan:
        payload = template.model_dump(mode="python")
        payload.update(
            cycle=cycle,
            workspace=str(workspace),
            control_digest=workspace_digest(
                control_workspace, template.policy.workspace_ignore
            ),
            candidate_ref=candidate_ref,
            candidate_digest=workspace_digest(
                workspace, template.policy.workspace_ignore
            ),
        )
        return VerificationPlan.model_validate(payload)

    first_plan = bound_plan(
        cycle=1, candidate_ref="candidate-ref-1", workspace=first_workspace
    )
    second_plan = bound_plan(
        cycle=2, candidate_ref="candidate-ref-2", workspace=second_workspace
    )
    calls: list[tuple[str, ...]] = []
    images: dict[str, dict[str, str]] = {}

    def fake_docker(
        argv: tuple[str, ...], *, timeout_seconds: float
    ) -> replay_module._DockerResult:
        assert timeout_seconds > 0
        calls.append(argv)
        if argv[:3] == ("docker", "buildx", "build"):
            tag = argv[argv.index("--tag") + 1]
            repository = tag.rsplit(":", 1)[0]
            manifest_digest = "sha256:" + sha256(tag.encode()).hexdigest()
            pinned_image = f"{repository}@{manifest_digest}"
            labels: dict[str, str] = {}
            for index, token in enumerate(argv):
                if token == "--label":
                    key, value = argv[index + 1].split("=", 1)
                    labels[key] = value
            images[pinned_image] = labels
            metadata_path = Path(argv[argv.index("--metadata-file") + 1])
            metadata_path.write_text(
                json.dumps({"containerimage.digest": manifest_digest}),
                encoding="utf-8",
            )
            return replay_module._DockerResult(0, b"built", b"")
        if argv[:2] == ("docker", "pull"):
            assert argv[2] in images
            return replay_module._DockerResult(0, b"pulled", b"")
        if argv[:3] == ("docker", "image", "inspect"):
            image = argv[3]
            payload = [
                {
                    "Id": "sha256:" + sha256(image.encode()).hexdigest(),
                    "RepoDigests": [image],
                    "Config": {"Labels": images[image]},
                }
            ]
            return replay_module._DockerResult(
                0, json.dumps(payload).encode(), b""
            )
        raise AssertionError(argv)

    monkeypatch.setattr(replay_module, "_run_docker", fake_docker)
    resolver = DockerBuildReplayVariantResolver(
        control_workspace=control_workspace,
        candidate_workspace_root=candidate_root,
        dockerfile="Dockerfile.verification",
        dockerfile_sha256=sha256(dockerfile_bytes).hexdigest(),
        control_repository="registry.example/loop/control",
        candidate_repository="registry.example/loop/candidate",
        command=("python3", "/app/replay.py"),
    )

    first = await resolver.resolve(first_plan)
    second = await resolver.resolve(second_plan)

    assert first.candidate.source_ref == "candidate-ref-1"
    assert first.candidate.source_digest == first_plan.candidate_digest
    assert second.candidate.source_ref == "candidate-ref-2"
    assert second.candidate.source_digest == second_plan.candidate_digest
    assert first.candidate.image != second.candidate.image
    builds = [call for call in calls if call[:3] == ("docker", "buildx", "build")]
    assert len(builds) == 4
    assert all("--push" in call and "--no-cache" in call for call in builds)
    assert all(call[call.index("--network") + 1] == "none" for call in builds)
    assert all(Path(call[-1]).is_absolute() for call in builds)
    assert all(str(tmp_path) not in call[-1] for call in builds)
    assert all(
        any(
            item.startswith(f"{replay_module.OCI_REVISION_LABEL}=")
            for item in call
        )
        and any(
            item.startswith(f"{replay_module.WORKSPACE_DIGEST_LABEL}=")
            for item in call
        )
        for call in builds
    )
    pulls = [call for call in calls if call[:2] == ("docker", "pull")]
    assert len(pulls) == 4
    assert all("@sha256:" in call[2] for call in pulls)


@pytest.mark.asyncio
async def test_docker_build_resolver_rejects_unbound_paths_and_workspace_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dockerfile_bytes = b"FROM scratch\nCOPY . /app\n"
    control_workspace = tmp_path / "control"
    candidate_root = tmp_path / "candidates"
    candidate_workspace = candidate_root / "cycle-1"
    outside_workspace = tmp_path / "outside"
    for workspace in (control_workspace, candidate_workspace, outside_workspace):
        workspace.mkdir(parents=True)
        (workspace / "Dockerfile.verification").write_bytes(dockerfile_bytes)
        (workspace / "application.txt").write_text("bound", encoding="utf-8")
    template = _plan_for_candidate(
        cycle=1,
        candidate_ref="candidate-ref-1",
        candidate_digest=workspace_digest(candidate_workspace),
    )

    def plan_for(workspace: Path) -> VerificationPlan:
        payload = template.model_dump(mode="python")
        payload.update(
            workspace=str(workspace),
            control_digest=workspace_digest(
                control_workspace, template.policy.workspace_ignore
            ),
            candidate_digest=workspace_digest(
                workspace, template.policy.workspace_ignore
            ),
        )
        return VerificationPlan.model_validate(payload)

    def unexpected_docker(
        argv: tuple[str, ...], *, timeout_seconds: float
    ) -> replay_module._DockerResult:
        raise AssertionError((argv, timeout_seconds))

    monkeypatch.setattr(replay_module, "_run_docker", unexpected_docker)
    resolver = DockerBuildReplayVariantResolver(
        control_workspace=control_workspace,
        candidate_workspace_root=candidate_root,
        dockerfile="Dockerfile.verification",
        dockerfile_sha256=sha256(dockerfile_bytes).hexdigest(),
        control_repository="registry.example/loop/control",
        candidate_repository="registry.example/loop/candidate",
        command=("python3", "/app/replay.py"),
    )

    with pytest.raises(ReplayError, match="超出受信"):
        await resolver.resolve(plan_for(outside_workspace))

    bound = plan_for(candidate_workspace)
    (candidate_workspace / "application.txt").write_text(
        "changed after freeze", encoding="utf-8"
    )
    with pytest.raises(ReplayError, match="Plan digest"):
        await resolver.resolve(bound)
