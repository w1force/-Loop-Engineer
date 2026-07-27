
from __future__ import annotations

from ...types import AgentState, UserMessage


def _format_catalog(skills) -> str:
    """每行 `- {name}: {description}`(对齐 CC formatCommandsWithinBudget 的基本形态)。

    description 里的多行空白压成单行,避免撑乱清单。
    """
    return "\n".join(f"- {m.name}: {' '.join(m.description.split())}" for m in skills)


def inject_skill_listing(agent_state: AgentState) -> None:

    new_skills = [
        m for m in agent_state.skills if m.name not in agent_state.sent_skill_names
    ]
    if not new_skills:
        return

    content = (
        "<system-reminder>\n"
        "The following skills are available for use with the Load_Skill tool:\n\n"
        f"{_format_catalog(new_skills)}\n"
        "</system-reminder>"
    )
    msg = UserMessage(content=content)

    #围绕KV cache，append到new message之后
    agent_state.messages.append(msg)

    for m in new_skills:
        agent_state.sent_skill_names.add(m.name)
