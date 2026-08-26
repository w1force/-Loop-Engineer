"""Verification Skill 的严格配置、证据和结论模型。

这里的 PASS 只来自机器证据。LLM 文本、修复者自报结果和普通 Bash tool_result
都不能构造最终放行结论。
"""

from __future__ import annotations

import base64
from enum import Enum
import fnmatch
from hashlib import sha256
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any, Literal, Self
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


ARTICLE_MAX_INPUT_TOKENS = 150_000
ARTICLE_MAX_VERIFICATION_ATTEMPTS = 3
ARTICLE_LINT_WARNING_PATTERNS = (
    (
        r"(?im)^(?!.*(?:\b(?:no|zero|0)\s+warnings?\b|"
        r"\bwarnings?\s*(?::|=|\()\s*(?:none|zero|0)\b\s*\)?|"
        r"\bwarning_count\s*[:=]\s*0\b)"
        r"\s*[\).,;:]*\s*(?:in\s+\S+(?:\s+\S+)*)?\s*$)"
        r".*(?:\bwarn(?:ing)?s?\b|\bwarning_count\b).*$"
    ),
)
_ALLOWED_WORKSPACE_IGNORE = frozenset(
    {
        ".git/**",
        ".loop-engineer/**",
        ".pytest_cache/**",
        "**/__pycache__/**",
        "**/*.pyc",
        ".venv/**",
        ".venv-*/**",
        "node_modules/**",
    }
)
_PROTECTED_BEHAVIOR_PATHS = ("@model", "@tool_calls", "@finished", "@outcome")
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _validate_skill_digest_map(value: dict[str, str]) -> dict[str, str]:
    if not value:
        raise ValueError("skill_digests 不能为空")
    if any(not _SKILL_NAME.fullmatch(name) for name in value):
        raise ValueError("skill_digests 包含非法 Skill 名称")
    if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in value.values()):
        raise ValueError("skill_digests 必须是 SHA-256")
    return value


def _validate_scenario_input_digest_map(value: dict[str, str]) -> dict[str, str]:
    if not value:
        raise ValueError("scenario_input_digests 不能为空")
    if any(not scenario_id.strip() for scenario_id in value):
        raise ValueError("scenario_input_digests 包含空场景 ID")
    if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in value.values()):
        raise ValueError("scenario_input_digests 必须是 SHA-256")
    return value


class VerificationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class ReplayWindowBinding(VerificationModel):
    """Immutable pointer from a replay receipt to one evidence window."""

    scenario_id: str = Field(min_length=1)
    variant: "Variant"
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_id: str = Field(min_length=1)
    otlp_barrier_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    oracle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplayEvidenceManifest(VerificationModel):
    schema_version: Literal["verification-replay-evidence-manifest/v1"] = (
        "verification-replay-evidence-manifest/v1"
    )
    windows: tuple[ReplayWindowBinding, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _complete_pairs(self) -> Self:
        keys = [(item.scenario_id, item.variant) for item in self.windows]
        if len(keys) != len(set(keys)):
            raise ValueError("replay manifest 场景窗口不能重复")
        collection_ids = [item.collection_id for item in self.windows]
        if len(collection_ids) != len(set(collection_ids)):
            raise ValueError("replay manifest collection_id 不能重复")
        scenario_ids = {item.scenario_id for item in self.windows}
        expected = {
            (scenario_id, variant)
            for scenario_id in scenario_ids
            for variant in (Variant.CONTROL, Variant.CANDIDATE)
        }
        if set(keys) != expected:
            raise ValueError("replay manifest 必须完整覆盖 control/candidate")
        for scenario_id in scenario_ids:
            digests = {
                item.input_digest
                for item in self.windows
                if item.scenario_id == scenario_id
            }
            if len(digests) != 1:
                raise ValueError("replay manifest 的 control/candidate 输入摘要不一致")
        return self

    @property
    def by_key(self) -> dict[tuple[str, "Variant"], ReplayWindowBinding]:
        return {(item.scenario_id, item.variant): item for item in self.windows}


class GateKind(str, Enum):
    """文章的六层验证与 Step 5 Trace 门禁合并后的七个逻辑门禁。"""

    LINT = "lint"
    UNIT = "unit"
    INTEGRATION = "integration"
    TRACE = "trace"
    STAGING_LOG = "staging_log"
    BEHAVIOR_COMPARE = "behavior_compare"
    UI = "ui"


class GateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"
    SKIPPED = "skipped"


class VerificationVerdict(str, Enum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    ERROR = "error"


class Variant(str, Enum):
    CONTROL = "control"
    CANDIDATE = "candidate"


def _validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("path 必须是工作区内的相对路径")
    return value


class CommandSpec(VerificationModel):
    """由受信配置冻结的单条命令；禁止 shell 字符串。"""

    id: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = "."
    timeout_ms: StrictInt = Field(default=120_000, ge=100, le=900_000)
    expected_exit_code: StrictInt = 0
    stdout_contains: tuple[str, ...] = ()
    stderr_contains: tuple[str, ...] = ()
    forbidden_output_patterns: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    assertion_categories: tuple[
        Literal["regression", "boundary", "side_effect"], ...
    ] = ()

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not token or "\x00" in token for token in value):
            raise ValueError("argv 不能包含空 token 或 NUL")
        for index, token in enumerate(value[:-1]):
            if PurePosixPath(token).name in {"sh", "bash", "zsh", "dash", "fish"}:
                if value[index + 1] in {"-c", "-lc"}:
                    raise ValueError("禁止通过 shell -c/-lc 执行命令字符串")
        return value

    @field_validator("cwd")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        return _validate_relative_path(value)

    @field_validator("env")
    @classmethod
    def _validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        reserved = {
            "BASH_ENV",
            "CI",
            "ENV",
            "HOME",
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
            "NODE_OPTIONS",
            "PATH",
            "PWD",
            "PYTHONHOME",
            "PYTHONPATH",
            "RUBYOPT",
            "SHELLOPTS",
            "TMPDIR",
        }
        invalid = [
            key
            for key in value
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or "\x00" in value[key]
            or key in reserved
            or key.startswith("DYLD_")
        ]
        if invalid:
            raise ValueError("env 包含非法键或 NUL")
        return value

    @field_validator("forbidden_output_patterns")
    @classmethod
    def _compile_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            re.compile(pattern)
        return value

    @field_validator("stdout_contains", "stderr_contains")
    @classmethod
    def _non_empty_output_markers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not marker.strip() for marker in value):
            raise ValueError("输出匹配标记不能为空")
        return value

    @field_validator("assertion_categories")
    @classmethod
    def _unique_assertion_categories(
        cls,
        value: tuple[Literal["regression", "boundary", "side_effect"], ...],
    ) -> tuple[Literal["regression", "boundary", "side_effect"], ...]:
        if len(value) != len(set(value)):
            raise ValueError("assertion_categories 不能重复")
        return value

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


def command_contract_digest(
    spec: CommandSpec, additional_forbidden_patterns: tuple[str, ...] = ()
) -> str:
    """Bind evidence to the complete frozen command contract.

    Lint's non-configurable warning patterns are external to ``CommandSpec`` and
    therefore participate in the same digest explicitly.
    """

    payload = {
        "command": spec.model_dump(mode="json"),
        "additional_forbidden_patterns": additional_forbidden_patterns,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ScenarioSpec(VerificationModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    steps: tuple[CommandSpec, ...] = Field(min_length=1)

    @field_validator("steps")
    @classmethod
    def _unique_step_ids(
        cls, value: tuple[CommandSpec, ...]
    ) -> tuple[CommandSpec, ...]:
        ids = [step.id for step in value]
        if len(ids) != len(set(ids)):
            raise ValueError("scenario step id 不能重复")
        return value

    @model_validator(mode="after")
    def _complete_explicit_assertion_categories(self) -> Self:
        categorized = [bool(step.assertion_categories) for step in self.steps]
        if any(categorized) and not all(categorized):
            raise ValueError(
                "显式 assertion 分类时，每个 scenario step 都必须声明类别"
            )
        return self

    @property
    def trusted_assertion_groups(self) -> dict[str, tuple[str, ...]]:
        """Derive assertion ownership from the trusted scenario definition.

        Legacy Skills without explicit categories conservatively require every
        step in all three groups.  Once categories are declared, every step is
        classified by the Skill rather than by a planning Agent.
        """

        explicit_categories = any(step.assertion_categories for step in self.steps)
        return {
            field_name: tuple(
                step.id
                for step in self.steps
                if not explicit_categories or category in step.assertion_categories
            )
            for field_name, category in (
                ("regression_assertions", "regression"),
                ("boundary_assertions", "boundary"),
                ("side_effect_assertions", "side_effect"),
            )
        }


class VerificationSkillSpec(VerificationModel):
    """skills/<name>/verification.yaml 的机器可执行契约。"""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    integration: tuple[ScenarioSpec, ...] = Field(min_length=1)
    ui: tuple[ScenarioSpec, ...] = ()

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _SKILL_NAME.fullmatch(value):
            raise ValueError("skill name 只能包含字母、数字、点、下划线和连字符")
        return value

    @field_validator("integration", "ui")
    @classmethod
    def _unique_scenario_ids(
        cls, value: tuple[ScenarioSpec, ...]
    ) -> tuple[ScenarioSpec, ...]:
        ids = [scenario.id for scenario in value]
        if len(ids) != len(set(ids)):
            raise ValueError("scenario id 不能重复")
        return value

    @model_validator(mode="after")
    def _globally_unique_scenarios(self) -> Self:
        ids = [item.id for item in (*self.integration, *self.ui)]
        if len(ids) != len(set(ids)):
            raise ValueError("integration 与 ui scenario id 不能重复")
        return self


class ResolvedVerificationSkill(VerificationModel):
    name: str
    spec: VerificationSkillSpec
    instructions: str = Field(min_length=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    directory: str = Field(min_length=1)


class FrozenVerificationSkill(VerificationModel):
    """Small, serializable snapshot used to recompute a persisted report."""

    name: str = Field(min_length=1)
    spec: VerificationSkillSpec
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _name_matches_spec(self) -> Self:
        if self.name != self.spec.name:
            raise ValueError("冻结 Skill 名称与 verification.yaml 不一致")
        return self


class LintGateSpec(VerificationModel):
    checks: tuple[CommandSpec, ...] = Field(min_length=1)
    # 内置模式不可被配置覆盖；调用方只能追加更精确的 linter 模式。
    warning_patterns: tuple[str, ...] = ARTICLE_LINT_WARNING_PATTERNS

    @field_validator("warning_patterns")
    @classmethod
    def _require_warning_detection(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            re.compile(pattern)
        return tuple(dict.fromkeys((*ARTICLE_LINT_WARNING_PATTERNS, *value)))

    @field_validator("checks")
    @classmethod
    def _unique_checks(cls, value: tuple[CommandSpec, ...]) -> tuple[CommandSpec, ...]:
        if len({item.id for item in value}) != len(value):
            raise ValueError("lint check id 不能重复")
        if any(item.expected_exit_code != 0 for item in value):
            raise ValueError("lint 硬门禁要求所有命令 expected_exit_code=0")
        return value


class UnitGateSpec(VerificationModel):
    checks: tuple[CommandSpec, ...] = Field(min_length=1)
    scope: Literal["full"] = "full"

    @field_validator("checks")
    @classmethod
    def _unique_checks(cls, value: tuple[CommandSpec, ...]) -> tuple[CommandSpec, ...]:
        if len({item.id for item in value}) != len(value):
            raise ValueError("unit check id 不能重复")
        if any(item.expected_exit_code != 0 for item in value):
            raise ValueError("全量单测硬门禁要求所有命令 expected_exit_code=0")
        return value


class TraceGateSpec(VerificationModel):
    expected_model: str = Field(min_length=1)
    max_input_tokens: StrictInt = Field(
        default=ARTICLE_MAX_INPUT_TOKENS,
        gt=0,
        le=ARTICLE_MAX_INPUT_TOKENS,
    )
    allowed_fallback_scenarios: tuple[str, ...] = ()


class LogGateSpec(VerificationModel):
    error_levels: tuple[str, ...] = ("ERROR", "FATAL")

    @field_validator("error_levels")
    @classmethod
    def _non_empty_levels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.upper() for item in value))
        if "ERROR" not in normalized:
            raise ValueError("预发日志硬门禁不能移除 ERROR")
        return normalized


class BehaviorScenarioSpec(VerificationModel):
    scenario_id: str = Field(min_length=1)
    expected_control_outcome: Literal["success", "failure"] | None = None
    expected_candidate_outcome: Literal["success"] = "success"
    allowed_changed_paths: tuple[str, ...] = ()
    required_changed_paths: tuple[str, ...] = ()
    forbidden_changed_paths: tuple[str, ...] = ()
    reproducer: StrictBool = False
    require_trace_shape: Literal[True] = True

    @field_validator("forbidden_changed_paths")
    @classmethod
    def _valid_forbidden_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not pattern for pattern in value):
            raise ValueError("forbidden_changed_paths 不能包含空值")
        if len(value) != len(set(value)):
            raise ValueError("forbidden_changed_paths 不能重复")
        return value

    @model_validator(mode="after")
    def _reproducer_has_expected_change(self) -> Self:
        if self.reproducer and self.expected_control_outcome != "failure":
            raise ValueError(
                "reproducer 必须明确声明 control=failure、candidate=success"
            )
        if (
            not self.reproducer
            and self.expected_control_outcome is not None
            and self.expected_control_outcome != self.expected_candidate_outcome
        ):
            raise ValueError("control/candidate outcome 变化必须显式标记 reproducer")
        if not set(self.required_changed_paths).issubset(self.allowed_changed_paths):
            raise ValueError("required_changed_paths 必须同时列入 allowed_changed_paths")
        overlap = set(self.allowed_changed_paths) & set(self.forbidden_changed_paths)
        if overlap:
            raise ValueError("行为路径不能同时 allowed 和 forbidden")
        patterns = (*self.allowed_changed_paths, *self.required_changed_paths)
        protected = [
            pattern
            for pattern in patterns
            if any(fnmatch.fnmatchcase(path, pattern) for path in _PROTECTED_BEHAVIOR_PATHS)
        ]
        if protected:
            raise ValueError(
                "model、tool_calls、finished、outcome 是不可放宽的行为字段: "
                + ", ".join(protected)
            )
        return self


class BehaviorGateSpec(VerificationModel):
    scenarios: tuple[BehaviorScenarioSpec, ...] = Field(min_length=1)

    @field_validator("scenarios")
    @classmethod
    def _unique_scenarios(
        cls, value: tuple[BehaviorScenarioSpec, ...]
    ) -> tuple[BehaviorScenarioSpec, ...]:
        ids = [scenario.scenario_id for scenario in value]
        if len(ids) != len(set(ids)):
            raise ValueError("behavior scenario_id 不能重复")
        return value

    @model_validator(mode="after")
    def _requires_reproducer(self) -> Self:
        if not any(item.reproducer for item in self.scenarios):
            raise ValueError("行为对比至少需要一个显式 reproducer 场景")
        return self


class UIGateSpec(VerificationModel):
    mode: Literal["required", "not_applicable"] = "required"
    global_scenarios: tuple[ScenarioSpec, ...] = ()
    not_applicable_reason: str | None = None

    @field_validator("global_scenarios")
    @classmethod
    def _unique_global_scenario_ids(
        cls, value: tuple[ScenarioSpec, ...]
    ) -> tuple[ScenarioSpec, ...]:
        ids = [scenario.id for scenario in value]
        if len(ids) != len(set(ids)):
            raise ValueError("global UI scenario id 不能重复")
        return value

    @model_validator(mode="after")
    def _not_applicable_has_no_commands(self) -> Self:
        if self.mode == "not_applicable" and self.global_scenarios:
            raise ValueError("UI not_applicable 时不能配置 UI scenario")
        if self.mode == "not_applicable" and not self.not_applicable_reason:
            raise ValueError("UI not_applicable 必须给出受信配置理由")
        if self.mode == "required" and self.not_applicable_reason:
            raise ValueError("UI required 时不能设置 not_applicable_reason")
        return self


class VerificationPolicy(VerificationModel):
    """参数由后续配置层提供；缺少任何文章必需项时运行结果 BLOCKED。"""

    schema_version: Literal["verification-policy/v1"] = "verification-policy/v1"
    lint: LintGateSpec | None = None
    unit: UnitGateSpec | None = None
    trace: TraceGateSpec | None = None
    staging_log: LogGateSpec | None = None
    behavior: BehaviorGateSpec | None = None
    ui: UIGateSpec | None = None
    required_skills_by_rule: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    sandbox_mode: Literal["required"] = "required"
    evidence_timeout_ms: StrictInt = Field(default=120_000, ge=100, le=900_000)
    workspace_ignore: tuple[str, ...] = (
        ".git/**",
        ".loop-engineer/**",
        ".pytest_cache/**",
        "**/__pycache__/**",
        "**/*.pyc",
        ".venv/**",
        ".venv-*/**",
        "node_modules/**",
    )

    @field_validator("workspace_ignore")
    @classmethod
    def _only_known_generated_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("workspace_ignore 不能重复")
        invalid = sorted(set(value) - _ALLOWED_WORKSPACE_IGNORE)
        if invalid:
            raise ValueError(
                "workspace_ignore 只能忽略固定生成物，不能排除任意源码: "
                + ", ".join(invalid)
            )
        return value

    @field_validator("required_skills_by_rule")
    @classmethod
    def _valid_required_skills_by_rule(
        cls, value: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        for matched_rule, skill_names in value.items():
            if not matched_rule or matched_rule != matched_rule.strip():
                raise ValueError("required_skills_by_rule 包含非法 matched_rule")
            if not skill_names:
                raise ValueError(
                    f"matched_rule {matched_rule} 必须配置至少一个 required Skill"
                )
            if len(skill_names) != len(set(skill_names)):
                raise ValueError(
                    f"matched_rule {matched_rule} 的 required Skills 不能重复"
                )
            invalid = [
                name for name in skill_names if not _SKILL_NAME.fullmatch(name)
            ]
            if invalid:
                raise ValueError(
                    f"matched_rule {matched_rule} 包含非法 required Skill 名称"
                )
        return value

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


class ScenarioAssertionContract(VerificationModel):
    """Frozen per-scenario assertions selected before evidence collection."""

    scenario_id: str = Field(min_length=1)
    skill_name: str | None = Field(default=None, min_length=1)
    forbidden_changed_paths: tuple[str, ...] = ()
    regression_assertions: tuple[str, ...] = ()
    boundary_assertions: tuple[str, ...] = ()
    side_effect_assertions: tuple[str, ...] = ()

    @field_validator("skill_name")
    @classmethod
    def _valid_skill_name(cls, value: str | None) -> str | None:
        if value is not None and not _SKILL_NAME.fullmatch(value):
            raise ValueError("assertion contract 包含非法 Skill 名称")
        return value

    @field_validator(
        "forbidden_changed_paths",
        "regression_assertions",
        "boundary_assertions",
        "side_effect_assertions",
    )
    @classmethod
    def _non_empty_unique_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("assertion contract 不能包含空值")
        if len(value) != len(set(value)):
            raise ValueError("assertion contract 不能包含重复值")
        return value

    @model_validator(mode="after")
    def _skill_matches_scenario(self) -> Self:
        if self.skill_name is not None and not self.scenario_id.startswith(
            self.skill_name + ":"
        ):
            raise ValueError("assertion contract 的 scenario_id 与 skill_name 不一致")
        return self


def _validate_assertion_contract_coverage(
    contracts: tuple[ScenarioAssertionContract, ...],
    scenario_input_digests: dict[str, str],
    skill_names: tuple[str, ...],
) -> None:
    scenario_ids = [item.scenario_id for item in contracts]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("assertion_contracts 的 scenario_id 不能重复")
    if set(scenario_ids) != set(scenario_input_digests):
        missing = sorted(set(scenario_input_digests) - set(scenario_ids))
        extra = sorted(set(scenario_ids) - set(scenario_input_digests))
        raise ValueError(
            "assertion_contracts 必须精确覆盖 scenario_input_digests; "
            f"missing={missing}, extra={extra}"
        )
    unselected = sorted(
        {
            item.skill_name
            for item in contracts
            if item.skill_name is not None and item.skill_name not in skill_names
        }
    )
    if unselected:
        raise ValueError(
            "assertion_contracts 引用了未选中的 Skill: " + ", ".join(unselected)
        )


class VerificationRunRequest(VerificationModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    cycle: StrictInt = Field(default=1, ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    incident_id: str = Field(min_length=1)
    incident_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    replay_manifest: ReplayEvidenceManifest | None = None
    scenario_input_digests: dict[str, str] = Field(min_length=1)
    assertion_contracts: tuple[ScenarioAssertionContract, ...] = Field(min_length=1)
    workspace: str = Field(min_length=1)
    control_ref: str = Field(min_length=1)
    control_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_ref: str = Field(min_length=1)
    expected_candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_names: tuple[str, ...] = Field(min_length=1)
    expected_skill_digests: dict[str, str] = Field(min_length=1)

    @field_validator("run_id")
    @classmethod
    def _safe_run_id(cls, value: str) -> str:
        if not _RUN_ID.fullmatch(value):
            raise ValueError("run_id 必须是安全标识，不能包含路径分隔符")
        return value

    @field_validator("skill_names")
    @classmethod
    def _valid_skill_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("skill_names 不能重复")
        invalid = [name for name in value if not _SKILL_NAME.fullmatch(name)]
        if invalid:
            raise ValueError("skill_names 包含非法名称")
        return value

    @field_validator("scenario_input_digests")
    @classmethod
    def _valid_scenario_input_digests(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_scenario_input_digest_map(value)

    @model_validator(mode="after")
    def _skill_digests_match_names(self) -> Self:
        if set(self.expected_skill_digests) != set(self.skill_names):
            raise ValueError("expected_skill_digests 必须精确覆盖 skill_names")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.expected_skill_digests.values()):
            raise ValueError("expected_skill_digests 必须是 SHA-256")
        _validate_assertion_contract_coverage(
            self.assertion_contracts,
            self.scenario_input_digests,
            self.skill_names,
        )
        if self.replay_manifest is not None:
            manifest_inputs = {
                item.scenario_id: item.input_digest
                for item in self.replay_manifest.windows
            }
            if manifest_inputs != self.scenario_input_digests:
                raise ValueError("replay_manifest 未精确绑定冻结场景输入")
            if self.replay_digest is None:
                raise ValueError("replay_manifest 缺少 replay_digest")
        return self


class CommandEvidence(VerificationModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, frozen=True)

    evidence_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    run_id: str = Field(min_length=1)
    cycle: StrictInt = Field(default=1, ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    gate: GateKind
    check_id: str = Field(min_length=1)
    scenario_id: str | None = None
    skill_name: str | None = None
    variant: Variant = Variant.CANDIDATE
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    command_spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    command_spec: CommandSpec
    additional_forbidden_patterns: tuple[str, ...] = ()
    candidate_ref: str = Field(min_length=1)
    candidate_digest_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_digest_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = Field(min_length=1)
    exit_code: StrictInt | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_encoding: Literal["utf-8", "base64"] = "utf-8"
    stderr_encoding: Literal["utf-8", "base64"] = "utf-8"
    stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stdout_bytes: StrictInt = Field(ge=0)
    stderr_bytes: StrictInt = Field(ge=0)
    stdout_truncated: StrictBool = False
    stderr_truncated: StrictBool = False
    sandbox_backend: Literal["macos-seatbelt+workspace-copy", "unavailable"]
    duration_ms: StrictInt = Field(ge=0)
    timed_out: StrictBool = False
    error: str | None = None
    expected_exit_code: StrictInt = 0
    expected_stdout_contains: tuple[str, ...] = ()
    expected_stderr_contains: tuple[str, ...] = ()
    forbidden_output_patterns: tuple[str, ...] = ()
    passed: StrictBool
    failures: tuple[str, ...] = ()

    @field_validator("forbidden_output_patterns")
    @classmethod
    def _valid_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            re.compile(pattern)
        return value

    @field_validator("additional_forbidden_patterns")
    @classmethod
    def _valid_additional_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            re.compile(pattern)
        return value

    @model_validator(mode="after")
    def _validate_result(self) -> Self:
        expected_forbidden = (
            *self.command_spec.forbidden_output_patterns,
            *self.additional_forbidden_patterns,
        )
        if (
            self.command_spec_digest
            != command_contract_digest(
                self.command_spec, self.additional_forbidden_patterns
            )
            or self.argv != self.command_spec.argv
            or self.cwd != self.command_spec.cwd
            or self.expected_exit_code != self.command_spec.expected_exit_code
            or self.expected_stdout_contains != self.command_spec.stdout_contains
            or self.expected_stderr_contains != self.command_spec.stderr_contains
            or self.forbidden_output_patterns != expected_forbidden
        ):
            raise ValueError("命令证据与冻结 CommandSpec 契约不一致")
        for label, text, encoding, digest, byte_count, truncated in (
            (
                "stdout",
                self.stdout,
                self.stdout_encoding,
                self.stdout_sha256,
                self.stdout_bytes,
                self.stdout_truncated,
            ),
            (
                "stderr",
                self.stderr,
                self.stderr_encoding,
                self.stderr_sha256,
                self.stderr_bytes,
                self.stderr_truncated,
            ),
        ):
            try:
                captured = (
                    text.encode("utf-8")
                    if encoding == "utf-8"
                    else base64.b64decode(text, validate=True)
                )
            except (UnicodeEncodeError, ValueError) as exc:
                raise ValueError(f"{label} 不是有效的 {encoding} 证据") from exc
            if byte_count < len(captured):
                raise ValueError(f"{label}_bytes 小于已保存内容")
            if not truncated and (
                byte_count != len(captured) or sha256(captured).hexdigest() != digest
            ):
                raise ValueError(f"{label} hash/byte_count 与完整输出不一致")
        combined = self.stdout + "\n" + self.stderr
        expectation_failed = (
            self.exit_code != self.expected_exit_code
            or any(item not in self.stdout for item in self.expected_stdout_contains)
            or any(item not in self.stderr for item in self.expected_stderr_contains)
            or any(re.search(pattern, combined) for pattern in self.forbidden_output_patterns)
        )
        if self.passed and (
            self.timed_out
            or self.error is not None
            or self.exit_code is None
            or self.failures
            or self.stdout_truncated
            or self.stderr_truncated
            or self.stdout_encoding != "utf-8"
            or self.stderr_encoding != "utf-8"
            or self.sandbox_backend != "macos-seatbelt+workspace-copy"
            or self.candidate_digest_before != self.candidate_digest_after
            or expectation_failed
        ):
            raise ValueError("通过的命令证据必须完整、无错误且绑定未变化的 candidate")
        if not self.passed and not self.failures:
            raise ValueError("未通过的命令证据必须说明 failures")
        if self.exit_code is None and not self.timed_out and self.error is None:
            raise ValueError("缺少 exit_code 时必须记录 timeout 或 error")
        return self


class TraceObservation(VerificationModel):
    trace_id: str = Field(min_length=1)
    request_id: str | None = Field(default=None, min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    scenario_id: str = Field(min_length=1)
    input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    variant: Literal[Variant.CANDIDATE] = Variant.CANDIDATE
    error_observations: StrictInt | None = Field(default=None, ge=0)
    actual_model: str | None = None
    fallback_used: StrictBool | None = None
    input_tokens: StrictInt | None = Field(default=None, ge=0)
    finished: StrictBool | None = None


class TraceEvidence(VerificationModel):
    run_id: str = Field(min_length=1)
    cycle: StrictInt = Field(default=1, ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    candidate_ref: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_digests: dict[str, str] = Field(min_length=1)
    collection_complete: StrictBool
    observations: tuple[TraceObservation, ...] = ()
    collector_error: str | None = Field(default=None, min_length=1)

    @field_validator("skill_digests")
    @classmethod
    def _valid_skill_digests(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_skill_digest_map(value)


class LogObservation(VerificationModel):
    observation_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    scenario_id: str = Field(min_length=1)
    variant: Variant
    service: str = Field(min_length=1)
    level: str = Field(min_length=1)
    error_type: str = Field(min_length=1)
    event_code: str | None = None
    message_template: str | None = None
    business_frame: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    session_id: str | None = None

    @model_validator(mode="after")
    def _has_correlation_id(self) -> Self:
        if not self.request_id and not self.trace_id and not self.session_id:
            raise ValueError(
                "日志 observation 必须包含 request_id、trace_id 或 session_id"
            )
        return self


class ScenarioCollection(VerificationModel):
    """Provider proof that one scenario/variant observation window completed."""

    scenario_id: str = Field(min_length=1)
    variant: Variant
    collection_id: str = Field(min_length=1)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class LogEvidence(VerificationModel):
    run_id: str = Field(min_length=1)
    cycle: StrictInt = Field(default=1, ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    control_ref: str = Field(min_length=1)
    control_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_ref: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_digests: dict[str, str] = Field(min_length=1)
    collection_complete: StrictBool
    collected_variants: tuple[Variant, ...] = ()
    collected_scenarios: tuple[ScenarioCollection, ...] = ()
    observations: tuple[LogObservation, ...] = ()
    collector_error: str | None = Field(default=None, min_length=1)

    @field_validator("skill_digests")
    @classmethod
    def _valid_skill_digests(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_skill_digest_map(value)

    @model_validator(mode="after")
    def _unique_observations(self) -> Self:
        ids = [item.observation_id for item in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("日志 observation_id 不能重复")
        if len(self.collected_variants) != len(set(self.collected_variants)):
            raise ValueError("collected_variants 不能重复")
        coverage = [(item.scenario_id, item.variant) for item in self.collected_scenarios]
        if len(coverage) != len(set(coverage)):
            raise ValueError("日志场景采集窗口不能重复")
        collection_ids = [item.collection_id for item in self.collected_scenarios]
        if len(collection_ids) != len(set(collection_ids)):
            raise ValueError("日志 collection_id 不能重复")
        return self


class ToolCallObservation(VerificationModel):
    """Ordered tool behavior without storing potentially sensitive raw payloads."""

    sequence: StrictInt = Field(ge=0)
    tool_name: str = Field(min_length=1)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["success", "failure"]


class BehaviorObservation(VerificationModel):
    observation_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    scenario_id: str = Field(min_length=1)
    variant: Variant
    outcome: Literal["success", "failure"]
    payload: Any
    input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model: str | None = None
    tool_calls: tuple["ToolCallObservation", ...] | None = None
    finished: StrictBool | None = None
    request_id: str | None = None
    trace_id: str | None = None
    session_id: str | None = None

    @field_validator("payload")
    @classmethod
    def _payload_must_be_json(cls, value: Any) -> Any:
        def validate(item: Any, path: str) -> None:
            if item is None or isinstance(item, (bool, int, str)):
                return
            if isinstance(item, float):
                if not math.isfinite(item):
                    raise ValueError(f"payload {path} 包含非有限浮点数")
                return
            if isinstance(item, list):
                for index, child in enumerate(item):
                    validate(child, f"{path}[{index}]")
                return
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError(f"payload {path} 的对象键必须是字符串")
                for key, child in item.items():
                    validate(child, f"{path}.{key}")
                return
            raise ValueError(f"payload {path} 不是 JSON 值")

        validate(value, "$")
        return value

    @model_validator(mode="after")
    def _tool_calls_are_ordered(self) -> Self:
        if self.tool_calls is not None:
            actual = [item.sequence for item in self.tool_calls]
            if actual != list(range(len(self.tool_calls))):
                raise ValueError("tool_calls sequence 必须从 0 连续递增")
        return self


class BehaviorEvidence(VerificationModel):
    run_id: str = Field(min_length=1)
    cycle: StrictInt = Field(default=1, ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    control_ref: str = Field(min_length=1)
    control_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_ref: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_digests: dict[str, str] = Field(min_length=1)
    collection_complete: StrictBool
    collected_variants: tuple[Variant, ...] = ()
    collected_scenarios: tuple[ScenarioCollection, ...] = ()
    observations: tuple[BehaviorObservation, ...] = ()
    collector_error: str | None = Field(default=None, min_length=1)

    @field_validator("skill_digests")
    @classmethod
    def _valid_skill_digests(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_skill_digest_map(value)

    @model_validator(mode="after")
    def _unique_observations(self) -> Self:
        ids = [item.observation_id for item in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("行为 observation_id 不能重复")
        if len(self.collected_variants) != len(set(self.collected_variants)):
            raise ValueError("collected_variants 不能重复")
        coverage = [(item.scenario_id, item.variant) for item in self.collected_scenarios]
        if len(coverage) != len(set(coverage)):
            raise ValueError("行为场景采集窗口不能重复")
        collection_ids = [item.collection_id for item in self.collected_scenarios]
        if len(collection_ids) != len(set(collection_ids)):
            raise ValueError("行为 collection_id 不能重复")
        return self


class GateResult(VerificationModel):
    gate: GateKind
    status: GateStatus
    summary: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _pass_has_no_failure(self) -> Self:
        if self.status is GateStatus.PASS and self.failures:
            raise ValueError("PASS gate 不能包含 failures")
        if self.status in {GateStatus.FAIL, GateStatus.BLOCKED, GateStatus.ERROR}:
            if not self.failures:
                raise ValueError("未通过的 gate 必须说明原因")
        return self


class VerificationReport(VerificationModel):
    schema_version: Literal["verification-report/v1"] = "verification-report/v1"
    run_id: str
    cycle: StrictInt = Field(ge=1, le=ARTICLE_MAX_VERIFICATION_ATTEMPTS)
    incident_id: str = Field(min_length=1)
    incident_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    replay_manifest: ReplayEvidenceManifest | None = None
    scenario_input_digests: dict[str, str] = Field(min_length=1)
    assertion_contracts: tuple[ScenarioAssertionContract, ...] = Field(min_length=1)
    control_ref: str = Field(min_length=1)
    control_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_ref: str = Field(min_length=1)
    candidate_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_digest_after: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    skill_names: tuple[str, ...] = Field(min_length=1)
    skill_digests: dict[str, str] = Field(default_factory=dict)
    skill_contracts: tuple[FrozenVerificationSkill, ...] = ()
    policy: VerificationPolicy
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_results: tuple[GateResult, ...]
    command_evidence: tuple[CommandEvidence, ...] = ()
    trace_evidence: TraceEvidence | None = None
    log_evidence: LogEvidence | None = None
    behavior_evidence: BehaviorEvidence | None = None
    verdict: VerificationVerdict

    @field_validator("run_id")
    @classmethod
    def _safe_run_id(cls, value: str) -> str:
        if not _RUN_ID.fullmatch(value):
            raise ValueError("run_id 必须是安全标识，不能包含路径分隔符")
        return value

    @field_validator("skill_names")
    @classmethod
    def _valid_skill_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            not _SKILL_NAME.fullmatch(item) for item in value
        ):
            raise ValueError("skill_names 必须非空、合法且不重复")
        return value

    @field_validator("scenario_input_digests")
    @classmethod
    def _valid_scenario_input_digests(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_scenario_input_digest_map(value)

    @model_validator(mode="after")
    def _verdict_must_match_machine_gates(self) -> Self:
        if self.policy.digest != self.policy_digest:
            raise ValueError("report policy 内容与 policy_digest 不一致")
        _validate_assertion_contract_coverage(
            self.assertion_contracts,
            self.scenario_input_digests,
            self.skill_names,
        )
        if self.replay_manifest is not None:
            manifest_inputs = {
                item.scenario_id: item.input_digest
                for item in self.replay_manifest.windows
            }
            if manifest_inputs != self.scenario_input_digests:
                raise ValueError("report replay_manifest 未精确绑定冻结场景输入")
        counts = {kind: 0 for kind in GateKind}
        for result in self.gate_results:
            counts[result.gate] += 1
        if any(count != 1 for count in counts.values()):
            expected = VerificationVerdict.ERROR
        elif any(item.status is GateStatus.ERROR for item in self.gate_results):
            expected = VerificationVerdict.ERROR
        elif any(
            item.status in {GateStatus.BLOCKED, GateStatus.SKIPPED}
            for item in self.gate_results
        ):
            expected = VerificationVerdict.BLOCKED
        elif any(item.status is GateStatus.FAIL for item in self.gate_results):
            expected = VerificationVerdict.REJECTED
        elif any(
            item.status is GateStatus.NOT_APPLICABLE and item.gate is not GateKind.UI
            for item in self.gate_results
        ):
            expected = VerificationVerdict.ERROR
        elif all(
            item.status is GateStatus.PASS
            or (
                item.gate is GateKind.UI
                and item.status is GateStatus.NOT_APPLICABLE
            )
            for item in self.gate_results
        ):
            expected = VerificationVerdict.VERIFIED
        else:
            expected = VerificationVerdict.ERROR
        if self.verdict is not expected:
            raise ValueError(
                f"verdict={self.verdict.value} 与机器 gate={expected.value} 不一致"
            )
        if self.verdict is VerificationVerdict.VERIFIED:
            if self.replay_digest is None:
                raise ValueError("VERIFIED 必须绑定通过的 replay receipt digest")
            if self.replay_manifest is None:
                raise ValueError("VERIFIED 必须绑定 replay receipt evidence manifest")
            if (
                self.candidate_digest is None
                or self.candidate_digest_after != self.candidate_digest
            ):
                raise ValueError("VERIFIED 必须绑定前后一致的 candidate digest")
            if set(self.skill_digests) != set(self.skill_names):
                raise ValueError("VERIFIED 必须包含所有选中 Skill 的 digest")
            if len(self.skill_contracts) != len(self.skill_names):
                raise ValueError("VERIFIED 的冻结 Skill 契约数量不一致")
            contracts = {item.name: item for item in self.skill_contracts}
            if (
                set(contracts) != set(self.skill_names)
                or any(
                    contracts[name].digest != self.skill_digests[name]
                    for name in self.skill_names
                )
            ):
                raise ValueError("VERIFIED 必须携带与冻结摘要一致的 Skill 契约")
            evidence_ids = [item.evidence_id for item in self.command_evidence]
            if len(evidence_ids) != len(set(evidence_ids)):
                raise ValueError("VERIFIED 的 CommandEvidence id 不能跨 Gate 重复")
            if any(
                item.run_id != self.run_id
                or item.cycle != self.cycle
                or item.candidate_ref != self.candidate_ref
                or item.candidate_digest_before != self.candidate_digest
                or item.candidate_digest_after != self.candidate_digest
                for item in self.command_evidence
            ):
                raise ValueError("VERIFIED 包含未绑定当前 candidate 的命令证据")
            if any(item.policy_digest != self.policy_digest for item in self.command_evidence):
                raise ValueError("VERIFIED 包含未绑定当前 policy 的命令证据")
            if any(not item.passed for item in self.command_evidence):
                raise ValueError("VERIFIED 不能包含未通过的命令证据")
            command_gates = {
                GateKind.LINT,
                GateKind.UNIT,
                GateKind.INTEGRATION,
            }
            ui_result = next(
                item for item in self.gate_results if item.gate is GateKind.UI
            )
            if ui_result.status is GateStatus.PASS:
                command_gates.add(GateKind.UI)
            if any(item.gate not in command_gates for item in self.command_evidence):
                raise ValueError("VERIFIED 包含不属于命令 Gate 的 CommandEvidence")
            command_ids = {
                item.evidence_id
                for item in self.command_evidence
                if item.gate in command_gates
            }
            referenced_ids = {
                evidence_id
                for result in self.gate_results
                if result.gate in command_gates
                for evidence_id in result.evidence_ids
            }
            if not command_ids or command_ids != referenced_ids:
                raise ValueError("VERIFIED 的命令 Gate 与 CommandEvidence 不一致")
            if any(
                item is None
                for item in (
                    self.trace_evidence,
                    self.log_evidence,
                    self.behavior_evidence,
                )
            ):
                raise ValueError("VERIFIED 必须保留 Trace、日志和行为原始证据")
            assert self.trace_evidence is not None
            assert self.log_evidence is not None
            assert self.behavior_evidence is not None
            externals = (
                self.trace_evidence,
                self.log_evidence,
                self.behavior_evidence,
            )
            if any(
                item.run_id != self.run_id
                or item.cycle != self.cycle
                or item.candidate_ref != self.candidate_ref
                or item.candidate_digest != self.candidate_digest
                or item.policy_digest != self.policy_digest
                or item.skill_digests != self.skill_digests
                for item in externals
            ):
                raise ValueError("VERIFIED 包含未绑定当前运行的外部证据")
            if any(
                not item.collection_complete or item.collector_error is not None
                for item in externals
            ):
                raise ValueError("VERIFIED 不能包含不完整或采集失败的外部证据")
            for item in (self.log_evidence, self.behavior_evidence):
                if (
                    item.control_ref != self.control_ref
                    or item.control_digest != self.control_digest
                ):
                    raise ValueError("VERIFIED 的 control 证据与冻结基线不一致")
            gate_map = {item.gate: item for item in self.gate_results}
            expected_external_ids = {
                GateKind.TRACE: {
                    item.trace_id for item in self.trace_evidence.observations
                },
                GateKind.STAGING_LOG: {
                    item.observation_id for item in self.log_evidence.observations
                },
                GateKind.BEHAVIOR_COMPARE: {
                    item.observation_id
                    for item in self.behavior_evidence.observations
                },
            }
            if any(
                set(gate_map[kind].evidence_ids) != ids
                for kind, ids in expected_external_ids.items()
            ):
                raise ValueError("VERIFIED 的外部 Gate 与原始 evidence 不一致")
            self._recompute_verified_gates(gate_map, contracts)
        return self

    def _recompute_verified_gates(
        self,
        gate_map: dict[GateKind, GateResult],
        contracts: dict[str, FrozenVerificationSkill],
    ) -> None:
        """Ignore claimed PASS values and deterministically replay all evaluators."""

        # Lazy import avoids a module cycle: gates consumes these immutable models.
        from .gates import (
            blocked,
            evaluate_behavior_gate,
            evaluate_command_gate,
            evaluate_log_gate,
            evaluate_trace_gate,
            validate_assertion_contract_definitions,
            validate_assertion_contracts,
        )

        assert self.candidate_digest is not None
        assert self.trace_evidence is not None
        assert self.log_evidence is not None
        assert self.behavior_evidence is not None

        recomputed: dict[GateKind, GateResult] = {}

        definition_failures = validate_assertion_contract_definitions(
            self.assertion_contracts,
            skills=contracts.values(),
            policy=self.policy,
        )
        if definition_failures:
            raise ValueError(
                "VERIFIED 的 assertion contract 与受信 Skill/Policy 不一致: "
                + "; ".join(definition_failures)
            )
        assertion_failures = validate_assertion_contracts(
            self.assertion_contracts,
            self.command_evidence,
        )
        if assertion_failures:
            raise ValueError(
                "VERIFIED 的 assertion contract 与原始命令证据不一致: "
                + "; ".join(assertion_failures)
            )

        def command_result(
            gate: GateKind,
            expected: dict[tuple[str | None, str | None, str], str],
        ) -> GateResult:
            return evaluate_command_gate(
                gate,
                (item for item in self.command_evidence if item.gate is gate),
                expected_contracts=expected,
                run_id=self.run_id,
                cycle=self.cycle,
                policy_digest=self.policy_digest,
                expected_skill_digests=self.skill_digests,
                candidate_ref=self.candidate_ref,
                candidate_digest=self.candidate_digest,
            )

        lint_contracts: dict[tuple[str | None, str | None, str], str] = {}
        if self.policy.lint is not None:
            lint_contracts = {
                (None, None, check.id): command_contract_digest(
                    check, self.policy.lint.warning_patterns
                )
                for check in self.policy.lint.checks
            }
        recomputed[GateKind.LINT] = (
            command_result(GateKind.LINT, lint_contracts)
            if lint_contracts
            else blocked(GateKind.LINT, "lint 配置缺失")
        )

        unit_contracts: dict[tuple[str | None, str | None, str], str] = {}
        if self.policy.unit is not None:
            unit_contracts = {
                (None, None, check.id): command_contract_digest(check)
                for check in self.policy.unit.checks
            }
        recomputed[GateKind.UNIT] = (
            command_result(GateKind.UNIT, unit_contracts)
            if unit_contracts
            else blocked(GateKind.UNIT, "全量单测配置缺失")
        )

        integration_contracts: dict[
            tuple[str | None, str | None, str], str
        ] = {}
        integration_ids: set[str] = set()
        for skill_name in self.skill_names:
            skill = contracts[skill_name]
            for scenario in skill.spec.integration:
                scenario_id = f"{skill_name}:{scenario.id}"
                integration_ids.add(scenario_id)
                for check in scenario.steps:
                    integration_contracts[(skill_name, scenario_id, check.id)] = (
                        command_contract_digest(check)
                    )
        recomputed[GateKind.INTEGRATION] = (
            command_result(GateKind.INTEGRATION, integration_contracts)
            if integration_contracts
            else blocked(
                GateKind.INTEGRATION,
                "选中的 Verification Skill 没有聚焦集成场景",
            )
        )

        ui_id_list: list[str] = []
        ui_contracts: dict[tuple[str | None, str | None, str], str] = {}
        if self.policy.ui is None:
            recomputed[GateKind.UI] = blocked(
                GateKind.UI, "UI 适用性与验证配置缺失"
            )
        elif self.policy.ui.mode == "not_applicable":
            declared_ui = [
                skill.name for skill in contracts.values() if skill.spec.ui
            ]
            if declared_ui:
                recomputed[GateKind.UI] = blocked(
                    GateKind.UI,
                    "UI 被声明不适用，但选中 Skill 包含 UI 场景: "
                    + ", ".join(sorted(declared_ui)),
                )
            else:
                recomputed[GateKind.UI] = GateResult(
                    gate=GateKind.UI,
                    status=GateStatus.NOT_APPLICABLE,
                    summary=(
                        "配置明确声明当前服务无 UI: "
                        + (self.policy.ui.not_applicable_reason or "")
                    ),
                )
        else:
            for skill_name in self.skill_names:
                skill = contracts[skill_name]
                for scenario in skill.spec.ui:
                    scenario_id = f"{skill_name}:{scenario.id}"
                    ui_id_list.append(scenario_id)
                    for check in scenario.steps:
                        ui_contracts[(skill_name, scenario_id, check.id)] = (
                            command_contract_digest(check)
                        )
            for scenario in self.policy.ui.global_scenarios:
                scenario_id = f"global:{scenario.id}"
                ui_id_list.append(scenario_id)
                for check in scenario.steps:
                    ui_contracts[(None, scenario_id, check.id)] = (
                        command_contract_digest(check)
                    )
            if len(ui_id_list) != len(set(ui_id_list)):
                recomputed[GateKind.UI] = blocked(
                    GateKind.UI,
                    "UI scenario id 在 Skill 与全局配置之间发生冲突",
                )
                ui_id_list = []
            else:
                recomputed[GateKind.UI] = (
                    command_result(GateKind.UI, ui_contracts)
                    if ui_contracts
                    else blocked(GateKind.UI, "UI 为 required，但没有配置 UI 场景")
                )

        behavior_ids = (
            {item.scenario_id for item in self.policy.behavior.scenarios}
            if self.policy.behavior is not None
            else set()
        )
        context_ids = integration_ids | behavior_ids | set(ui_id_list)
        recomputed[GateKind.TRACE] = (
            evaluate_trace_gate(
                self.policy.trace,
                self.trace_evidence,
                run_id=self.run_id,
                cycle=self.cycle,
                candidate_ref=self.candidate_ref,
                candidate_digest=self.candidate_digest,
                policy_digest=self.policy_digest,
                expected_skill_digests=self.skill_digests,
                scenario_ids=context_ids,
                scenario_input_digests=self.scenario_input_digests,
            )
            if self.policy.trace is not None
            else blocked(GateKind.TRACE, "Trace 门禁配置缺失")
        )
        recomputed[GateKind.STAGING_LOG] = (
            evaluate_log_gate(
                self.policy.staging_log,
                self.log_evidence,
                run_id=self.run_id,
                cycle=self.cycle,
                control_ref=self.control_ref,
                control_digest=self.control_digest,
                candidate_ref=self.candidate_ref,
                candidate_digest=self.candidate_digest,
                policy_digest=self.policy_digest,
                expected_skill_digests=self.skill_digests,
                scenario_ids=context_ids,
                scenario_input_digests=self.scenario_input_digests,
            )
            if self.policy.staging_log is not None
            else blocked(GateKind.STAGING_LOG, "预发日志门禁配置缺失")
        )
        candidate_traces = {
            item.trace_id: item for item in self.trace_evidence.observations
        }
        recomputed[GateKind.BEHAVIOR_COMPARE] = (
            evaluate_behavior_gate(
                self.policy.behavior,
                self.behavior_evidence,
                run_id=self.run_id,
                cycle=self.cycle,
                control_ref=self.control_ref,
                control_digest=self.control_digest,
                candidate_ref=self.candidate_ref,
                candidate_digest=self.candidate_digest,
                policy_digest=self.policy_digest,
                expected_skill_digests=self.skill_digests,
                candidate_traces=candidate_traces,
                scenario_input_digests=self.scenario_input_digests,
                assertion_contracts=self.assertion_contracts,
            )
            if self.policy.behavior is not None
            else blocked(GateKind.BEHAVIOR_COMPARE, "行为对比门禁配置缺失")
        )

        mismatches = [
            kind.value for kind in GateKind if gate_map[kind] != recomputed[kind]
        ]
        if mismatches:
            raise ValueError(
                "VERIFIED 的 GateResult 与原始 evidence 重算结果不一致: "
                + ", ".join(mismatches)
            )


__all__ = [
    "ARTICLE_LINT_WARNING_PATTERNS",
    "ARTICLE_MAX_INPUT_TOKENS",
    "ARTICLE_MAX_VERIFICATION_ATTEMPTS",
    "BehaviorEvidence",
    "BehaviorGateSpec",
    "BehaviorObservation",
    "BehaviorScenarioSpec",
    "CommandEvidence",
    "CommandSpec",
    "FrozenVerificationSkill",
    "GateKind",
    "GateResult",
    "GateStatus",
    "LintGateSpec",
    "LogEvidence",
    "LogGateSpec",
    "LogObservation",
    "ReplayEvidenceManifest",
    "ReplayWindowBinding",
    "ResolvedVerificationSkill",
    "ScenarioAssertionContract",
    "ScenarioSpec",
    "ScenarioCollection",
    "TraceEvidence",
    "TraceGateSpec",
    "TraceObservation",
    "ToolCallObservation",
    "UIGateSpec",
    "UnitGateSpec",
    "Variant",
    "VerificationPolicy",
    "VerificationReport",
    "VerificationRunRequest",
    "VerificationSkillSpec",
    "VerificationVerdict",
    "command_contract_digest",
]
