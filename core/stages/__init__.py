"""Stage services for the Loop Engineer pipeline.

Each stage runs a fresh-context agent driven by a frozen top-level SKILL.md and a
restricted tool policy, then hands a structured artifact to the orchestrator:

    diagnosis -> DiagnosisProposal -> (IncidentFreezer) -> IncidentBundle
    repair    -> RepairResult      -> (CandidateSnapshotter) -> CandidateSnapshot
"""
