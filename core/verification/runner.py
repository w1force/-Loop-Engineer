"""Verification 命令执行器。

命令只接受冻结 argv，在一次性工作区副本中执行；默认要求 OS sandbox、最小环境、
有界输出和进程组超时终止。候选源码只用于取快照与校验，绝不作为 command cwd。
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import fnmatch
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import tempfile
import time
from typing import Literal

from .models import (
    CommandEvidence,
    CommandSpec,
    GateKind,
    Variant,
    command_contract_digest,
)


DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
_ALLOWED_EXECUTABLES = frozenset(
    {
        "bun", "cargo", "deno", "eslint", "go", "gradle", "gradlew",
        "jest", "make", "mypy", "mvn", "mvnw", "node", "npm", "npx",
        "pnpm", "pytest", "pyright", "python", "python3", "ruff", "tsc",
        "uv", "vitest", "yarn",
    }
)


class WorkspaceDigestError(RuntimeError):
    pass


def _ignored(relative: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        prefix = pattern[:-3].rstrip("/") if pattern.endswith("/**") else None
        if prefix and (relative == prefix or relative.startswith(prefix + "/")):
            return True
        if fnmatch.fnmatchcase(relative, pattern) or Path(relative).match(pattern):
            return True
    return False


def workspace_manifest(
    root: str | Path, ignore: tuple[str, ...] = ()
) -> dict[str, dict[str, str | int]]:
    """生成无歧义 manifest，并拒绝读取过程中发生变化的文件。"""

    workspace = Path(root).resolve()
    if not workspace.is_dir():
        raise WorkspaceDigestError(f"workspace 不存在或不是目录: {workspace}")
    manifest: dict[str, dict[str, str | int]] = {}
    try:
        paths = sorted(workspace.rglob("*"), key=lambda item: item.as_posix())
        for path in paths:
            relative = path.relative_to(workspace).as_posix()
            if _ignored(relative, ignore):
                continue
            before = path.lstat()
            if stat.S_ISDIR(before.st_mode):
                continue
            if stat.S_ISLNK(before.st_mode):
                content = os.readlink(path).encode("utf-8")
                kind = "link"
            elif stat.S_ISREG(before.st_mode):
                content = path.read_bytes()
                kind = "file"
            else:
                content = b""
                kind = "special"
            after = path.lstat()
            before_state = (
                before.st_mode, before.st_size, before.st_mtime_ns, before.st_ino
            )
            after_state = (
                after.st_mode, after.st_size, after.st_mtime_ns, after.st_ino
            )
            if before_state != after_state:
                raise WorkspaceDigestError(
                    f"workspace 在采集 manifest 时发生变化: {relative}"
                )
            manifest[relative] = {
                "type": kind,
                "mode": before.st_mode & 0o777,
                "size": len(content),
                "sha256": sha256(content).hexdigest(),
            }
    except OSError as exc:
        raise WorkspaceDigestError(f"计算 workspace manifest 失败: {exc}") from exc
    return manifest


def workspace_digest(root: str | Path, ignore: tuple[str, ...] = ()) -> str:
    manifest = workspace_manifest(root, ignore)
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _copy_ignore(source: Path, patterns: tuple[str, ...]):
    def ignore(directory: str, names: list[str]) -> set[str]:
        current = Path(directory)
        ignored: set[str] = set()
        for name in names:
            relative = (current / name).relative_to(source).as_posix()
            if _ignored(relative, patterns):
                ignored.add(name)
        return ignored

    return ignore


def _clone_workspace(source: Path, patterns: tuple[str, ...]) -> Path:
    temporary_root = Path(tempfile.mkdtemp(prefix="loop-verification-"))
    target = temporary_root / "candidate"
    try:
        shutil.copytree(
            source,
            target,
            symlinks=True,
            ignore=_copy_ignore(source, patterns),
        )
    except Exception:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise
    return target.resolve()


def _resolve_executable(value: str, cwd: Path) -> Path | None:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    if "/" in value or "\\" in value:
        return (cwd / path).resolve()
    found = shutil.which(value)
    return Path(found).resolve() if found else None


def _validate_executable(
    argv: tuple[str, ...],
    source: Path,
    execution: Path,
    *,
    command_cwd: Path | None = None,
) -> Path:
    executable_name = Path(argv[0]).name
    if executable_name not in _ALLOWED_EXECUTABLES:
        raise ValueError(f"verification executable 不在允许列表: {argv[0]}")
    raw_executable = Path(argv[0])
    if raw_executable.is_absolute():
        try:
            raw_executable.absolute().relative_to(source)
        except ValueError:
            pass
        else:
            raise ValueError(
                "verification executable 路径不能位于 candidate（包括外链运行时）"
            )
    resolved = _resolve_executable(argv[0], command_cwd or execution)
    if resolved is None or not resolved.is_file():
        raise ValueError(f"verification executable 不存在: {argv[0]}")
    if not os.access(resolved, os.X_OK):
        raise ValueError(f"verification executable 不可执行: {argv[0]}")
    try:
        resolved.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("verification executable 不能直接来自原始 candidate")
    roots = [
        execution,
        Path(sys.executable).resolve().parent,
        Path("/bin"),
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
    ]
    for root in roots:
        if not root.exists():
            continue
        trusted = root.resolve()
        if resolved == trusted:
            return resolved
        try:
            resolved.relative_to(trusted)
            return resolved
        except ValueError:
            pass
    raise ValueError(f"verification executable 来源不可信: {resolved}")


def _minimal_environment(execution: Path, configured: dict[str, str]) -> dict[str, str]:
    inherited = ("JAVA_HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TERM", "TZ")
    env = {key: os.environ[key] for key in inherited if key in os.environ}
    home = execution / ".verification-home"
    temp = execution / ".verification-tmp"
    home.mkdir(exist_ok=True)
    temp.mkdir(exist_ok=True)
    env.update(
        {
            "CI": "1",
            "HOME": str(home),
            "PWD": str(execution),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(temp),
        }
    )
    env.update(configured)
    return env


def _sandboxed_argv(
    argv: tuple[str, ...],
    *,
    execution: Path,
    executable: Path,
    immutable_files: tuple[str, ...],
    mode: Literal["required"],
) -> tuple[tuple[str, ...], str]:
    del mode  # 当前只有 required；保留参数便于后续替换 Docker backend。
    sandbox_exec = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or sandbox_exec is None:
        raise RuntimeError(
            "当前平台没有可用的 verification OS sandbox；禁止降级到宿主执行"
        )
    system_roots = [
        Path(value)
        for value in (
            "/System", "/Library", "/bin", "/dev", "/opt/homebrew",
            "/private/etc", "/private/var/db/timezone", "/sbin", "/usr",
        )
        if Path(value).exists()
    ]
    runtime_root = (
        executable.parent.parent
        if executable.parent.name == "bin"
        else executable.parent
    )
    raw_executable = Path(argv[0])
    raw_runtime_root: Path | None = None
    if raw_executable.is_absolute():
        raw_runtime_root = (
            raw_executable.parent.parent
            if raw_executable.parent.name == "bin"
            else raw_executable.parent
        )
    read_roots = {execution, runtime_root, *system_roots}
    if raw_runtime_root is not None:
        read_roots.add(raw_runtime_root)
    profile = "\n".join(
        [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            "(deny file-read*)",
            "(allow file-read-metadata)",
            '(allow file-read* (literal "/"))',
            *[
                f"(allow file-read* (subpath {json.dumps(str(root.resolve()))}))"
                for root in sorted(read_roots, key=str)
            ],
            "(deny file-write*)",
            f"(allow file-write* (subpath {json.dumps(str(execution))}))",
            '(allow file-write* (literal "/dev/null"))',
            *[
                f"(deny file-write* (literal {json.dumps(str(execution / relative))}))"
                for relative in immutable_files
            ],
        ]
    )
    return (sandbox_exec, "-p", profile, *argv), "macos-seatbelt+workspace-copy"


@dataclass(frozen=True)
class _CapturedStream:
    text: str
    encoding: Literal["utf-8", "base64"]
    digest: str
    byte_count: int
    truncated: bool


async def _capture_stream(
    stream: asyncio.StreamReader | None, limit: int
) -> _CapturedStream:
    digest = sha256()
    captured = bytearray()
    count = 0
    if stream is not None:
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
            if len(captured) < limit:
                captured.extend(chunk[: limit - len(captured)])
    captured_bytes = bytes(captured)
    try:
        text = captured_bytes.decode("utf-8")
        encoding: Literal["utf-8", "base64"] = "utf-8"
    except UnicodeDecodeError:
        text = base64.b64encode(captured_bytes).decode("ascii")
        encoding = "base64"
    return _CapturedStream(
        text=text,
        encoding=encoding,
        digest=digest.hexdigest(),
        byte_count=count,
        truncated=count > len(captured),
    )


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate the whole verification process group, including orphan children."""

    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.returncode is None:
            proc.kill()
    except ProcessLookupError:
        pass
    if proc.returncode is None:
        await proc.wait()


class CommandRunner:
    def __init__(
        self,
        *,
        workspace_ignore: tuple[str, ...] = (),
        sandbox_mode: Literal["required"] = "required",
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ):
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes 必须大于 0")
        self.workspace_ignore = workspace_ignore
        self.sandbox_mode = sandbox_mode
        self.max_output_bytes = max_output_bytes

    async def run(
        self,
        spec: CommandSpec,
        *,
        run_id: str,
        cycle: int = 1,
        gate: GateKind,
        workspace: str | Path,
        candidate_ref: str,
        policy_digest: str,
        skill_digest: str | None = None,
        scenario_id: str | None = None,
        skill_name: str | None = None,
        variant: Variant = Variant.CANDIDATE,
        additional_forbidden_patterns: tuple[str, ...] = (),
    ) -> CommandEvidence:
        spec = CommandSpec.model_validate(spec.model_dump(mode="python"))
        source = Path(workspace).resolve()
        stdout = _CapturedStream("", "utf-8", sha256(b"").hexdigest(), 0, False)
        stderr = _CapturedStream("", "utf-8", sha256(b"").hexdigest(), 0, False)
        exit_code: int | None = None
        timed_out = False
        error: str | None = None
        sandbox_backend = "unavailable"
        execution: Path | None = None
        before = "0" * 64
        after = "0" * 64
        started = time.monotonic()
        process: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[_CapturedStream] | None = None
        stderr_task: asyncio.Task[_CapturedStream] | None = None
        effective_forbidden = (
            *spec.forbidden_output_patterns,
            *additional_forbidden_patterns,
        )

        try:
            before = await asyncio.to_thread(
                workspace_digest, source, self.workspace_ignore
            )
            execution = await asyncio.to_thread(
                _clone_workspace, source, self.workspace_ignore
            )
            initial_manifest = await asyncio.to_thread(
                workspace_manifest, execution, self.workspace_ignore
            )
            cloned_digest = await asyncio.to_thread(
                workspace_digest, execution, self.workspace_ignore
            )
            if cloned_digest != before:
                raise RuntimeError("candidate 副本 digest 与源快照不一致")
            command_cwd = (execution / spec.cwd).resolve()
            try:
                command_cwd.relative_to(execution)
            except ValueError as exc:
                raise ValueError("command cwd 越出隔离 workspace") from exc
            if not command_cwd.is_dir():
                raise ValueError(f"command cwd 不存在: {spec.cwd}")
            executable = _validate_executable(
                spec.argv,
                source,
                execution,
                command_cwd=command_cwd,
            )
            sandbox_argv, sandbox_backend = _sandboxed_argv(
                spec.argv,
                execution=execution,
                executable=executable,
                immutable_files=tuple(initial_manifest),
                mode=self.sandbox_mode,
            )
            env = _minimal_environment(execution, spec.env)
            env.update(
                {
                    "LOOP_ENGINEER_VERIFICATION_RUN_ID": run_id,
                    "LOOP_ENGINEER_VERIFICATION_GATE": gate.value,
                    "LOOP_ENGINEER_VERIFICATION_VARIANT": variant.value,
                    "LOOP_ENGINEER_CANDIDATE_REF": candidate_ref,
                }
            )
            if scenario_id:
                env["LOOP_ENGINEER_VERIFICATION_SCENARIO_ID"] = scenario_id
            process = await asyncio.create_subprocess_exec(
                *sandbox_argv,
                cwd=str(command_cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
            stdout_task = asyncio.create_task(
                _capture_stream(process.stdout, self.max_output_bytes)
            )
            stderr_task = asyncio.create_task(
                _capture_stream(process.stderr, self.max_output_bytes)
            )
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=spec.timeout_ms / 1000
                )
                exit_code = process.returncode
                # A command may let its leader exit while leaving children alive.
                # Kill the isolated group before waiting for inherited pipe FDs.
                await _terminate_process_group(process)
            except TimeoutError:
                timed_out = True
                await _terminate_process_group(process)
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)

            post_manifest = await asyncio.to_thread(
                workspace_manifest, execution, self.workspace_ignore
            )
            modified = [
                relative
                for relative, metadata in initial_manifest.items()
                if post_manifest.get(relative) != metadata
            ]
            if modified:
                error = "verification command modified locked snapshot files: " + ", ".join(
                    modified[:10]
                )
        except asyncio.CancelledError:
            if process is not None:
                try:
                    await _terminate_process_group(process)
                except Exception:
                    pass
            capture_tasks = [
                task for task in (stdout_task, stderr_task) if task is not None
            ]
            if capture_tasks:
                for task in capture_tasks:
                    task.cancel()
                await asyncio.gather(*capture_tasks, return_exceptions=True)
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            cleanup_failed = False
            if process is not None:
                try:
                    await _terminate_process_group(process)
                except Exception as cleanup_exc:
                    cleanup_failed = True
                    error += (
                        "; process cleanup failed: "
                        f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                    )
            capture_tasks = [
                task for task in (stdout_task, stderr_task) if task is not None
            ]
            if capture_tasks:
                if cleanup_failed:
                    for task in capture_tasks:
                        task.cancel()
                captured = await asyncio.gather(
                    *capture_tasks, return_exceptions=True
                )
                for task, result in zip(capture_tasks, captured, strict=True):
                    if isinstance(result, BaseException):
                        error += f"; stream capture failed: {type(result).__name__}: {result}"
                    elif task is stdout_task:
                        stdout = result
                    else:
                        stderr = result
        finally:
            try:
                after = await asyncio.to_thread(
                    workspace_digest, source, self.workspace_ignore
                )
            except Exception as exc:
                error = error or f"{type(exc).__name__}: {exc}"
            if execution is not None:
                await asyncio.to_thread(shutil.rmtree, execution.parent, True)

        failures: list[str] = []
        if timed_out:
            failures.append(f"timeout after {spec.timeout_ms}ms")
        if error:
            failures.append(error)
        if stdout.truncated or stderr.truncated:
            failures.append("command output exceeded capture limit")
        if stdout.encoding != "utf-8" or stderr.encoding != "utf-8":
            failures.append("command output is not valid UTF-8")
        if exit_code is not None and exit_code != spec.expected_exit_code:
            failures.append(
                f"exit code {exit_code}, expected {spec.expected_exit_code}"
            )
        for marker in spec.stdout_contains:
            if marker not in stdout.text:
                failures.append(f"stdout missing marker: {marker}")
        for marker in spec.stderr_contains:
            if marker not in stderr.text:
                failures.append(f"stderr missing marker: {marker}")
        combined = stdout.text + "\n" + stderr.text
        for pattern in effective_forbidden:
            if re.search(pattern, combined):
                failures.append(f"output matched forbidden pattern: {pattern}")
        if before != after:
            failures.append("candidate workspace changed during verification command")
        if exit_code is None and not timed_out and error is None:
            failures.append("command completed without exit code")

        return CommandEvidence(
            run_id=run_id,
            cycle=cycle,
            gate=gate,
            check_id=spec.id,
            scenario_id=scenario_id,
            skill_name=skill_name,
            variant=variant,
            policy_digest=policy_digest,
            skill_digest=skill_digest,
            command_spec_digest=command_contract_digest(
                spec, additional_forbidden_patterns
            ),
            command_spec=spec,
            additional_forbidden_patterns=additional_forbidden_patterns,
            candidate_ref=candidate_ref,
            candidate_digest_before=before,
            candidate_digest_after=after,
            argv=spec.argv,
            cwd=spec.cwd,
            exit_code=exit_code,
            stdout=stdout.text,
            stderr=stderr.text,
            stdout_encoding=stdout.encoding,
            stderr_encoding=stderr.encoding,
            stdout_sha256=stdout.digest,
            stderr_sha256=stderr.digest,
            stdout_bytes=stdout.byte_count,
            stderr_bytes=stderr.byte_count,
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            sandbox_backend=sandbox_backend,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            timed_out=timed_out,
            error=error,
            expected_exit_code=spec.expected_exit_code,
            expected_stdout_contains=spec.stdout_contains,
            expected_stderr_contains=spec.stderr_contains,
            forbidden_output_patterns=effective_forbidden,
            passed=not failures,
            failures=tuple(failures),
        )


__all__ = [
    "CommandRunner",
    "WorkspaceDigestError",
    "command_contract_digest",
    "workspace_digest",
    "workspace_manifest",
]
