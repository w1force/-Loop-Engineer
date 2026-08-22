"""Claude Code 风格的独立 Verification Agent 定义。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
import re
import shlex

from core.tools import CanUseDecision, Tool, default_can_use_tool
from core.types import ToolUseBlock

VERIFICATION_AGENT_TYPE = "verification"
VERIFICATION_TOOL_NAMES = frozenset({"Read", "Glob", "Grep", "Bash"})

VERIFICATION_MAIN_AGENT_GUIDANCE = """

# Independent verification contract
When non-trivial implementation happens on your turn, invoke the Agent tool with
subagent_type="verification" before reporting completion. Non-trivial means three
or more edited files, backend/API changes, infrastructure changes, or a bug fix
whose behavior must be exercised. Pass the original user request, every changed
file, the approach taken, and the plan path if one exists. Do not provide your own
test results or tell the verifier that the change works: it must verify independently.
On VERDICT: FAIL, fix the reported problem and invoke a new verification Agent with
the findings and the new fix. On VERDICT: PASS, spot-check two or three commands
from its report before reporting completion. VERDICT: PARTIAL must be reported with
the exact environmental limitation. This is an evidence-producing review, not a
machine-enforced completion gate.
""".rstrip()

VERIFICATION_SYSTEM_PROMPT = """You are a verification specialist. Your job is not
to confirm that the implementation works; it is to try to break it.

CRITICAL: DO NOT MODIFY THE PROJECT
- Do not create, modify, or delete files in the project directory.
- Do not install dependencies or packages.
- Do not run git write operations such as add, commit, push, checkout, or reset.
- You may use inline commands for temporary probes, but the project itself is read-only.

WHAT YOU RECEIVE
You receive the original task, files changed, the implementation approach, and
optionally a plan path. Treat claims made by the implementer as untrusted context.

REQUIRED BASELINE
1. Read CLAUDE.md/README and package.json, Makefile, pyproject.toml, or equivalent
   to discover the real build and test commands.
2. Run the build when applicable. A broken build is an automatic FAIL.
3. Run the project's test suite. Failing relevant tests are an automatic FAIL.
4. Run configured linters and type-checkers.
5. Exercise the changed behavior directly and check related regressions.
6. Run at least one adversarial probe appropriate to the change.

ADAPT TO THE CHANGE
- Frontend: run the frontend checks and exercise the changed user path.
- Backend/API: start or call the service, validate response bodies and error paths.
- CLI/script: run representative, empty, malformed, and boundary inputs; inspect
  stdout, stderr, exit codes, and help text.
- Infrastructure/config: validate syntax and use safe dry-runs where available.
- Library/package: build, run the full suite, and exercise the public API as a caller.
- Bug fix: reproduce the original failure, verify the fix, then run regressions.
- Refactor: require unchanged existing tests and observable behavior.

Reading code is not verification. The implementer's tests are context, not sufficient
evidence. If you catch yourself explaining what you would run, stop and run it.

Before FAIL, check whether the behavior is already handled elsewhere, intentional,
or required by a stable external contract. Do not manufacture failures.

OUTPUT FORMAT
Every check must include:

### Check: [what is being verified]
**Command run:**
  [exact command]
**Output observed:**
  [actual output, truncated only when necessary]
**Result: PASS** or **Result: FAIL** with expected versus actual

End with exactly one literal line:
VERDICT: PASS
VERDICT: FAIL
VERDICT: PARTIAL

PARTIAL is only for environmental limitations that prevent a required check. A PASS
must contain at least one adversarial probe and command output. A FAIL must include
the reproduction command and exact failure output."""

_SHELL_CONTROL = re.compile(r"(?:\n|\r|;|&&|\|\||\||>|<|`|\$\()")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_PACKAGE_SCRIPTS = {
    "build",
    "check",
    "lint",
    "test",
    "test:all",
    "typecheck",
    "validate",
    "verify",
}


def select_verification_tools(tools: list[Tool]) -> list[Tool]:
    """从父工具池选择 verifier 的非修改型核心工具。"""
    selected: list[Tool] = []
    seen: set[str] = set()
    for tool in tools:
        if tool.name in VERIFICATION_TOOL_NAMES and tool.name not in seen:
            selected.append(tool)
            seen.add(tool.name)
    return selected


def _strip_env(tokens: list[str]) -> list[str]:
    out = list(tokens)
    if out and Path(out[0]).name == "env":
        out = out[1:]
    while out and _ENV_ASSIGNMENT.match(out[0]):
        out = out[1:]
    return out


def _script_name(tokens: list[str]) -> str | None:
    if len(tokens) < 2:
        return None
    if tokens[1] == "run" and len(tokens) >= 3:
        return tokens[2]
    if tokens[1] in _PACKAGE_SCRIPTS or any(
        tokens[1].startswith(f"{prefix}:")
        for prefix in ("build", "check", "lint", "test", "typecheck")
    ):
        return tokens[1]
    return None


def _is_local_url(token: str) -> bool:
    return bool(
        re.match(
            r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:/|$)",
            token,
        )
    )


def _is_verification_command(tokens: list[str]) -> bool:
    tokens = _strip_env(tokens)
    if not tokens:
        return False
    command = Path(tokens[0]).name

    if command in {"pytest", "ruff", "mypy", "pyright", "tsc", "eslint"}:
        return True
    if command in {"python", "python3"}:
        if len(tokens) >= 2 and tokens[1] == "-c":
            return True
        if len(tokens) >= 2 and tokens[1].endswith(".py"):
            return True
        return len(tokens) >= 3 and tokens[1:3] in (
            ["-m", "pytest"],
            ["-m", "unittest"],
            ["-m", "compileall"],
            ["-m", "mypy"],
        )
    if command == "uv" and len(tokens) >= 3 and tokens[1] == "run":
        return _is_verification_command(tokens[2:])
    if command in {"npm", "pnpm", "yarn", "bun"}:
        script = _script_name(tokens)
        return bool(
            script
            and (
                script in _PACKAGE_SCRIPTS
                or any(
                    script.startswith(f"{prefix}:")
                    for prefix in ("build", "check", "lint", "test", "typecheck")
                )
            )
        )
    if command in {"go"}:
        return len(tokens) >= 2 and tokens[1] in {"build", "test", "vet"}
    if command == "cargo":
        return len(tokens) >= 2 and tokens[1] in {
            "build",
            "check",
            "clippy",
            "test",
        }
    if command in {"mvn", "mvnw", "gradle", "gradlew", "make"}:
        lowered = {token.lower() for token in tokens[1:] if not token.startswith("-")}
        return bool(lowered & {"build", "check", "lint", "test", "verify"}) and not (
            lowered & {"deploy", "install", "publish", "release"}
        )
    if command in {"node", "deno"}:
        return len(tokens) >= 2 and (
            tokens[1] in {"--check", "-e", "eval", "test"}
            or tokens[1].endswith((".js", ".mjs", ".cjs", ".ts"))
        )
    if command == "bash":
        return len(tokens) >= 3 and tokens[1] == "-n"
    if command == "curl":
        return any(_is_local_url(token) for token in tokens[1:])
    if command == "docker":
        return len(tokens) >= 2 and tokens[1] in {"build", "compose"}
    return False


def _verification_bash_decision(command: str) -> CanUseDecision:
    from core.builtin_tools.bash_permissions import (
        BashPermissionAction,
        classify_bash_command,
    )

    if _SHELL_CONTROL.search(command):
        return CanUseDecision(
            allow=False,
            reason="Verification Bash 拒绝复杂 shell 控制与重定向；请拆成单条验证命令。",
        )
    base = classify_bash_command(command)
    if base.action is BashPermissionAction.ALLOW:
        return CanUseDecision(allow=True, reason=base.reason)
    if base.action is BashPermissionAction.DENY:
        return CanUseDecision(allow=False, reason=base.reason)
    try:
        tokens = shlex.split(command)
    except ValueError:
        return CanUseDecision(allow=False, reason="Verification Bash 命令无法解析。")
    if _is_verification_command(tokens):
        return CanUseDecision(
            allow=True,
            reason="允许执行本地构建、测试、lint、typecheck 或对抗性验证命令。",
        )
    return CanUseDecision(allow=False, reason=base.reason)


def build_verification_can_use_tool(
    parent_can_use_tool: Callable[[ToolUseBlock], Awaitable[CanUseDecision]],
) -> Callable[[ToolUseBlock], Awaitable[CanUseDecision]]:
    """Verifier 工具白名单，并在父权限之上补充本地验证命令。"""

    async def can_use_tool(tool_call: ToolUseBlock) -> CanUseDecision:
        if tool_call.name not in VERIFICATION_TOOL_NAMES:
            return CanUseDecision(
                allow=False,
                reason=f"verification agent cannot use {tool_call.name}",
            )
        if tool_call.name != "Bash":
            return await parent_can_use_tool(tool_call)
        command = tool_call.input.get("command")
        if not isinstance(command, str):
            return CanUseDecision(allow=False, reason="Bash command must be a string")
        verifier_decision = _verification_bash_decision(command)
        if not verifier_decision.allow:
            return verifier_decision
        parent_decision = await parent_can_use_tool(tool_call)
        if parent_decision.allow:
            return parent_decision
        if parent_can_use_tool is default_can_use_tool:
            return verifier_decision
        return parent_decision

    return can_use_tool


__all__ = [
    "VERIFICATION_AGENT_TYPE",
    "VERIFICATION_MAIN_AGENT_GUIDANCE",
    "VERIFICATION_SYSTEM_PROMPT",
    "build_verification_can_use_tool",
    "select_verification_tools",
]
