"""Built-in subagent definitions."""

from .verification import (
    VERIFICATION_AGENT_TYPE,
    VERIFICATION_MAIN_AGENT_GUIDANCE,
    VERIFICATION_SYSTEM_PROMPT,
    build_verification_can_use_tool,
    select_verification_tools,
)

__all__ = [
    "VERIFICATION_AGENT_TYPE",
    "VERIFICATION_MAIN_AGENT_GUIDANCE",
    "VERIFICATION_SYSTEM_PROMPT",
    "build_verification_can_use_tool",
    "select_verification_tools",
]
