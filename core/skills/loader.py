"""skill 发现与元数据解析。

扫描 skill_dirs 的直接子目录,解析每个 SKILL.md 的 YAML frontmatter,产出 SkillMeta。
frontmatter 宽松:容忍未知字段,YAML 损坏/缺 description → 跳过该 skill(不中断整体扫描)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillMeta:
    """一个 skill 的元数据。"""
    name: str            # = 目录名,skill 标识(load_skill 入参)
    description: str     # frontmatter.description,进 system 目录段
    skill_dir: Path      # skill 目录绝对路径
    skill_md: Path       # SKILL.md 绝对路径(= skill_dir / "SKILL.md")


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
    def scan(skill_dirs: list[str | Path]) -> list[SkillMeta]:
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


def render_catalog(metas: list[SkillMeta]) -> str:
    """metas → <skills>...</skills> 目录段 + 调用指引。空 metas 返回 ''。"""
    if not metas:
        return ""
    lines = ["", "", "<skills>"]
    for m in metas:
        desc = " ".join(m.description.split())  # 压缩多行空白成单行
        lines.append(f"- name: {m.name}")
        lines.append(f"  description: {desc}")
    lines.append("</skills>")
    lines.append("")
    lines.append("当用户请求匹配某个 skill 时,调用 load_skill(name) 加载完整指令后再执行。")
    return "\n".join(lines)


def append_catalog(system: str | list[dict], catalog: str) -> str | list[dict]:
    """把 catalog 拼到 system 末尾,兼容 str 与 list[dict] 两种形态。"""
    if isinstance(system, str):
        return system + catalog
    return [*system, {"type": "text", "text": catalog}]
