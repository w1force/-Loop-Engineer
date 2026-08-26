"""Explicit CLI entrypoint for VERIFIED -> GitHub PR publication."""

from __future__ import annotations

import argparse
import json

from .github import ReleaseManager
from .models import ApplicationRegistry, ReleaseRequest


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m core.release")
    parser.add_argument("--registry", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    registry = ApplicationRegistry.load(args.registry)
    request = ReleaseRequest.model_validate_json(
        open(args.request, encoding="utf-8").read()
    )
    receipt = ReleaseManager(registry).release(request)
    print(json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
