from __future__ import annotations

from datetime import date
from pathlib import Path
import shlex
import subprocess
import time
from types import SimpleNamespace

import pytest

import core.release.github as github_release
from core.observability import (
    ExecutionWindow,
    LocalObservabilityStore,
    OtlpFlushBarrier,
    normalized_input_digest,
)
from core.release.barrier import verify_live_replay_barriers
from core.release import CoordinatorReleaseAction
from core.release.github import (
    GitHubPullRequestPublisher,
    ReleaseError,
    ReleaseManager,
    _Git,
    _git_tree_digest,
    _normalize_remote,
)
from core.release.models import ApplicationRegistry, ApplicationSpec, ReleaseRequest
from core.verification import (
    ReplayEvidenceManifest,
    ReplayWindowBinding,
    Variant,
    VerifiedReleaseRequest,
    VerificationVerdict,
    workspace_digest,
)


REPLAY_MANIFEST = ReplayEvidenceManifest(
    windows=tuple(
        ReplayWindowBinding(
            scenario_id="checkout:case",
            variant=variant,
            input_digest="e" * 64,
            collection_id=f"checkout-{variant.value}",
            otlp_barrier_digest="1" * 64,
            oracle_digest="2" * 64,
            result_sha256="3" * 64,
        )
        for variant in (Variant.CONTROL, Variant.CANDIDATE)
    )
)


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
        verification_incident_id="incident-1",
        verification_incident_digest="c" * 64,
        verification_plan_digest="d" * 64,
        verification_replay_digest="f" * 64,
        verification_replay_manifest=REPLAY_MANIFEST,
        verification_scenario_input_digests={"checkout:case": "e" * 64},
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
        verification_observability_database=str(root / "observability.sqlite3"),
        verification_policy_digest="a" * 64,
        verification_skill_digests={"checkout": "b" * 64},
        release_receipt_root=str(root / "receipts"),
    )


def _sealed_observability_database(path: Path) -> ReplayEvidenceManifest:
    store = LocalObservabilityStore(path)
    input_digest = normalized_input_digest({})
    bindings = []
    for variant, trace_id in (("control", "a" * 32), ("candidate", "b" * 32)):
        collection_id = f"checkout-{variant}"
        resource = {
            "attributes": [
                {"key": "verification.run_id", "value": {"stringValue": "run-1"}},
                {"key": "verification.cycle", "value": {"intValue": "1"}},
                {"key": "verification.scenario_id", "value": {"stringValue": "checkout:case"}},
                {"key": "verification.variant", "value": {"stringValue": variant}},
                {"key": "verification.input_digest", "value": {"stringValue": input_digest}},
                {"key": "verification.collection_id", "value": {"stringValue": collection_id}},
            ]
        }
        store.ingest_otlp_traces({"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": [{"traceId": trace_id, "spanId": "1" * 16, "name": "run", "startTimeUnixNano": "10", "endTimeUnixNano": "20"}]}]}]})
        store.ingest_otlp_logs({"resourceLogs": [{"resource": resource, "scopeLogs": [{"logRecords": [{"timeUnixNano": "15", "severityText": "INFO", "body": {"stringValue": "ok"}}]}]}]})
        window = ExecutionWindow(
            run_id="run-1", cycle=1, scenario_id="checkout:case", variant=variant,
            input_digest=input_digest, input_payload={}, collection_id=collection_id,
            control_ref="control", control_digest="b" * 64,
            candidate_ref="candidate", candidate_digest="a" * 64,
            policy_digest="c" * 64, skill_digests={"checkout": "d" * 64},
            started_at_ns=1, ended_at_ns=30, collection_complete=True,
            oracle_digest="2" * 64, result_sha256="3" * 64,
        )
        store.record_execution(window)
        store.record_otlp_flush_barrier(OtlpFlushBarrier(
            flush_id=f"flush-{variant}", collection_id=collection_id,
            run_id="run-1", cycle=1, scenario_id="checkout:case", variant=variant,
            input_digest=input_digest, signals=("traces", "logs"),
            flush_started_at_ns=30, flush_completed_at_ns=30,
            deadline_ns=time.time_ns() + 10_000_000_000,
        ))
        row = next(row for row in store.execution_windows("run-1", 1) if row["variant"] == variant)
        bindings.append(ReplayWindowBinding(
            scenario_id="checkout:case", variant=Variant(variant), input_digest=input_digest,
            collection_id=collection_id, otlp_barrier_digest=store.otlp_barrier_digest(row),
            oracle_digest="2" * 64, result_sha256="3" * 64,
        ))
    return ReplayEvidenceManifest(windows=tuple(bindings))


def test_article_branch_name_format() -> None:
    request = _request(Path("/tmp/repo"))
    assert request.branch_name(date(2026, 8, 24)) == "fix/fix-timeout_20260824_2"


def test_release_request_rejects_git_metadata_path(tmp_path: Path) -> None:
    payload = _request(tmp_path).model_dump()
    payload["changed_files"] = [".git/config"]
    with pytest.raises(ValueError, match="changed_files"):
        ReleaseRequest.model_validate(payload)


def test_release_request_requires_plan_and_incident_binding(tmp_path: Path) -> None:
    payload = _request(tmp_path).model_dump()
    for field in (
        "verification_incident_id",
        "verification_incident_digest",
        "verification_plan_digest",
        "verification_replay_digest",
        "verification_scenario_input_digests",
    ):
        incomplete = dict(payload)
        incomplete.pop(field)
        with pytest.raises(ValueError):
            ReleaseRequest.model_validate(incomplete)


@pytest.mark.asyncio
async def test_coordinator_release_rejects_plan_binding_mismatch(tmp_path: Path) -> None:
    request = _request(tmp_path)
    manager = ReleaseManager(ApplicationRegistry(applications=(_app(tmp_path, tmp_path),)))
    action = CoordinatorReleaseAction(manager, request)
    verified = VerifiedReleaseRequest(
        run_id=request.verification_run_id,
        cycle=request.verification_cycle,
        incident_id=request.verification_incident_id,
        incident_digest=request.verification_incident_digest,
        plan_digest="0" * 64,
        replay_digest=request.verification_replay_digest,
        replay_manifest=request.verification_replay_manifest,
        scenario_input_digests=request.verification_scenario_input_digests,
        candidate_ref="candidate-ref",
        candidate_digest="f" * 64,
        evidence_location=str(tmp_path / "evidence"),
    )

    with pytest.raises(ReleaseError, match="Plan/Incident"):
        await action.release_verified(verified)


def test_remote_normalization_accepts_only_https_github_transport() -> None:
    assert _normalize_remote("https://github.com/acme/service.git") == (
        "github.com",
        "acme/service",
    )
    for unsafe in (
        "http://github.com/acme/service.git",
        "git@github.com:acme/service.git",
        "ssh://git@github.com/acme/service.git",
    ):
        with pytest.raises(ReleaseError, match="不支持"):
            _normalize_remote(unsafe)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("remote_url", "http://github.com/acme/service.git"),
        ("remote_url", "https://github.com/acme/other.git"),
        ("github_api_url", "http://api.github.com"),
        ("github_api_url", "http://127.0.0.1:9999"),
        ("github_api_url", "https://example.com"),
    ),
)
def test_application_rejects_untrusted_github_endpoints(
    tmp_path: Path, field: str, value: str
) -> None:
    payload = _app(tmp_path, tmp_path).model_dump()
    payload[field] = value

    with pytest.raises(ValueError):
        ApplicationSpec.model_validate(payload)


def test_publisher_rejects_untrusted_api_before_constructing_client() -> None:
    with pytest.raises(ReleaseError, match="GitHub API URL"):
        GitHubPullRequestPublisher("token", api_url="http://127.0.0.1:9999")


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

    bad_payload = app.model_dump()
    bad_payload.update(
        {
            "remote_url": "https://github.com/acme/other.git",
            "github_repository": "acme/other",
        }
    )
    bad = ApplicationSpec.model_validate(bad_payload)
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
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"


def test_live_release_barrier_rejects_late_otlp(tmp_path: Path) -> None:
    database = tmp_path / "observability.sqlite3"
    manifest = _sealed_observability_database(database)
    verify_live_replay_barriers(
        database, run_id="run-1", cycle=1, manifest=manifest
    )
    store = LocalObservabilityStore(database)
    with store._connect() as connection:
        connection.execute(
            "UPDATE otlp_flush_barriers SET late_arrival_count = 1 "
            "WHERE collection_id = ?",
            ("checkout-candidate",),
        )
    with pytest.raises(ValueError, match="已失效"):
        verify_live_replay_barriers(
            database, run_id="run-1", cycle=1, manifest=manifest
        )


def test_release_fails_closed_when_live_barrier_recheck_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "service"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "remote", "add", "origin", "https://github.com/acme/service.git")
    manager = ReleaseManager(ApplicationRegistry(applications=(_app(repository, tmp_path),)))
    report = SimpleNamespace(
        verdict=VerificationVerdict.VERIFIED,
        policy=SimpleNamespace(workspace_ignore=(".git/**",)),
        candidate_digest=workspace_digest(repository, (".git/**",)),
    )
    monkeypatch.setenv("LOOP_ENGINEER_VERIFICATION_SIGNING_KEY", "k" * 32)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        github_release.AttestedJsonEvidenceStore,
        "load_attested",
        lambda *args, **kwargs: (report, str(tmp_path / "report.json")),
    )
    monkeypatch.setattr(
        github_release,
        "verify_live_replay_barriers",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("late arrival")),
    )

    with pytest.raises(ReleaseError, match="实时 OTLP barrier"):
        manager.release(_request(repository))
    assert not _git(repository, "branch", "--list", "fix/fix-timeout_20260824_2")


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


def test_release_rejects_changed_file_excluded_from_verified_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "service"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "remote", "add", "origin", "https://github.com/acme/service.git")
    ignored = repository / ".loop-engineer" / "hidden.txt"
    ignored.parent.mkdir()
    ignored.write_text("unverified\n", encoding="utf-8")
    manager = ReleaseManager(
        ApplicationRegistry(applications=(_app(repository, tmp_path),))
    )
    report = SimpleNamespace(
        verdict=VerificationVerdict.VERIFIED,
        policy=SimpleNamespace(workspace_ignore=(".git/**", ".loop-engineer/**")),
        candidate_digest=workspace_digest(
            repository, (".git/**", ".loop-engineer/**")
        ),
    )
    monkeypatch.setenv("LOOP_ENGINEER_VERIFICATION_SIGNING_KEY", "k" * 32)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        github_release.AttestedJsonEvidenceStore,
        "load_attested",
        lambda *args, **kwargs: (report, str(tmp_path / "report.json")),
    )
    request = _request(repository).model_copy(
        update={"changed_files": (".loop-engineer/hidden.txt",)}
    )

    with pytest.raises(ReleaseError, match="workspace_ignore"):
        manager.release(request)

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
    monkeypatch.setattr(
        github_release, "verify_live_replay_barriers", lambda *args, **kwargs: None
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
