"""skill 系统:发现 + 元数据。

SkillMeta 定义在 core/types(避免底层 types 反向依赖上层 skills);外部统一从 core.types 取。
load_skill_tool 在 core/builtin_tools/load_skill.py(从 ctx.agent_state.skills 读)。
Task 4:prepare_skills 已退役(system 注入由 core.agent_loop.build_system_prompt 负责)。
"""
from .loader import SkillLoader

__all__ = [
    "SkillLoader",
]
