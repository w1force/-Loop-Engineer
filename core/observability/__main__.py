"""CLI for the embedded local observability service."""

from __future__ import annotations

import argparse
import json
import os

from .server import serve
from .store import CCBDebugLogImporter, LocalObservabilityStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m core.observability")
    parser.add_argument(
        "--database",
        default=".loop-engineer/observability.sqlite3",
        help="SQLite database path",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="initialize the database")
    init.set_defaults(action="init")

    server = commands.add_parser("serve", help="serve OTLP/HTTP JSON on loopback")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=4318)
    server.add_argument("--ccb-debug-dir")
    server.set_defaults(action="serve")

    importer = commands.add_parser("import-ccb", help="import CCB debug files once")
    importer.add_argument("directory")
    importer.set_defaults(action="import")

    logs = commands.add_parser("logs", help="query stored logs")
    logs.add_argument("--trace-id")
    logs.add_argument("--session-id")
    logs.add_argument("--run-id")
    logs.add_argument("--scenario-id")
    logs.add_argument("--variant", choices=("control", "candidate"))
    logs.add_argument("--service-name")
    logs.add_argument("--level")
    logs.add_argument("--start-time-ns", type=int)
    logs.add_argument("--end-time-ns", type=int)
    logs.add_argument("--limit", type=int, default=100)
    logs.set_defaults(action="logs")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.action == "init":
        store = LocalObservabilityStore(args.database)
        print(store.path)
        return
    if args.action == "serve":
        coordinator_token = os.environ.get("LOOP_ENGINEER_COORDINATOR_TOKEN", "")
        if not coordinator_token:
            raise SystemExit(
                "LOOP_ENGINEER_COORDINATOR_TOKEN is required for execution-window writes"
            )
        serve(
            database=args.database,
            host=args.host,
            port=args.port,
            coordinator_token=coordinator_token,
            otlp_token=os.environ.get("LOOP_ENGINEER_OTLP_TOKEN"),
            ccb_debug_dir=args.ccb_debug_dir,
        )
        return
    store = LocalObservabilityStore(args.database)
    if args.action == "import":
        print(CCBDebugLogImporter(store).import_directory(args.directory))
        return
    if args.action == "logs":
        print(
            json.dumps(
                store.search_logs(
                    trace_id=args.trace_id,
                    session_id=args.session_id,
                    run_id=args.run_id,
                    scenario_id=args.scenario_id,
                    variant=args.variant,
                    service_name=args.service_name,
                    level=args.level,
                    start_time_ns=args.start_time_ns,
                    end_time_ns=args.end_time_ns,
                    limit=args.limit,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
