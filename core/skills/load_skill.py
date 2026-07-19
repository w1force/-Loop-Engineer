"""load_skill 工具:按需加载指定 skill 的 SKILL.md 全文(对标 CC progressive disclosure)。

模型先看 system 里的 <skills> 目录(name+description),决策后调用本工具加载完整指令。
工厂闭包捕获 metas,和 builtin_tools(read_state) 同模式。
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from core.tools import Tool, ToolContext
from .loader import SkillMeta, SkillLoader, render_catalog, append_catalog

logger = logging.getLogger(__name__)


class LoadSkillInput(BaseModel):
    name: str


def load_skill_tool(metas: list[SkillMeta]) -> Tool:
    """工厂闭包:捕获 metas,返回 load_skill Tool。"""
    index = {m.name: m for m in metas}

    async def _load(inp: LoadSkillInput, ctx: ToolContext) -> str:
        meta = index.get(inp.name)
        if meta is None:
            return f"Error: skill '{inp.name}' not found. Available: {sorted(index)}"
        try:
            return meta.skill_md.read_text(encoding="utf-8")  # 返回 SKILL.md 全文
        except OSError as e:
            return f"Error: cannot read skill '{inp.name}': {e}"

    return Tool(
        name="load_skill",
        description="加载指定 skill 的完整指令。先看 <skills> 目录决定用哪个 skill,再调用此工具。",
        input_model=LoadSkillInput,
        func=_load,
        is_concurrency_safe=True,  # 只读 → 可并发
    )


def prepare_skills(
    skill_dirs: Sequence[str | Path],
    system: str | list[dict],
    tools: list[Tool],
) -> tuple[str | list[dict], list[Tool]]:
    """扫描 + 注入:有 skill 则拼目录到 system + 追加 load_skill 工具。
    无 skill / 扫描异常 → 原样返回(降级,不中断主流程)。"""
    try:
        metas = SkillLoader.scan(list(skill_dirs))
    except Exception as e:
        logger.warning("skill scan failed, disabled: %s", e)
        return system, tools
    if not metas:
        return system, tools
    system = append_catalog(system, render_catalog(metas))
    tools = [*tools, load_skill_tool(metas)]
    return system, tools
