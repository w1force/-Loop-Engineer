"""skill 发现与元数据解析。

扫描 skill_dirs 的直接子目录,解析每个 SKILL.md 的 YAML frontmatter,产出 SkillMeta。
frontmatter 宽松:容忍未知字段,YAML 损坏/缺 description → 跳过该 skill(不中断整体扫描)。

SkillMeta 已移到 core/types.py(避免底层 types 反向依赖上层 skills;本模块单向 import)。
render_catalog/append_catalog 已删除(逻辑移到 Task 4 的 build_system_prompt)。
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import yaml

from ..types import SkillMeta

logger = logging.getLogger(__name__)


def _parse_frontmatter(skill_md: Path) -> dict:
    """解析 SKILL.md 的 YAML frontmatter。
    无 frontmatter / 无闭合 --- / YAML 损坏 / 非 dict → 返回 {}。"""
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    fm_text = "\n".join(lines[1:end])
    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


class SkillLoader:
    @staticmethod
    def scan(skill_dirs: Sequence[str | Path]) -> list[SkillMeta]:
        """扫描所有 skill_dirs 的直接子目录,解析 SKILL.md frontmatter。
        返回按 name 排序的 list[SkillMeta]。容错:单 skill 失败不影响其他。"""
        metas: dict[str, SkillMeta] = {}  # name -> meta(同 name 后者覆盖)
        for d in skill_dirs:
            root = Path(d)
            if not root.is_dir():
                logger.warning("skill dir not found, skipped: %s", root)
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                skill_md = child / "SKILL.md"
                if not skill_md.is_file():
                    continue  # 无 SKILL.md:当普通子目录,静默跳过
                fm = _parse_frontmatter(skill_md)
                desc = fm.get("description")
                if not isinstance(desc, str) or not desc.strip():
                    logger.warning("skill missing description, skipped: %s", child.name)
                    continue
                if child.name in metas:
                    logger.warning("duplicate skill name, later overrides: %s", child.name)
                metas[child.name] = SkillMeta(
                    name=child.name,
                    description=desc.strip(),
                    skill_dir=child,
                    skill_md=skill_md,
                )
        return sorted(metas.values(), key=lambda m: m.name)
