"""load_skill 工具:按需加载 SKILL.md 全文(从 ctx.agent_state.skills 动态取,不闭包)。"""
from __future__ import annotations

from pydantic import BaseModel

from ..tools import ToolContext, build_tool
from ..types import SkillMeta


class LoadSkillInput(BaseModel):
    name: str


async def _load(inp: LoadSkillInput, ctx: ToolContext) -> str:
    skills: list[SkillMeta] = ctx.agent_state.skills   # ★ 从 ctx 动态取
    index = {m.name: m for m in skills}
    meta = index.get(inp.name)
    if meta is None:
        return f"Error: skill '{inp.name}' not found. Available: {sorted(index)}"
    try:
        return meta.skill_md.read_text(encoding="utf-8")
    except OSError as e:
        return f"Error: cannot read skill '{inp.name}': {e}"


LOAD_SKILL_TOOL = build_tool(
    name="Load_Skill",
    description="加载指定 skill 的完整指令。先看 <skills> 目录决定用哪个 skill,再调用此工具。",
    input_model=LoadSkillInput,
    func=_load,
    is_concurrency_safe=True,
)
