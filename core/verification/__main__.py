"""Trusted CLI path: SQLite evidence -> hard gates -> signed report."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from core.observability import (
    SQLiteBehaviorEvidenceProvider,
    SQLiteLogEvidenceProvider,
    SQLiteTraceEvidenceProvider,
)

from .engine import VerificationEngine
from .models import VerificationPolicy, VerificationRunRequest, VerificationVerdict
from .replay import ReplayBatchReceipt
from .skill import VerificationSkillLoader
from .store import AttestedJsonEvidenceStore


def _load_json(path: str) -> object:
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复 JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        Path(path).expanduser().read_text(encoding="utf-8"),
        object_pairs_hook=no_duplicates,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m core.verification")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--replay-receipt", required=True)
    parser.add_argument("--skills-root", action="append", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--repository", required=True, help="GitHub owner/repository")
    parser.add_argument(
        "--signing-key-env",
        default="LOOP_ENGINEER_VERIFICATION_SIGNING_KEY",
    )
    return parser


async def _run(args: argparse.Namespace) -> tuple[str, str]:
    signing_key = os.environ.get(args.signing_key_env, "").encode("utf-8")
    if not signing_key:
        raise RuntimeError(
            f"缺少 Verification 签名密钥环境变量: {args.signing_key_env}"
        )
    policy = VerificationPolicy.model_validate(_load_json(args.policy))
    receipt = ReplayBatchReceipt.model_validate(_load_json(args.replay_receipt))
    if not receipt.passed:
        raise RuntimeError("replay receipt 未通过，禁止执行硬验证或签名")
    request_payload = _load_json(args.request)
    if not isinstance(request_payload, dict):
        raise ValueError("verification request 必须是 JSON object")
    declared_replay_digest = request_payload.get("replay_digest")
    if declared_replay_digest is not None and declared_replay_digest != receipt.digest:
        raise ValueError("verification request 与 replay receipt digest 不一致")
    declared_manifest = request_payload.get("replay_manifest")
    receipt_manifest = receipt.replay_manifest.model_dump(mode="json")
    if declared_manifest is not None and declared_manifest != receipt_manifest:
        raise ValueError("verification request 与 replay receipt manifest 不一致")
    request_payload["replay_digest"] = receipt.digest
    request_payload["replay_manifest"] = receipt_manifest
    request = VerificationRunRequest.model_validate(request_payload)
    expected = {
        "run_id": request.run_id,
        "cycle": request.cycle,
        "plan_digest": request.plan_digest,
        "control_digest": request.control_digest,
        "candidate_ref": request.candidate_ref,
        "candidate_digest": request.expected_candidate_digest,
        "policy_digest": request.expected_policy_digest,
        "skill_digests": request.expected_skill_digests,
    }
    mismatches = [
        name for name, value in expected.items() if getattr(receipt, name) != value
    ]
    receipt_inputs = {
        item.scenario_id: item.input_digest for item in receipt.scenario_receipts
    }
    if receipt_inputs != request.scenario_input_digests:
        mismatches.append("scenario_input_digests")
    if mismatches:
        raise ValueError(
            "replay receipt 与 verification request 绑定不一致: "
            + ", ".join(mismatches)
        )
    engine = VerificationEngine(
        policy=policy,
        skill_loader=VerificationSkillLoader(args.skills_root),
        trace_provider=SQLiteTraceEvidenceProvider(args.database),
        log_provider=SQLiteLogEvidenceProvider(args.database),
        behavior_provider=SQLiteBehaviorEvidenceProvider(args.database),
    )
    report = await engine.verify(request)
    if report.verdict is VerificationVerdict.VERIFIED:
        raise RuntimeError(
            "standalone verification CLI 不得签发 VERIFIED；请通过受信 "
            "VerificationCoordinator 执行 replay、硬门禁和签名"
        )
    store = AttestedJsonEvidenceStore(
        args.evidence_root,
        signing_key=signing_key,
        app_id=args.app_id,
        repository=args.repository,
    )
    location = await store.persist(report)
    return report.verdict.value, location


def main() -> None:
    args = _parser().parse_args()
    verdict, location = asyncio.run(_run(args))
    print(
        json.dumps(
            {"verdict": verdict, "evidence_location": location},
            ensure_ascii=False,
            indent=2,
        )
    )
    if verdict != VerificationVerdict.VERIFIED.value:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
