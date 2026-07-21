"""Task 5: 示范 skill bullet-summarize 冒烟测试(扫描真实 skills/)。"""
from pathlib import Path

from core.skills import SkillLoader

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SKILLS_DIR = _PROJECT_ROOT / "skills"


def test_bullet_summarize_discoverable():
    """skills/bullet-summarize 能被 scan 发现。"""
    metas = SkillLoader.scan([_SKILLS_DIR])
    assert "bullet-summarize" in [m.name for m in metas]


def test_bullet_summarize_description_present():
    """bullet-summarize 的 description 非空且语义正确。"""
    metas = SkillLoader.scan([_SKILLS_DIR])
    m = next((x for x in metas if x.name == "bullet-summarize"), None)
    assert m is not None
    assert "总结" in m.description or "summarize" in m.description.lower()
