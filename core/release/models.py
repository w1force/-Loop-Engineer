"""Strict configuration and receipts for the GitHub release gate."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class ApplicationSpec(ReleaseModel):
    app_id: str
    repository_path: str = Field(min_length=1)
    remote_name: str = "origin"
    remote_url: str = Field(min_length=1)
    github_repository: str = Field(pattern=_REPOSITORY.pattern)
    base_branch: str = "develop"
    reviewers: tuple[str, ...] = Field(min_length=1)
    github_api_url: str = "https://api.github.com"
    verification_evidence_root: str = Field(min_length=1)
    verification_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_skill_digests: dict[str, str] = Field(min_length=1)
    verification_signing_key_env: str = "LOOP_ENGINEER_VERIFICATION_SIGNING_KEY"
    release_receipt_root: str = Field(min_length=1)

    @field_validator("app_id")
    @classmethod
    def _valid_app_id(cls, value: str) -> str:
        if not _APP_ID.fullmatch(value):
            raise ValueError("app_id 非法")
        return value

    @field_validator("remote_name", "base_branch", "reviewers")
    @classmethod
    def _safe_git_names(cls, value):
        values = value if isinstance(value, tuple) else (value,)
        if any(
            not item
            or item.startswith("-")
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", item)
            for item in values
        ):
            raise ValueError("Git 名称非法")
        return value

    @model_validator(mode="after")
    def _unique_reviewers(self) -> Self:
        if len(self.reviewers) != len(set(self.reviewers)):
            raise ValueError("reviewers 不能重复")
        return self

    @field_validator("verification_skill_digests")
    @classmethod
    def _valid_skill_digests(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not _APP_ID.fullmatch(name)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for name, digest in value.items()
        ):
            raise ValueError("verification_skill_digests 非法")
        return value

    @field_validator("verification_signing_key_env")
    @classmethod
    def _valid_key_env(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
            raise ValueError("verification_signing_key_env 非法")
        return value


class ApplicationRegistry(ReleaseModel):
    schema_version: Literal["release-applications/v1"] = "release-applications/v1"
    applications: tuple[ApplicationSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_apps(self) -> Self:
        ids = [item.app_id for item in self.applications]
        if len(ids) != len(set(ids)):
            raise ValueError("app_id 不能重复")
        paths = [str(Path(item.repository_path).expanduser().resolve()) for item in self.applications]
        if len(paths) != len(set(paths)):
            raise ValueError("repository_path 不能被多个 App ID 复用")
        return self

    def get(self, app_id: str) -> ApplicationSpec:
        matches = [item for item in self.applications if item.app_id == app_id]
        if len(matches) != 1:
            raise KeyError(f"未知 App ID: {app_id}")
        return matches[0]

    @classmethod
    def load(cls, path: str | Path) -> "ApplicationRegistry":
        source = Path(path).expanduser().resolve()

        def no_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"重复 JSON key: {key}")
                result[key] = value
            return result

        return cls.model_validate(
            json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
        )


class ReleaseRequest(ReleaseModel):
    app_id: str
    repository_path: str = Field(min_length=1)
    verification_run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    verification_cycle: int = Field(ge=1, le=3)
    problem_slug: str
    branch_date: date = Field(default_factory=date.today)
    sequence: int = Field(default=1, ge=1, le=999)
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1)
    commit_message: str = Field(min_length=1, max_length=256)
    changed_files: tuple[str, ...] = Field(min_length=1)
    draft: bool = False

    @field_validator("app_id")
    @classmethod
    def _valid_app_id(cls, value: str) -> str:
        if not _APP_ID.fullmatch(value):
            raise ValueError("app_id 非法")
        return value

    @field_validator("problem_slug")
    @classmethod
    def _valid_slug(cls, value: str) -> str:
        if not _SLUG.fullmatch(value):
            raise ValueError("problem_slug 必须是小写 kebab-case")
        return value

    @field_validator("changed_files")
    @classmethod
    def _safe_changed_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("changed_files 不能重复")
        for item in value:
            path = Path(item)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not item.strip()
                or "\x00" in item
                or path.parts[0] == ".git"
            ):
                raise ValueError("changed_files 必须是仓库内相对路径")
        return value

    def branch_name(self, today: date | None = None) -> str:
        day = (today or self.branch_date).strftime("%Y%m%d")
        return f"fix/{self.problem_slug}_{day}_{self.sequence}"


class PullRequestReceipt(ReleaseModel):
    schema_version: Literal["github-pr-receipt/v1"] = "github-pr-receipt/v1"
    app_id: str
    repository: str
    branch: str
    base_branch: str
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    pull_request_number: int = Field(gt=0)
    pull_request_url: str = Field(min_length=1)
    reviewers: tuple[str, ...] = Field(min_length=1)
    verification_run_id: str = Field(min_length=1)
    verification_cycle: int = Field(ge=1, le=3)
    verification_report_path: str = Field(min_length=1)


__all__ = [
    "ApplicationRegistry",
    "ApplicationSpec",
    "PullRequestReceipt",
    "ReleaseRequest",
]
