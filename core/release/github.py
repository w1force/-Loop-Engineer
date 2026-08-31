"""Machine-enforced VERIFIED -> GitHub pull request transition."""

from __future__ import annotations

from datetime import date
import fnmatch
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlparse

import httpx

from core.verification import (
    AttestedJsonEvidenceStore,
    VerificationVerdict,
    canonical_json_digest,
    workspace_digest,
)

from .barrier import verify_live_replay_barriers

from .models import (
    ApplicationRegistry,
    ApplicationSpec,
    PullRequestReceipt,
    ReleaseRequest,
)


class ReleaseError(RuntimeError):
    pass


def _normalize_remote(value: str) -> tuple[str, str]:
    raw = value.strip()
    parsed = urlparse(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ReleaseError(f"Git remote URL 端口非法: {value}") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseError(f"不支持的 Git remote URL: {value}")
    return parsed.hostname.lower(), parsed.path.removesuffix(".git").strip("/")


class _Git:
    def __init__(self, repository: Path):
        self.repository = repository

    @staticmethod
    def _environment(extra: dict[str, str] | None = None) -> dict[str, str]:
        # Git subprocesses must not inherit the verification signing key,
        # GitHub API token, or arbitrary GIT_* execution hooks from the Agent.
        inherited = (
            "HOME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "LOGNAME",
            "PATH",
            "SSH_AUTH_SOCK",
            "TMPDIR",
            "TZ",
            "USER",
        )
        env = {key: os.environ[key] for key in inherited if key in os.environ}
        env.update(
            {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        if extra:
            env.update(extra)
        return env

    @staticmethod
    def _argv(*argv: str) -> tuple[str, ...]:
        return (
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            *argv,
        )

    def run(self, *argv: str, check: bool = True) -> str:
        result = subprocess.run(
            self._argv(*argv),
            cwd=self.repository,
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ReleaseError(f"git {' '.join(argv)} 失败: {detail}")
        return result.stdout.strip()

    def run_bytes(
        self,
        *argv: str,
        check: bool = True,
        input_bytes: bytes | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> bytes:
        result = subprocess.run(
            self._argv(*argv),
            cwd=self.repository,
            env=self._environment(extra_env),
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).decode(
                "utf-8", errors="replace"
            ).strip()
            raise ReleaseError(f"git {' '.join(argv)} 失败: {detail}")
        return result.stdout

    def succeeds(self, *argv: str) -> bool:
        result = subprocess.run(
            self._argv(*argv),
            cwd=self.repository,
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
        return result.returncode == 0

    def assert_safe_local_config(self) -> None:
        unsafe_pattern = (
            r"^(core\.(hooksPath|fsmonitor|sshCommand)|"
            r"credential\..*|filter\..*\.(clean|smudge|process|required)|"
            r"include(If\..*)?\.path|remote\..*\.(pushurl|receivepack|uploadpack)|"
            r"url\..*\.insteadOf|http\..*)$"
        )
        configured = self.run(
            "config",
            "--local",
            "--name-only",
            "--get-regexp",
            unsafe_pattern,
            check=False,
        )
        if configured:
            raise ReleaseError(
                "仓库本地 Git 配置包含可执行或改写发布链路的设置: "
                + ", ".join(sorted(set(configured.splitlines())))
            )

    def stage_raw(self, paths: tuple[str, ...]) -> None:
        """Stage exact workspace bytes without invoking Git clean filters."""

        object_format = self.run("rev-parse", "--show-object-format")
        zero_oid = "0" * (64 if object_format == "sha256" else 40)
        records: list[bytes] = []
        for relative in sorted(paths):
            source = self.repository / relative
            try:
                source.parent.resolve().relative_to(self.repository.resolve())
            except ValueError as exc:
                raise ReleaseError(f"待提交路径通过父目录逃逸仓库: {relative}") from exc
            if not source.exists() and not source.is_symlink():
                records.append(
                    f"0 {zero_oid}\t".encode("ascii")
                    + os.fsencode(relative)
                    + b"\0"
                )
                continue
            before = source.lstat()
            if stat.S_ISLNK(before.st_mode):
                content = os.fsencode(os.readlink(source))
                mode = "120000"
            elif stat.S_ISREG(before.st_mode):
                content = source.read_bytes()
                mode = "100755" if before.st_mode & stat.S_IXUSR else "100644"
            else:
                raise ReleaseError(f"待提交路径不是普通文件或符号链接: {relative}")
            after = source.lstat()
            if (
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
                before.st_ino,
            ) != (
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ):
                raise ReleaseError(f"待提交文件读取期间发生变化: {relative}")
            object_id = self.run_bytes(
                "hash-object", "-w", "--stdin", input_bytes=content
            ).decode("ascii").strip()
            if not re.fullmatch(r"[0-9a-f]+", object_id):
                raise ReleaseError(f"无法创建 Git blob: {relative}")
            records.append(
                f"{mode} {object_id}\t".encode("ascii")
                + os.fsencode(relative)
                + b"\0"
            )
        self.run_bytes(
            "update-index",
            "-z",
            "--index-info",
            input_bytes=b"".join(records),
        )

    def push(self, remote: str, branch: str, token: str) -> None:
        remote_url = self.run("remote", "get-url", "--push", remote)
        if urlparse(remote_url).scheme == "https":
            if not token:
                raise ReleaseError("HTTPS Git push 缺少 GitHub token")
            with tempfile.TemporaryDirectory(prefix="loop-git-askpass-") as directory:
                askpass = Path(directory) / "askpass.sh"
                askpass.write_text(
                    "#!/bin/sh\n"
                    "case \"$1\" in\n"
                    "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
                    "  *) printf '%s\\n' \"$LOOP_ENGINEER_GIT_PUSH_TOKEN\" ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                askpass.chmod(0o700)
                self.run_bytes(
                    "-c",
                    "credential.helper=",
                    "push",
                    "--set-upstream",
                    remote,
                    branch,
                    extra_env={
                        "GIT_ASKPASS": str(askpass),
                        "GIT_ASKPASS_REQUIRE": "force",
                        "LOOP_ENGINEER_GIT_PUSH_TOKEN": token,
                    },
                )
            return
        self.run("push", "--set-upstream", remote, branch)

    def changed_files(self) -> set[str]:
        values: set[str] = set()
        for argv in (
            ("diff", "--name-only", "-z"),
            ("diff", "--cached", "--name-only", "-z"),
            ("ls-files", "--others", "--exclude-standard", "-z"),
        ):
            values.update(item for item in self.run(*argv).split("\0") if item)
        return values

    def changed_files_between(self, base: str, head: str = "HEAD") -> set[str]:
        return {
            item
            for item in self.run(
                "diff", "--name-only", "-z", f"{base}..{head}"
            ).split("\0")
            if item
        }


def _ignored(relative: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        prefix = pattern[:-3].rstrip("/") if pattern.endswith("/**") else None
        if prefix and (relative == prefix or relative.startswith(prefix + "/")):
            return True
        if fnmatch.fnmatchcase(relative, pattern) or Path(relative).match(pattern):
            return True
    return False


def _git_tree_digest(
    git: _Git,
    commit: str,
    ignore: tuple[str, ...],
) -> str:
    """Hash committed blobs using the same manifest contract as workspace_digest."""

    raw = git.run_bytes("ls-tree", "-rz", "--full-tree", "-r", commit)
    manifest: dict[str, dict[str, str | int]] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ")
            relative = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReleaseError("无法解析 Git tree entry") from exc
        if _ignored(relative, ignore):
            continue
        if object_type != "blob":
            raise ReleaseError(
                f"candidate 包含不支持的 Git 对象: {relative} ({object_type})"
            )
        content = git.run_bytes("cat-file", "blob", object_id)
        if mode == "120000":
            kind = "link"
            permission = 0o777
        elif mode in {"100644", "100755"}:
            kind = "file"
            permission = 0o755 if mode == "100755" else 0o644
        else:
            raise ReleaseError(f"candidate 包含不支持的文件模式: {relative} ({mode})")
        manifest[relative] = {
            "type": kind,
            "mode": permission,
            "size": len(content),
            "sha256": sha256(content).hexdigest(),
        }
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class GitHubPullRequestPublisher:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        if not token:
            raise ReleaseError("缺少 GitHub token")
        parsed = urlparse(api_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ReleaseError("GitHub API URL 端口非法") from exc
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "api.github.com"
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ReleaseError("GitHub API URL 必须是 https://api.github.com")
        self.api_url = "https://api.github.com"
        self.client = httpx.Client(
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
            trust_env=False,
        )

    def _request(self, method: str, path: str, **kwargs) -> Any:
        response = self.client.request(method, self.api_url + path, **kwargs)
        if response.status_code >= 400:
            raise ReleaseError(
                f"GitHub API {method} {path} 失败: "
                f"HTTP {response.status_code} {response.text[:500]}"
            )
        return response.json()

    def create_or_get(
        self,
        *,
        repository: str,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        reviewers: tuple[str, ...],
        draft: bool,
    ) -> tuple[int, str]:
        owner = repository.split("/", 1)[0]
        existing = self._request(
            "GET",
            f"/repos/{repository}/pulls",
            params={"state": "open", "head": f"{owner}:{branch}", "base": base_branch},
        )
        if existing:
            pull_request = existing[0]
        else:
            pull_request = self._request(
                "POST",
                f"/repos/{repository}/pulls",
                json={
                    "title": title,
                    "head": branch,
                    "base": base_branch,
                    "body": body,
                    "draft": draft,
                },
            )
        number = int(pull_request["number"])
        url = str(pull_request["html_url"])
        self._request(
            "POST",
            f"/repos/{repository}/pulls/{number}/requested_reviewers",
            json={"reviewers": list(reviewers)},
        )
        return number, url


class ReleaseManager:
    """Refuse every Git/GitHub mutation until the verified report is rechecked."""

    def __init__(
        self,
        registry: ApplicationRegistry,
        *,
        github_token_env: str = "GITHUB_TOKEN",
    ):
        self.registry = registry
        self.github_token_env = github_token_env

    def release(
        self,
        request: ReleaseRequest,
        *,
        today: date | None = None,
    ) -> PullRequestReceipt:
        app = self._validate_application(request)
        repository = Path(request.repository_path).expanduser().resolve()
        git = _Git(repository)
        git.assert_safe_local_config()
        signing_key = os.environ.get(app.verification_signing_key_env, "").encode(
            "utf-8"
        )
        if not signing_key:
            raise ReleaseError(
                f"缺少 Verification 签名密钥环境变量: {app.verification_signing_key_env}"
            )
        github_token = os.environ.get(self.github_token_env, "")
        publisher = GitHubPullRequestPublisher(
            github_token,
            api_url=app.github_api_url,
        )
        try:
            report, report_path = AttestedJsonEvidenceStore.load_attested(
                app.verification_evidence_root,
                run_id=request.verification_run_id,
                cycle=request.verification_cycle,
                signing_key=signing_key,
                app_id=app.app_id,
                repository=app.github_repository,
                incident_id=request.verification_incident_id,
                incident_digest=request.verification_incident_digest,
                plan_digest=request.verification_plan_digest,
                replay_digest=request.verification_replay_digest,
                replay_manifest=request.verification_replay_manifest,
                scenario_input_digests=request.verification_scenario_input_digests,
                policy_digest=app.verification_policy_digest,
                skill_digests=app.verification_skill_digests,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise ReleaseError(f"受信 Verification evidence 校验失败: {exc}") from exc
        if report.verdict is not VerificationVerdict.VERIFIED:
            raise ReleaseError(f"Verification 未放行: {report.verdict.value}")
        ignored_changed_files = sorted(
            path
            for path in request.changed_files
            if _ignored(path, report.policy.workspace_ignore)
        )
        if ignored_changed_files:
            raise ReleaseError(
                "发布文件命中 verification workspace_ignore，内容未被验证: "
                + ", ".join(ignored_changed_files)
            )
        current_digest = workspace_digest(repository, report.policy.workspace_ignore)
        if current_digest != report.candidate_digest:
            raise ReleaseError("当前服务源码与 VERIFIED candidate digest 不一致")
        try:
            verify_live_replay_barriers(
                app.verification_observability_database,
                run_id=request.verification_run_id,
                cycle=request.verification_cycle,
                manifest=request.verification_replay_manifest,
            )
        except ValueError as exc:
            raise ReleaseError(f"实时 OTLP barrier 复核失败: {exc}") from exc

        expected_files = set(request.changed_files)
        git.run("diff", "--check")
        branch = request.branch_name(today)
        protected = {"main", "master", "develop", app.base_branch}
        if branch in protected or not branch.startswith("fix/"):
            raise ReleaseError("修复分支名称不符合 fix/<问题>_<日期>_<序号>")
        remote_base = git.run(
            "ls-remote", "--heads", app.remote_name, f"refs/heads/{app.base_branch}"
        )
        if not remote_base:
            raise ReleaseError(f"目标分支不存在: {app.base_branch}")
        remote_base_sha = remote_base.split()[0]
        if not git.succeeds("cat-file", "-e", f"{remote_base_sha}^{{commit}}"):
            raise ReleaseError("本地缺少远端目标分支对象，请先 fetch 后重新验证")

        local_branch_exists = bool(git.run("branch", "--list", branch))
        current_branch = git.run("symbolic-ref", "--short", "HEAD", check=False)
        if local_branch_exists:
            if current_branch != branch:
                raise ReleaseError(
                    f"检测到部分发布状态；请切换到 {branch} 后重试，拒绝自动切换"
                )
            if git.changed_files():
                raise ReleaseError("恢复发布时修复分支工作区必须保持干净")
            commit_sha = git.run("rev-parse", "HEAD")
            if not git.succeeds(
                "merge-base", "--is-ancestor", remote_base_sha, commit_sha
            ):
                raise ReleaseError("修复分支不是从当前远端目标分支派生")
            if git.run("rev-list", "--count", f"{remote_base_sha}..{commit_sha}") != "1":
                raise ReleaseError("修复分支必须只包含一个基于目标分支的提交")
            committed_files = git.changed_files_between(remote_base_sha, commit_sha)
            if committed_files != expected_files:
                raise ReleaseError(
                    "已提交文件与授权清单不一致: "
                    f"expected={sorted(expected_files)}, actual={sorted(committed_files)}"
                )
            if git.run("show", "-s", "--format=%s", commit_sha) != request.commit_message:
                raise ReleaseError("已有修复提交的 commit message 与请求不一致")
        else:
            actual_files = git.changed_files()
            if actual_files != expected_files:
                raise ReleaseError(
                    f"待提交文件与授权清单不一致: expected={sorted(expected_files)}, "
                    f"actual={sorted(actual_files)}"
                )
            if git.run("rev-parse", "HEAD") != remote_base_sha:
                raise ReleaseError("当前 HEAD 必须精确等于远端目标分支后才能创建修复分支")
            remote_branch = git.run(
                "ls-remote", "--heads", app.remote_name, f"refs/heads/{branch}"
            )
            if remote_branch:
                raise ReleaseError(
                    f"远端修复分支已存在但本地状态缺失，拒绝猜测恢复: {branch}"
                )
            git.run("switch", "-c", branch)
            git.stage_raw(request.changed_files)
            git.run("commit", "-m", request.commit_message)
            if git.changed_files():
                raise ReleaseError("提交后仓库仍有未授权或未提交改动")
            commit_sha = git.run("rev-parse", "HEAD")

        if (
            _git_tree_digest(git, commit_sha, report.policy.workspace_ignore)
            != report.candidate_digest
        ):
            raise ReleaseError("Git commit tree 与已验证 candidate 内容不一致")
        refreshed_base = git.run(
            "ls-remote", "--heads", app.remote_name, f"refs/heads/{app.base_branch}"
        )
        if not refreshed_base or refreshed_base.split()[0] != remote_base_sha:
            raise ReleaseError("目标分支在发布期间发生变化，必须重新基线化和验证")
        remote_branch = git.run(
            "ls-remote", "--heads", app.remote_name, f"refs/heads/{branch}"
        )
        if remote_branch:
            if remote_branch.split()[0] != commit_sha:
                raise ReleaseError("远端修复分支与本地已验证提交不一致")
        else:
            git.push(app.remote_name, branch, github_token)

        try:
            verify_live_replay_barriers(
                app.verification_observability_database,
                run_id=request.verification_run_id,
                cycle=request.verification_cycle,
                manifest=request.verification_replay_manifest,
            )
        except ValueError as exc:
            raise ReleaseError(f"创建 PR 前 OTLP barrier 复核失败: {exc}") from exc
        number, url = publisher.create_or_get(
            repository=app.github_repository,
            branch=branch,
            base_branch=app.base_branch,
            title=request.title,
            body=request.body,
            reviewers=app.reviewers,
            draft=request.draft,
        )
        receipt = PullRequestReceipt(
            app_id=app.app_id,
            repository=app.github_repository,
            branch=branch,
            base_branch=app.base_branch,
            commit_sha=commit_sha,
            pull_request_number=number,
            pull_request_url=url,
            reviewers=app.reviewers,
            verification_run_id=report.run_id,
            verification_cycle=report.cycle,
            verification_incident_id=report.incident_id,
            verification_incident_digest=report.incident_digest,
            candidate_digest=report.candidate_digest,
            verification_report_digest=canonical_json_digest(
                report.model_dump(mode="json")
            ),
            verification_report_path=report_path,
        )
        self._persist_receipt(receipt, app.release_receipt_root)
        return receipt

    def _validate_application(self, request: ReleaseRequest) -> ApplicationSpec:
        try:
            app = self.registry.get(request.app_id)
        except KeyError as exc:
            raise ReleaseError(str(exc)) from exc
        configured = Path(app.repository_path).expanduser().resolve()
        requested = Path(request.repository_path).expanduser().resolve()
        if requested != configured:
            raise ReleaseError("安全校验失败: App ID 与仓库路径不匹配")
        git = _Git(requested)
        actual_root = Path(git.run("rev-parse", "--show-toplevel")).resolve()
        if actual_root != requested:
            raise ReleaseError("安全校验失败: 当前目录不是登记的仓库根目录")
        actual_remote = git.run("remote", "get-url", app.remote_name)
        if _normalize_remote(actual_remote) != _normalize_remote(app.remote_url):
            raise ReleaseError("安全校验失败: Git remote 与登记仓库地址不匹配")
        actual_push_remote = git.run("remote", "get-url", "--push", app.remote_name)
        if _normalize_remote(actual_push_remote) != _normalize_remote(app.remote_url):
            raise ReleaseError("安全校验失败: Git push remote 与登记仓库地址不匹配")
        remote_host, remote_repository = _normalize_remote(app.remote_url)
        if remote_host != "github.com" or remote_repository != app.github_repository:
            raise ReleaseError("安全校验失败: App 登记信息中的 GitHub 仓库不一致")
        return app

    @staticmethod
    def _persist_receipt(
        receipt: PullRequestReceipt, receipt_root: str | Path
    ) -> None:
        root = Path(receipt_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{receipt.verification_run_id}.json"
        if target.exists():
            existing = PullRequestReceipt.model_validate_json(
                target.read_text(encoding="utf-8")
            )
            if existing != receipt:
                raise ReleaseError("同一 verification run 已存在不同 MR 回执")
            return
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=root, prefix=".receipt-", delete=False
        ) as handle:
            json.dump(
                receipt.model_dump(mode="json"),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        os.replace(temporary, target)


__all__ = ["GitHubPullRequestPublisher", "ReleaseError", "ReleaseManager"]
