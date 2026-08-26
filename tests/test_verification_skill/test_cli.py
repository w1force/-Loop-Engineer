from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace

import pytest

from core.verification import VerificationVerdict
from core.verification import __main__ as verification_cli


def test_standalone_cli_cannot_sign_verified_report(monkeypatch, tmp_path) -> None:
    digest = "a" * 64
    skill_digests = {"checkout": "b" * 64}
    request = SimpleNamespace(
        run_id="run-1",
        cycle=1,
        plan_digest="c" * 64,
        control_digest="d" * 64,
        candidate_ref="candidate-1",
        expected_candidate_digest="e" * 64,
        expected_policy_digest="f" * 64,
        expected_skill_digests=skill_digests,
        scenario_input_digests={"checkout:case": "1" * 64},
    )
    receipt = SimpleNamespace(
        passed=True,
        digest=digest,
        run_id=request.run_id,
        cycle=request.cycle,
        plan_digest=request.plan_digest,
        control_digest=request.control_digest,
        candidate_ref=request.candidate_ref,
        candidate_digest=request.expected_candidate_digest,
        policy_digest=request.expected_policy_digest,
        skill_digests=skill_digests,
        scenario_receipts=(
            SimpleNamespace(
                scenario_id="checkout:case",
                input_digest="1" * 64,
            ),
        ),
        replay_manifest=SimpleNamespace(model_dump=lambda mode: {}),
    )

    payloads = {
        "policy.json": {},
        "receipt.json": {},
        "request.json": {},
    }
    monkeypatch.setenv("TEST_SIGNING_KEY", "s" * 32)
    monkeypatch.setattr(
        verification_cli,
        "_load_json",
        lambda path: payloads[path],
    )
    monkeypatch.setattr(
        verification_cli.VerificationPolicy,
        "model_validate",
        lambda value: object(),
    )
    monkeypatch.setattr(
        verification_cli.ReplayBatchReceipt,
        "model_validate",
        lambda value: receipt,
    )
    monkeypatch.setattr(
        verification_cli.VerificationRunRequest,
        "model_validate",
        lambda value: request,
    )

    class _Engine:
        def __init__(self, **kwargs):
            pass

        async def verify(self, actual_request):
            assert actual_request is request
            return SimpleNamespace(verdict=VerificationVerdict.VERIFIED)

    monkeypatch.setattr(verification_cli, "VerificationEngine", _Engine)
    monkeypatch.setattr(
        verification_cli,
        "AttestedJsonEvidenceStore",
        lambda *args, **kwargs: pytest.fail("VERIFIED must not reach the signer"),
    )

    args = argparse.Namespace(
        policy="policy.json",
        request="request.json",
        replay_receipt="receipt.json",
        skills_root=[str(tmp_path / "skills")],
        database=str(tmp_path / "evidence.sqlite3"),
        evidence_root=str(tmp_path / "evidence"),
        app_id="app",
        repository="owner/repository",
        signing_key_env="TEST_SIGNING_KEY",
    )

    with pytest.raises(RuntimeError, match="standalone verification CLI"):
        asyncio.run(verification_cli._run(args))
