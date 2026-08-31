from __future__ import annotations

from pathlib import Path
import os
import sys

import pytest

from core.verification import CommandRunner, CommandSpec, GateKind
from core.verification.runner import _terminate_process_group, _validate_executable


RUNNER_ARGS = {
    "run_id": "run-1",
    "candidate_ref": "candidate",
    "policy_digest": "b" * 64,
}


@pytest.mark.asyncio
async def test_runner_uses_argv_without_shell_interpretation(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    spec = CommandSpec(
        id="argv",
        argv=(
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1])",
            f"; touch {marker}",
        ),
        stdout_contains=(f"; touch {marker}",),
    )
    evidence = await CommandRunner().run(
        spec,
        gate=GateKind.INTEGRATION,
        workspace=tmp_path,
        **RUNNER_ARGS,
    )
    assert evidence.passed
    assert evidence.exit_code == 0
    assert not marker.exists()
    assert evidence.sandbox_backend == "macos-seatbelt+workspace-copy"


@pytest.mark.asyncio
async def test_runner_records_timeout_and_marker_mismatch(tmp_path: Path) -> None:
    timeout = await CommandRunner().run(
        CommandSpec(
            id="timeout",
            argv=(sys.executable, "-c", "import time; time.sleep(2)"),
            timeout_ms=100,
        ),
        gate=GateKind.UNIT,
        workspace=tmp_path,
        **RUNNER_ARGS,
    )
    assert timeout.timed_out
    assert not timeout.passed
    assert timeout.exit_code is None

    mismatch = await CommandRunner().run(
        CommandSpec(
            id="marker",
            argv=(sys.executable, "-c", "print('actual')"),
            stdout_contains=("expected",),
        ),
        gate=GateKind.UNIT,
        workspace=tmp_path,
        **RUNNER_ARGS,
    )
    assert not mismatch.passed
    assert "stdout missing marker" in mismatch.failures[0]


@pytest.mark.asyncio
async def test_runner_uses_disposable_copy_and_protects_locked_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("original", encoding="utf-8")
    evidence = await CommandRunner().run(
        CommandSpec(
            id="mutate",
            argv=(
                sys.executable,
                "-c",
                "from pathlib import Path; Path('changed.txt').write_text('x')",
            ),
        ),
        gate=GateKind.INTEGRATION,
        workspace=tmp_path,
        **RUNNER_ARGS,
    )
    assert evidence.passed
    assert not (tmp_path / "changed.txt").exists()

    locked = await CommandRunner().run(
        CommandSpec(
            id="mutate-locked",
            argv=(
                sys.executable,
                "-c",
                "from pathlib import Path; Path('source.txt').write_text('changed')",
            ),
        ),
        gate=GateKind.INTEGRATION,
        workspace=tmp_path,
        **RUNNER_ARGS,
    )
    assert not locked.passed
    assert source.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_lint_warning_pattern_fails_even_with_zero_exit(tmp_path: Path) -> None:
    evidence = await CommandRunner().run(
        CommandSpec(
            id="lint",
            argv=(sys.executable, "-c", "print('warning: unused import')"),
        ),
        gate=GateKind.LINT,
        workspace=tmp_path,
        additional_forbidden_patterns=(r"(?i)warning",),
        **RUNNER_ARGS,
    )
    assert evidence.exit_code == 0
    assert not evidence.passed


def test_command_spec_rejects_shell_command_strings() -> None:
    with pytest.raises(ValueError, match="shell"):
        CommandSpec(id="shell", argv=("bash", "-c", "echo ok"))


def test_candidate_local_symlink_cannot_supply_verification_runtime(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    execution = tmp_path / "execution"
    source.mkdir()
    execution.mkdir()
    local_runtime = source / "python3"
    local_runtime.symlink_to(sys.executable)

    with pytest.raises(ValueError, match="candidate"):
        _validate_executable((str(local_runtime),), source, execution)


def test_relative_executable_is_resolved_from_command_cwd(tmp_path: Path) -> None:
    source = tmp_path / "source"
    execution = tmp_path / "execution"
    command_cwd = execution / "service"
    source.mkdir()
    command_cwd.mkdir(parents=True)
    wrapper = command_cwd / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o700)

    assert _validate_executable(
        ("./gradlew",),
        source,
        execution,
        command_cwd=command_cwd,
    ) == wrapper.resolve()


def test_relative_executable_must_exist_and_be_executable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    execution = tmp_path / "execution"
    source.mkdir()
    execution.mkdir()

    with pytest.raises(ValueError, match="不存在"):
        _validate_executable(("./gradlew",), source, execution)

    wrapper = execution / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不可执行"):
        _validate_executable(("./gradlew",), source, execution)


@pytest.mark.asyncio
async def test_sandbox_blocks_writes_outside_execution_copy(tmp_path: Path) -> None:
    outside = tmp_path.parent / "verification-escape.txt"
    if outside.exists():
        outside.unlink()
    evidence = await CommandRunner().run(
        CommandSpec(
            id="escape",
            argv=(
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(outside)!r}).write_text('x')",
            ),
        ),
        gate=GateKind.INTEGRATION,
        workspace=tmp_path,
        **RUNNER_ARGS,
    )
    assert not evidence.passed
    assert not outside.exists()


@pytest.mark.asyncio
async def test_output_is_bounded_and_full_stream_is_hashed(tmp_path: Path) -> None:
    evidence = await CommandRunner(max_output_bytes=32).run(
        CommandSpec(
            id="large-output",
            argv=(sys.executable, "-c", "print('x' * 1000)"),
        ),
        gate=GateKind.UNIT,
        workspace=tmp_path,
        **RUNNER_ARGS,
    )
    assert evidence.stdout_truncated
    assert evidence.stdout_bytes > len(evidence.stdout.encode("utf-8"))
    assert not evidence.passed


@pytest.mark.asyncio
async def test_non_utf8_output_is_recorded_and_fails_closed(tmp_path: Path) -> None:
    evidence = await CommandRunner().run(
        CommandSpec(
            id="binary-output",
            argv=(
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(bytes([255]))",
            ),
        ),
        gate=GateKind.UNIT,
        workspace=tmp_path,
        **RUNNER_ARGS,
    )

    assert evidence.stdout_encoding == "base64"
    assert evidence.stdout_sha256 == "a8100ae6aa1940d0b663bb31cd466142ebbdbd5187131b92d93818987832eb89"
    assert not evidence.passed
    assert any("not valid UTF-8" in failure for failure in evidence.failures)


@pytest.mark.asyncio
async def test_runner_does_not_inherit_unapproved_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERY_SECRET_TOKEN", "must-not-leak")
    evidence = await CommandRunner().run(
        CommandSpec(
            id="env",
            argv=(
                sys.executable,
                "-c",
                "import os; print(os.getenv('VERY_SECRET_TOKEN'))",
            ),
            stdout_contains=("None",),
        ),
        gate=GateKind.UNIT,
        workspace=tmp_path,
        **RUNNER_ARGS,
    )
    assert evidence.passed
    assert "must-not-leak" not in evidence.stdout


@pytest.mark.asyncio
async def test_termination_targets_children_after_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group cleanup is POSIX-specific")

    calls: list[tuple[int, int]] = []

    class FinishedLeader:
        pid = 12345
        returncode = 0

        async def wait(self) -> int:
            raise AssertionError("finished leader must not be waited twice")

    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))

    await _terminate_process_group(FinishedLeader())  # type: ignore[arg-type]

    assert calls == [(12345, 9)]


@pytest.mark.asyncio
async def test_runner_records_process_group_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("process-group cleanup is POSIX-specific")

    def deny_killpg(pid: int, sig: int) -> None:
        del pid, sig
        raise PermissionError("denied")

    monkeypatch.setattr(os, "killpg", deny_killpg)
    evidence = await CommandRunner().run(
        CommandSpec(id="cleanup", argv=(sys.executable, "-c", "print('done')")),
        gate=GateKind.UNIT,
        workspace=tmp_path,
        **RUNNER_ARGS,
    )

    assert not evidence.passed
    assert evidence.error is not None
    assert "PermissionError" in evidence.error
