"""Connectors: protocol adapters for external systems (logs, OTLP, git, github).

Connectors only adapt protocols into structured records; they hold no business
state machine and never see credentials meant for the control plane.
"""
