"""Verification 结构化报告的本地原子存储。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import VerificationReport


class VerificationEvidenceStore(Protocol):
    async def persist(self, report: VerificationReport) -> str: ...


class JsonEvidenceStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    async def persist(self, report: VerificationReport) -> str:
        return await asyncio.to_thread(self._persist_sync, report)

    def _persist_sync(self, report: VerificationReport) -> str:
        # 不信任调用方持有对象的可变容器；序列化后重新走全部交叉校验。
        report = VerificationReport.model_validate(report.model_dump(mode="python"))
        self.root.mkdir(parents=True, exist_ok=True)
        target = (self.root / report.run_id / f"cycle-{report.cycle}").resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("verification evidence 目标路径越界") from exc
        if target.exists():
            raise FileExistsError(f"verification evidence 已存在，拒绝覆盖: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".verification-tmp-", dir=target.parent)
        )
        try:
            payload = report.model_dump(mode="json")
            report_path = temporary / "report.json"
            report_bytes = (
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            report_path.write_bytes(report_bytes)
            self._write_additional_files(
                temporary,
                report=report,
                report_bytes=report_bytes,
            )
            report_path.chmod(0o600)
            command_path = temporary / "command-evidence.json"
            command_path.write_text(
                json.dumps(
                    {
                        "run_id": report.run_id,
                        "candidate_ref": report.candidate_ref,
                        "candidate_digest": report.candidate_digest,
                        "evidence": [
                            item.model_dump(mode="json")
                            for item in report.command_evidence
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            command_path.chmod(0o600)
            external_path = temporary / "external-evidence.json"
            external_path.write_text(
                json.dumps(
                    {
                        "trace": (
                            report.trace_evidence.model_dump(mode="json")
                            if report.trace_evidence is not None
                            else None
                        ),
                        "logs": (
                            report.log_evidence.model_dump(mode="json")
                            if report.log_evidence is not None
                            else None
                        ),
                        "behavior": (
                            report.behavior_evidence.model_dump(mode="json")
                            if report.behavior_evidence is not None
                            else None
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            external_path.chmod(0o600)
            temporary.chmod(0o700)
            os.replace(temporary, target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return str(target)

    def _write_additional_files(
        self,
        directory: Path,
        *,
        report: VerificationReport,
        report_bytes: bytes,
    ) -> None:
        return


class VerificationAttestation(BaseModel):
    """HMAC envelope created by the trusted coordinator, never by the repair Agent."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: str = Field(pattern=r"^verification-attestation/v1$")
    app_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    cycle: int = Field(ge=1, le=3)
    candidate_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_digests: dict[str, str] = Field(default_factory=dict)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: str = Field(min_length=1)
    mac_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _valid_skill_digests(self):
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for name, digest in self.skill_digests.items()
        ):
            raise ValueError("attestation skill_digests 非法")
        return self


def _attestation_bytes(payload: dict[str, object], report_bytes: bytes) -> bytes:
    metadata = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return b"verification-attestation/v1\0" + metadata + b"\0" + report_bytes


class AttestedJsonEvidenceStore(JsonEvidenceStore):
    """Persist reports with an application-bound coordinator HMAC."""

    def __init__(
        self,
        root: str | Path,
        *,
        signing_key: bytes,
        app_id: str,
        repository: str,
    ):
        super().__init__(root)
        if len(signing_key) < 32:
            raise ValueError("verification signing key 至少需要 32 字节")
        self.signing_key = bytes(signing_key)
        self.app_id = app_id
        self.repository = repository

    def _write_additional_files(
        self,
        directory: Path,
        *,
        report: VerificationReport,
        report_bytes: bytes,
    ) -> None:
        body: dict[str, object] = {
            "schema_version": "verification-attestation/v1",
            "app_id": self.app_id,
            "repository": self.repository,
            "run_id": report.run_id,
            "cycle": report.cycle,
            "candidate_digest": report.candidate_digest,
            "policy_digest": report.policy_digest,
            "skill_digests": report.skill_digests,
            "report_sha256": sha256(report_bytes).hexdigest(),
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
        mac = hmac.new(
            self.signing_key,
            _attestation_bytes(body, report_bytes),
            sha256,
        ).hexdigest()
        attestation = VerificationAttestation.model_validate(
            {**body, "mac_sha256": mac}
        )
        path = directory / "attestation.json"
        path.write_text(
            json.dumps(
                attestation.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    @classmethod
    def load_attested(
        cls,
        root: str | Path,
        *,
        run_id: str,
        cycle: int,
        signing_key: bytes,
        app_id: str,
        repository: str,
        policy_digest: str,
        skill_digests: dict[str, str],
    ) -> tuple[VerificationReport, str]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id):
            raise ValueError("verification run_id 非法")
        if cycle not in {1, 2, 3}:
            raise ValueError("verification cycle 必须在 1..3")
        if len(signing_key) < 32:
            raise ValueError("verification signing key 至少需要 32 字节")
        evidence_root = Path(root).expanduser().resolve()
        target = (evidence_root / run_id / f"cycle-{cycle}").resolve()
        try:
            target.relative_to(evidence_root)
        except ValueError as exc:
            raise ValueError("verification evidence 路径越界") from exc
        report_path = target / "report.json"
        attestation_path = target / "attestation.json"
        if not report_path.is_file() or not attestation_path.is_file():
            raise FileNotFoundError("受信 verification report/attestation 不存在")
        report_bytes = report_path.read_bytes()
        attestation = VerificationAttestation.model_validate_json(
            attestation_path.read_text(encoding="utf-8")
        )
        body = attestation.model_dump(mode="json", exclude={"mac_sha256"})
        expected_mac = hmac.new(
            signing_key,
            _attestation_bytes(body, report_bytes),
            sha256,
        ).hexdigest()
        if not hmac.compare_digest(attestation.mac_sha256, expected_mac):
            raise ValueError("verification attestation 签名无效")
        if sha256(report_bytes).hexdigest() != attestation.report_sha256:
            raise ValueError("verification report 内容摘要不匹配")
        expected = {
            "app_id": app_id,
            "repository": repository,
            "run_id": run_id,
            "cycle": cycle,
            "policy_digest": policy_digest,
            "skill_digests": skill_digests,
        }
        mismatches = [
            name
            for name, value in expected.items()
            if getattr(attestation, name) != value
        ]
        if mismatches:
            raise ValueError(
                "verification attestation 与受信配置不一致: "
                + ", ".join(mismatches)
            )
        report = VerificationReport.model_validate_json(report_bytes)
        if (
            report.run_id != attestation.run_id
            or report.cycle != attestation.cycle
            or report.candidate_digest != attestation.candidate_digest
            or report.policy_digest != attestation.policy_digest
            or report.skill_digests != attestation.skill_digests
        ):
            raise ValueError("verification report 与 attestation 绑定不一致")
        return report, str(report_path)


__all__ = [
    "AttestedJsonEvidenceStore",
    "JsonEvidenceStore",
    "VerificationAttestation",
    "VerificationEvidenceStore",
]
