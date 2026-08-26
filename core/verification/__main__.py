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
    request = VerificationRunRequest.model_validate(_load_json(args.request))
    engine = VerificationEngine(
        policy=policy,
        skill_loader=VerificationSkillLoader(args.skills_root),
        trace_provider=SQLiteTraceEvidenceProvider(args.database),
        log_provider=SQLiteLogEvidenceProvider(args.database),
        behavior_provider=SQLiteBehaviorEvidenceProvider(args.database),
    )
    report = await engine.verify(request)
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
