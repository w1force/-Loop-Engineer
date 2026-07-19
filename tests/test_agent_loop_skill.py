"""Task 4: skill 注入逻辑(prepare_skills)测试。

prepare_skills 是纯函数,覆盖注入/降级/拼接;submit 接线靠 pyright + Task 5 冒烟。
"""
from pydantic import BaseModel

from core.skills import prepare_skills
from core.tools import Tool


class _DummyIn(BaseModel):
    pass


def _dummy_tool(name: str) -> Tool:
    async def _f(inp, ctx):
        return "ok"
    return Tool(name=name, description="d", input_model=_DummyIn, func=_f)


def test_prepare_skills_injects(tmp_path):
    skills = tmp_path / "skills"
    d = skills / "foo"; d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: foo\n---\n# foo\n", encoding="utf-8")
    existing = [_dummy_tool("read")]
    system, tools = prepare_skills([skills], "base system", existing)
    assert "<skills>" in system
    assert "foo" in system
    assert any(t.name == "load_skill" for t in tools)
    assert any(t.name == "read" for t in tools)  # 原工具保留


def test_prepare_skills_empty_dirs_noop():
    existing = [_dummy_tool("read")]
    system, tools = prepare_skills([], "base", existing)
    assert system == "base"
    assert tools == existing


def test_prepare_skills_nonexistent_dir_noop(tmp_path):
    existing = [_dummy_tool("read")]
    system, tools = prepare_skills([tmp_path / "nope"], "base", existing)
    assert system == "base"
    assert tools == existing


def test_prepare_skills_str_system(tmp_path):
    skills = tmp_path / "skills"
    d = skills / "foo"; d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: foo\n---\n# foo\n", encoding="utf-8")
    system, _ = prepare_skills([skills], "abc", [])
    assert isinstance(system, str)
    assert system.startswith("abc")


def test_prepare_skills_list_system(tmp_path):
    skills = tmp_path / "skills"
    d = skills / "foo"; d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: foo\n---\n# foo\n", encoding="utf-8")
    base = [{"type": "text", "text": "a"}]
    system, _ = prepare_skills([skills], base, [])
    assert isinstance(system, list)
    assert system[0] == {"type": "text", "text": "a"}
    assert system[-1]["type"] == "text"
    assert "<skills>" in system[-1]["text"]


def test_prepare_skills_scan_exception_degrades(monkeypatch, tmp_path):
    from core.skills import loader as loader_mod

    def _boom(_dirs):
        raise RuntimeError("boom")

    monkeypatch.setattr(loader_mod.SkillLoader, "scan", _boom)
    existing = [_dummy_tool("read")]
    system, tools = prepare_skills([tmp_path], "base", existing)
    assert system == "base"  # 降级:不注入
    assert tools == existing
