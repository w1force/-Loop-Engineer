from __future__ import annotations

from datetime import date
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace

import pytest

import core.release.github as github_release
from core.release.github import (
    ReleaseError,
    ReleaseManager,
    _Git,
    _git_tree_digest,
    _normalize_remote,
)
from core.release.models import ApplicationRegistry, ApplicationSpec, ReleaseRequest
from core.verification import VerificationVerdict, workspace_digest


def _git(repository: Path, *argv: str) -> str:
    result = subprocess.run(
        ("git", *argv),
        cwd=repository,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _request(repository: Path) -> ReleaseRequest:
    return ReleaseRequest(
        app_id="ccb",
        repository_path=str(repository),
        verification_run_id="run-1",
        verification_cycle=1,
        problem_slug="fix-timeout",
        sequence=2,
        title="fix: timeout",
        body="Verified repair",
        commit_message="fix: timeout",
        changed_files=("service.txt",),
    )


def _app(repository: Path, root: Path) -> ApplicationSpec:
    return ApplicationSpec(
        app_id="ccb",
        repository_path=str(repository),
        remote_url="https://github.com/acme/service.git",
        github_repository="acme/service",
        base_branch="main",
        reviewers=("reviewer",),
        verification_evidence_root=str(root / "evidence"),
        verification_policy_digest="a" * 64,
        verification_skill_digests={"checkout": "b" * 64},
        release_receipt_root=str(root / "receipts"),
    )


def test_article_branch_name_format() -> None:
    request = _request(Path("/tmp/repo"))
    assert request.branch_name(date(2026, 8, 24)) == "fix/fix-timeout_20260824_2"


def test_release_request_rejects_git_metadata_path(tmp_path: Path) -> None:
    payload = _request(tmp_path).model_dump()
    payload["changed_files"] = [".git/config"]
    with pytest.raises(ValueError, match="changed_files"):
        ReleaseRequest.model_validate(payload)


def test_remote_normalization_accepts_https_and_ssh() -> None:
    assert _normalize_remote("https://github.com/acme/service.git") == (
        "github.com",
        "acme/service",
    )
    assert _normalize_remote("git@github.com:acme/service.git") == (
        "github.com",
        "acme/service",
    )


def test_step_zero_requires_app_path_and_remote_triple_match(tmp_path: Path) -> None:
    repository = tmp_path / "service"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "remote", "add", "origin", "https://github.com/acme/service.git")
    app = _app(repository, tmp_path)
    manager = ReleaseManager(ApplicationRegistry(applications=(app,)))
    assert manager._validate_application(_request(repository)) == app

    _git(
        repository,
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        "https://github.com/acme/other.git",
    )
    with pytest.raises(ReleaseError, match="push remote"):
        manager._validate_application(_request(repository))
    _git(repository, "config", "--unset-all", "remote.origin.pushurl")

    bad = app.model_copy(update={"remote_url": "https://github.com/acme/other.git"})
    with pytest.raises(ReleaseError, match="Git remote"):
        ReleaseManager(ApplicationRegistry(applications=(bad,)))._validate_application(
            _request(repository)
        )


def test_registry_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    path.write_text(
        '{"schema_version":"release-applications/v1",'
        '"schema_version":"release-applications/v1","applications":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复 JSON key"):
        ApplicationRegistry.load(path)


def test_release_rejects_executable_local_git_config(tmp_path: Path) -> None:
    repository = tmp_path / "service"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "filter.exfil.clean", "malicious-command")

    with pytest.raises(ReleaseError, match="本地 Git 配置"):
        _Git(repository).assert_safe_local_config()


def test_release_git_environment_does_not_inherit_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("LOOP_ENGINEER_VERIFICATION_SIGNING_KEY", "signing-secret")

    environment = _Git._environment()

    assert "GITHUB_TOKEN" not in environment
    assert "LOOP_ENGINEER_VERIFICATION_SIGNING_KEY" not in environment


def test_missing_credentials_fail_before_git_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "service"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "remote", "add", "origin", "https://github.com/acme/service.git")
    manager = ReleaseManager(ApplicationRegistry(applications=(_app(repository, tmp_path),)))
    monkeypatch.delenv("LOOP_ENGINEER_VERIFICATION_SIGNING_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(ReleaseError, match="签名密钥"):
        manager.release(_request(repository))
    assert not _git(repository, "branch", "--list", "fix/fix-timeout_20260824_2")


def test_git_tree_digest_matches_committed_tree(tmp_path: Path) -> None:
    repository = tmp_path / "service"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "loop@example.test")
    _git(repository, "config", "user.name", "Loop Engineer")
    (repository / "service.txt").write_text("v1\n", encoding="utf-8")
    _git(repository, "add", "service.txt")
    _git(repository, "commit", "-m", "initial")

    assert _git_tree_digest(_Git(repository), "HEAD", ()) == workspace_digest(
        repository, (".git/**",)
    )


def test_release_is_idempotent_after_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(("git", "init", "--bare", str(remote)), check=True)
    repository = tmp_path / "service"
    subprocess.run(("git", "clone", str(remote), str(repository)), check=True)
    _git(repository, "config", "user.email", "loop@example.test")
    _git(repository, "config", "user.name", "Loop Engineer")
    _git(repository, "switch", "-c", "main")
    (repository / "service.txt").write_text("v1\n", encoding="utf-8")
    _git(repository, "add", "service.txt")
    _git(repository, "commit", "-m", "initial")
    _git(repository, "push", "-u", "origin", "main")
    (repository / "service.txt").write_text("v2\n", encoding="utf-8")
    hook_marker = tmp_path / "hook-ran"
    hook = (
        "#!/bin/sh\n"
        f"printf '%s\\n' hook-ran >> {shlex.quote(str(hook_marker))}\n"
    )
    for name in ("pre-commit", "pre-push"):
        hook_path = repository / ".git" / "hooks" / name
        hook_path.write_text(hook, encoding="utf-8")
        hook_path.chmod(0o700)

    app = _app(repository, tmp_path).model_copy(
        update={"remote_url": "https://github.com/acme/service.git"}
    )
    manager = ReleaseManager(ApplicationRegistry(applications=(app,)))
    manager._validate_application = lambda request: app  # type: ignore[method-assign]
    ignore = (".git/**", ".loop-engineer/**")
    report = SimpleNamespace(
        verdict=VerificationVerdict.VERIFIED,
        policy=SimpleNamespace(workspace_ignore=ignore),
        candidate_digest=workspace_digest(repository, ignore),
        run_id="run-1",
        cycle=1,
    )
    monkeypatch.setenv("LOOP_ENGINEER_VERIFICATION_SIGNING_KEY", "k" * 32)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        github_release.AttestedJsonEvidenceStore,
        "load_attested",
        lambda *args, **kwargs: (report, str(tmp_path / "evidence" / "report.json")),
    )

    class FakePublisher:
        def __init__(self, token: str, api_url: str):
            assert token == "token"

        def create_or_get(self, **kwargs):
            assert kwargs["branch"] == "fix/fix-timeout_20260824_2"
            return 17, "https://github.com/acme/service/pull/17"

    monkeypatch.setattr(github_release, "GitHubPullRequestPublisher", FakePublisher)
    request = _request(repository)
    first = manager.release(request, today=date(2026, 8, 24))
    second = manager.release(request, today=date(2026, 8, 24))

    assert first == second
    assert first.pull_request_number == 17
    assert _git(repository, "rev-list", "--count", "main..HEAD") == "1"
    assert _git(repository, "ls-remote", "--heads", "origin", first.branch)
    assert not hook_marker.exists()
