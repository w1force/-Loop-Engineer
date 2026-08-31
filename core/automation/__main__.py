"""Automation entry point.

Daemon (self-scheduling; scans every N seconds + daily full sweep + drains queue):

    python -m core.automation --config automation.json

One-shot (for external cron / systemd timers — no long-running process):

    python -m core.automation --config automation.json --once incremental
    python -m core.automation --config automation.json --once full
    python -m core.automation --config automation.json --once drain

Sample crontab (5-min incremental + daily full + minute worker):

    */5 * * * *  cd /srv/loop && python -m core.automation --config automation.json --once incremental
    17 3 * * *   cd /srv/loop && python -m core.automation --config automation.json --once full
    * * * * *    cd /srv/loop && python -m core.automation --config automation.json --once drain

Sample systemd timer: a .service running ``--once incremental`` + a .timer with
``OnCalendar=*:0/5``. Either way discovery only enqueues; the worker (or --once drain)
drives the agent, so the cron path never invokes an agent directly.

With no --config (or a config with blank addresses) the loop runs as a safe no-op
until sources are filled in.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .registry import AutomationConfig
from .service import AutomationService


def _load_config(path: str | None) -> AutomationConfig:
    if not path:
        return AutomationConfig()
    return AutomationConfig.from_json_file(path)


async def _run_daemon(service: AutomationService) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError):
            pass  # e.g. Windows / non-main thread
    await service.run_forever(stop)


async def _run_once(service: AutomationService, action: str) -> None:
    if action == "drain":
        summary = await service.drain_once()
    else:
        summary = await service.scan_once(mode=action)
    print(summary)


def main() -> None:
    parser = argparse.ArgumentParser(prog="core.automation")
    parser.add_argument("--config", help="path to automation config JSON")
    parser.add_argument(
        "--once",
        choices=("incremental", "full", "drain"),
        help="run a single scan/drain and exit (for external cron/systemd)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    service = AutomationService.build(_load_config(args.config))
    if args.once:
        asyncio.run(_run_once(service, args.once))
    else:
        asyncio.run(_run_daemon(service))


if __name__ == "__main__":
    main()
