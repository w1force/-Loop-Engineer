"""MCP onboarding diagnostics.

Doctor checks are intentionally read-only: they report missing dependencies or
bad config, but never install packages or rewrite user files.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from .types import MCPServerConfig, MCPTransport

JPROFILER_PACKAGE = "@ej-technologies/jprofiler-mcp@latest"
JPROFILER_TIMEOUT_MIN_SECONDS = 120


@dataclass(frozen=True)
class CheckResult:
    """One local diagnostic result."""

    name: str
    ok: bool
    message: str
    remediation: str | None = None


def check_executable(name: str) -> CheckResult:
    """Check whether an executable is visible in PATH."""

    resolved = shutil.which(name)
    if resolved:
        return CheckResult(name=name, ok=True, message=f"found at {resolved}")
    return CheckResult(
        name=name,
        ok=False,
        message=f"{name} not found in PATH",
        remediation=f"Install {name} or update PATH before starting this MCP server.",
    )


def check_jprofiler_config(configs: list[MCPServerConfig]) -> list[CheckResult]:
    """Validate the JProfiler MCP server config without starting it."""

    cfg = _find_jprofiler_config(configs)
    if cfg is None:
        return [
            CheckResult(
                name="JProfiler config",
                ok=False,
                message="JProfiler MCP server is not configured",
                remediation=(
                    "Copy .mcp.jprofiler.example.json to .mcp.json and set "
                    "LOOP_ENGINEER_MCP_CONFIG_PATH=.mcp.json."
                ),
            )
        ]

    results: list[CheckResult] = []
    results.append(_check_jprofiler_shape(cfg))
    if cfg.command == "npx":
        results.append(check_executable("npx"))
        results.append(_check_npx_package(cfg))
    else:
        results.append(_check_jpmcp_executable(cfg.command))
    results.append(_check_timeout(cfg))
    return _dedupe_results(results)


def _find_jprofiler_config(
    configs: list[MCPServerConfig],
) -> MCPServerConfig | None:
    for cfg in configs:
        if cfg.name.lower() == "jprofiler":
            return cfg
    for cfg in configs:
        if "jprofiler" in " ".join([cfg.command, *cfg.args]).lower():
            return cfg
    return None


def _check_jprofiler_shape(cfg: MCPServerConfig) -> CheckResult:
    if cfg.disabled:
        return CheckResult(
            name="JProfiler config",
            ok=False,
            message="JProfiler MCP server is disabled",
            remediation="Remove disabled=true before expecting JProfiler tools.",
        )
    if cfg.transport is not MCPTransport.STDIO:
        return CheckResult(
            name="JProfiler config",
            ok=False,
            message=f"JProfiler MCP uses unsupported transport: {cfg.transport.value}",
            remediation="Use stdio transport for the official JProfiler MCP server.",
        )
    return CheckResult(
        name="JProfiler config",
        ok=True,
        message=f"stdio server configured as {cfg.command}",
    )


def _check_npx_package(cfg: MCPServerConfig) -> CheckResult:
    if JPROFILER_PACKAGE in cfg.args:
        return CheckResult(
            name="JProfiler npm package",
            ok=True,
            message=f"uses official package {JPROFILER_PACKAGE}",
        )
    return CheckResult(
        name="JProfiler npm package",
        ok=False,
        message="npx command does not reference the official JProfiler MCP package",
        remediation=(
            "Use args ['-y', '@ej-technologies/jprofiler-mcp@latest'] unless "
            "you intentionally pin a reviewed version."
        ),
    )


def _check_jpmcp_executable(command: str) -> CheckResult:
    if command == "/path/to/JProfiler/bin/jpmcp":
        return CheckResult(
            name="JProfiler jpmcp",
            ok=False,
            message="local JProfiler command is still the placeholder path",
            remediation=(
                "Copy .mcp.jprofiler.local.example.json to .mcp.json and replace "
                "/path/to/JProfiler/bin/jpmcp with your real JProfiler executable."
            ),
        )
    if "/" in command:
        path = Path(command)
        if path.is_file():
            return CheckResult(
                name="JProfiler jpmcp",
                ok=True,
                message=f"found executable path {path}",
            )
        return CheckResult(
            name="JProfiler jpmcp",
            ok=False,
            message=f"configured executable does not exist: {path}",
            remediation="Check the JProfiler install path and update .mcp.json.",
        )
    resolved = shutil.which(command)
    if resolved:
        return CheckResult(
            name="JProfiler jpmcp",
            ok=True,
            message=f"found executable at {resolved}",
        )
    return CheckResult(
        name="JProfiler jpmcp",
        ok=False,
        message=f"{command} not found in PATH",
        remediation="Install JProfiler or use the npx wrapper example.",
    )


def _check_timeout(cfg: MCPServerConfig) -> CheckResult:
    if cfg.timeout >= JPROFILER_TIMEOUT_MIN_SECONDS:
        return CheckResult(
            name="JProfiler timeout",
            ok=True,
            message=f"timeout is {cfg.timeout:g}s",
        )
    return CheckResult(
        name="JProfiler timeout",
        ok=False,
        message=f"timeout is only {cfg.timeout:g}s",
        remediation=(
            f"Use at least {JPROFILER_TIMEOUT_MIN_SECONDS}s because first startup "
            "or profiling work may be slow."
        ),
    )


def _dedupe_results(results: list[CheckResult]) -> list[CheckResult]:
    seen: set[tuple[str, str]] = set()
    deduped: list[CheckResult] = []
    for result in results:
        key = (result.name, result.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(result)
    return deduped
