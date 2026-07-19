"""Task 1: SkillLoader.scan + frontmatter 解析测试。"""
from pathlib import Path

from core.skills import SkillLoader, SkillMeta


def _make_skill(root: Path, name: str, body: str) -> Path:
    """在 root 下造一个 skill 目录 + SKILL.md。"""
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def test_scan_basic_sorted(tmp_path):
    skills = tmp_path / "skills"
    _make_skill(skills, "foo", "---\ndescription: foo skill\n---\n# foo\n")
    _make_skill(skills, "bar", "---\ndescription: bar skill\n---\n# bar\n")
    metas = SkillLoader.scan([skills])
    assert [m.name for m in metas] == ["bar", "foo"]  # 按 name 排序


def test_scan_missing_description_skipped(tmp_path):
    skills = tmp_path / "skills"
    _make_skill(skills, "foo", "---\nname: foo\n---\n# foo\n")  # 无 description
    assert SkillLoader.scan([skills]) == []


def test_scan_yaml_corrupt_skipped(tmp_path):
    skills = tmp_path / "skills"
    _make_skill(skills, "foo", "---\ndescription: [unclosed\n---\n# foo\n")
    assert SkillLoader.scan([skills]) == []


def test_scan_unknown_field_tolerated(tmp_path):
    skills = tmp_path / "skills"
    body = "---\ndescription: foo\nwhen_to_use: bar\nallowed-tools: read\n---\n# foo\n"
    _make_skill(skills, "foo", body)
    metas = SkillLoader.scan([skills])
    assert len(metas) == 1 and metas[0].name == "foo"


def test_scan_nonexistent_dir_skipped(tmp_path):
    assert SkillLoader.scan([tmp_path / "nope"]) == []


def test_scan_duplicate_name_later_overrides(tmp_path):
    a = tmp_path / "a"; b = tmp_path / "b"
    _make_skill(a, "foo", "---\ndescription: from-a\n---\n")
    _make_skill(b, "foo", "---\ndescription: from-b\n---\n")
    metas = SkillLoader.scan([a, b])
    assert len(metas) == 1 and metas[0].description == "from-b"


def test_scan_no_skill_md_skipped(tmp_path):
    skills = tmp_path / "skills"
    (skills / "notaskill").mkdir(parents=True)
    assert SkillLoader.scan([skills]) == []


def test_scan_empty_dirs():
    assert SkillLoader.scan([]) == []


def test_skill_meta_fields(tmp_path):
    skills = tmp_path / "skills"
    _make_skill(skills, "foo", "---\ndescription: foo skill\n---\n# foo\n")
    m = SkillLoader.scan([skills])[0]
    assert isinstance(m, SkillMeta)
    assert m.name == "foo"
    assert m.description == "foo skill"
    assert m.skill_md == skills / "foo" / "SKILL.md"
    assert m.skill_dir == skills / "foo"


def test_scan_multiline_description(tmp_path):
    skills = tmp_path / "skills"
    body = "---\ndescription: |\n  line one\n  line two\n---\n# foo\n"
    _make_skill(skills, "foo", body)
    desc = SkillLoader.scan([skills])[0].description
    assert "line one" in desc and "line two" in desc


# --- Task 2: render_catalog + append_catalog ---

from core.skills import render_catalog, append_catalog  # noqa: E402


def test_render_catalog_format(tmp_path):
    skills = tmp_path / "skills"
    _make_skill(skills, "foo", "---\ndescription: foo skill\n---\n")
    out = render_catalog(SkillLoader.scan([skills]))
    assert "<skills>" in out and "</skills>" in out
    assert "name: foo" in out
    assert "foo skill" in out
    assert "load_skill(name)" in out


def test_render_catalog_empty():
    assert render_catalog([]) == ""


def test_render_catalog_multiline_description(tmp_path):
    skills = tmp_path / "skills"
    _make_skill(skills, "foo", "---\ndescription: |\n  line one\n  line two\n---\n")
    out = render_catalog(SkillLoader.scan([skills]))
    assert "line one" in out and "line two" in out


def test_append_catalog_str():
    assert append_catalog("abc", "X") == "abcX"


def test_append_catalog_list():
    base = [{"type": "text", "text": "a"}]
    out = append_catalog(base, "X")
    assert isinstance(out, list)
    assert out[0] == {"type": "text", "text": "a"}
    assert out[1] == {"type": "text", "text": "X"}
