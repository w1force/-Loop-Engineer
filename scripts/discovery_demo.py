#!/usr/bin/env python
"""Demo: ingest a CCB run log into discovery and print the incident + DiagnosisRequest.

End-to-end bridge between the two projects (no running coordinator needed):

    # 1. produce a CCB run log (in the CCB repo)
    python -m python_port.agent_service --scenario timeout --log /tmp/ccb-timeout.jsonl

    # 2. scan it here
    python scripts/discovery_demo.py /tmp/ccb-timeout.jsonl \
        --service claude-code --control /Users/wangzheng.440/claude-code --ref HEAD

Prints the deterministic detection, the deduped incident, and the DiagnosisRequest
that would feed LoopEngineer.run(). Uses a throwaway state DB unless --state is given.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on path

from core.connectors.logs import JsonlRunLogConnector
from core.discovery import DiscoveryPipeline, incident_to_diagnosis_request
from core.state import LoopStateStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a run log through discovery")
    parser.add_argument("log_path", help="JSONL run-log to scan (e.g. a CCB run)")
    parser.add_argument("--service", default="claude-code")
    parser.add_argument("--source-id", default="ccb-run")
    parser.add_argument("--control", default=".", help="control workspace for the DiagnosisRequest")
    parser.add_argument("--ref", default="HEAD", help="frozen control ref")
    parser.add_argument("--state", default=None, help="state DB path (default: temp)")
    args = parser.parse_args(argv)

    state_path = args.state or str(Path(tempfile.mkdtemp()) / "state.db")
    store = LoopStateStore(state_path)
    connector = JsonlRunLogConnector(store, source_id=args.source_id)
    pipeline = DiscoveryPipeline(store)

    result = pipeline.scan(connector, args.log_path, service=args.service)
    print(f"scanned {result.records_scanned} records; "
          f"{len(result.new_incidents)} new incident(s), "
          f"{len(result.updated_incidents)} deduped")

    for incident in result.new_incidents:
        print(f"\n[incident {incident.incident_id}] rule={incident.matched_rule} "
              f"severity={incident.severity} eligibility={incident.eligibility} "
              f"occurrences={incident.occurrences}")
        request = incident_to_diagnosis_request(
            incident, control_workspace=args.control, control_ref=args.ref
        )
        print(f"  -> DiagnosisRequest: matched_rule={request.matched_rule}")
        print(f"     requirement: {request.requirement[:100]}")
        print(f"     control: {request.control_ref} @ {request.control_workspace}")
        print(f"     evidence: {request.error_logs[0].uri}")

    if not result.new_incidents:
        print("\n(no actionable incidents — healthy run or no matching rule)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
