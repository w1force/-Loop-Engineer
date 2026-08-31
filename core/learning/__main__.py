"""Poll human GitHub reviews and finalize Repair Skill learning."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from config import get_settings
from core.providers.anthropic import AnthropicAdapter
from core.release.review import GitHubReviewClient, GitHubReviewMonitor

from .review_worker import PendingReviewWorker
from .runtime import build_default_learning_service


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m core.learning")
    parser.add_argument(
        "--github-token-env",
        default="GITHUB_TOKEN",
        help="environment variable containing the GitHub token",
    )
    parser.add_argument(
        "--watch-interval",
        type=float,
        default=0,
        help="poll forever at this interval; zero performs one poll",
    )
    parser.add_argument(
        "--receipt-root",
        action="append",
        default=[],
        help=(
            "ReleaseManager receipt directory used to recover a PR created before "
            "the learning archive was bound; may be repeated"
        ),
    )
    return parser


async def _build_worker(
    args: argparse.Namespace,
) -> tuple[PendingReviewWorker, GitHubReviewClient]:
    token = os.environ.get(args.github_token_env, "")
    if not token:
        raise RuntimeError(f"missing GitHub token environment: {args.github_token_env}")
    settings = get_settings()
    if not settings.api_key:
        raise RuntimeError("missing LOOP_ENGINEER_API_KEY for trajectory distillation")
    provider = AnthropicAdapter(
        api_key=settings.api_key,
        base_url=settings.base_url,
        debug_sse=False,
        thinking_budget_tokens=settings.thinking_budget_tokens,
    )
    learning = build_default_learning_service(provider, agent_model=settings.model)
    client = GitHubReviewClient(token)
    return (
        PendingReviewWorker(
            archive=learning.archive,
            monitor=GitHubReviewMonitor(client=client, learning_sink=learning),
            receipt_roots=tuple(args.receipt_root),
        ),
        client,
    )


async def _run(args: argparse.Namespace) -> int:
    if args.watch_interval < 0:
        raise ValueError("watch-interval cannot be negative")
    worker, client = await _build_worker(args)
    try:
        while True:
            summary = await worker.poll_once()
            print(json.dumps(summary.__dict__, ensure_ascii=False, sort_keys=True))
            if args.watch_interval == 0:
                return 2 if summary.failures else 0
            await asyncio.sleep(args.watch_interval)
    finally:
        client.close()


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
