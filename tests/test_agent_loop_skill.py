"""Task 4: build_agent_state + build_system_prompt 测试。

Task 4 退役 prepare_skills 后,skill 注入逻辑分三段:
- build_agent_state(config):scan skills + 新建 FileStateCache + 设 cwd + 迁移 initial_messages
- build_system_prompt(agent_state, config):只追加环境与按需加载说明
- inject_skill_listing(agent_state):在对话阶段仅注入 name/description
"""
from pathlib import Path

from core.agent_loop import AgentConfig, build_agent_state, build_system_prompt
from core.loop.phases.skill_listing import inject_skill_listing
from core.types import AgentState, SkillMeta, UserMessage


class _NoopProvider:
    """空 provider stub:build_agent_state/build_system_prompt 不触达 provider。"""

    def stream(self, **kwargs):  # pragma: no cover - 仅满足 Provider 协议
        raise NotImplementedError

    def count_tokens(self, messages) -> int:  # pragma: no cover
        return 0


def test_build_agent_state_scans_skills(tmp_path):
    skills = tmp_path / "skills"
    d = skills / "foo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: foo\n---\n# foo\n", encoding="utf-8")
    cfg = AgentConfig(
        provider=_NoopProvider(), system="base", model="m", max_tokens=100,
        skill_dirs=[str(skills)], cwd=str(tmp_path),
    )
    astate = build_agent_state(cfg)
    assert len(astate.skills) == 1 and astate.skills[0].name == "foo"
    assert astate.cwd == str(tmp_path)
    assert astate.messages == []


def test_build_agent_state_scan_failure_degrades(tmp_path):
    cfg = AgentConfig(
        provider=_NoopProvider(), system="base", model="m", max_tokens=100,
        skill_dirs=[str(tmp_path / "nope")],
    )
    astate = build_agent_state(cfg)
    assert astate.skills == []   # 降级


def test_build_agent_state_migrates_initial_messages(tmp_path):
    """initial_messages 迁移到 agent_state.messages(解决 Task 2 死字段 concern)。"""
    msg = UserMessage(content="seed")
    cfg = AgentConfig(
        provider=_NoopProvider(), system="base", model="m", max_tokens=100,
        initial_messages=[msg],
    )
    astate = build_agent_state(cfg)
    assert astate.messages == [msg]


def test_build_agent_state_has_fresh_file_read_state(tmp_path):
    cfg = AgentConfig(provider=_NoopProvider(), system="base", model="m", max_tokens=100)
    astate = build_agent_state(cfg)
    # FileStateCache 默认空:read 没记录 → get 返回 None
    assert astate.file_read_state.get("/nope") is None


def test_build_system_prompt_empty_skills():
    astate = AgentState(skills=[])
    cfg = AgentConfig(provider=_NoopProvider(), system="base", model="m", max_tokens=100)
    out = build_system_prompt(astate, cfg)
    assert isinstance(out, str) and out.startswith("base")
    assert f"Primary working directory: {cfg.cwd}" in out
    assert "Bash commands start in this directory" in out
    assert "<system-reminder>" in out and "或其他标签" in out


def test_build_system_prompt_str():
    m = SkillMeta(name="foo", description="d", skill_dir=Path("/x"), skill_md=Path("/x/SKILL.md"))
    astate = AgentState(skills=[m])
    cfg = AgentConfig(provider=_NoopProvider(), system="base", model="m", max_tokens=100)
    out = build_system_prompt(astate, cfg)
    assert isinstance(out, str) and out.startswith("base")
    assert "Load_Skill" in out
    assert "<skills>" not in out and "foo: d" not in out
    inject_skill_listing(astate)
    assert "- foo: d" in str(astate.messages[-1].content)


def test_build_system_prompt_list():
    m = SkillMeta(name="foo", description="d", skill_dir=Path("/x"), skill_md=Path("/x/SKILL.md"))
    astate = AgentState(skills=[m])
    cfg = AgentConfig(
        provider=_NoopProvider(), system=[{"type": "text", "text": "a"}], model="m", max_tokens=100,
    )
    out = build_system_prompt(astate, cfg)
    assert isinstance(out, list) and out[0] == {"type": "text", "text": "a"}
    assert "Load_Skill" in out[-1]["text"]
    assert "foo: d" not in out[-1]["text"]
    inject_skill_listing(astate)
    assert "- foo: d" in str(astate.messages[-1].content)


def test_build_system_prompt_list_includes_agent_cwd():
    astate = AgentState(skills=[], cwd="/workspace/project")
    cfg = AgentConfig(
        provider=_NoopProvider(), system=[{"type": "text", "text": "a"}], model="m", max_tokens=100,
    )
    out = build_system_prompt(astate, cfg)
    assert isinstance(out, list)
    assert "Primary working directory: /workspace/project" in out[-1]["text"]
    assert "Bash commands start in this directory" in out[-1]["text"]


def test_build_system_prompt_empty_skills_list_passthrough():
    """空 skills + list[dict] system 也追加通用 system-reminder 说明。"""
    astate = AgentState(skills=[])
    base = [{"type": "text", "text": "a"}]
    cfg = AgentConfig(provider=_NoopProvider(), system=base, model="m", max_tokens=100)
    out = build_system_prompt(astate, cfg)
    assert isinstance(out, list) and out[0] == base[0]
    assert "<system-reminder>" in out[-1]["text"]


def test_skill_listing_description_whitespace_collapsed():
    """description 多行空白压缩成单行。"""
    m = SkillMeta(
        name="foo",
        description="line one\n  line two",
        skill_dir=Path("/x"),
        skill_md=Path("/x/SKILL.md"),
    )
    astate = AgentState(skills=[m])
    inject_skill_listing(astate)
    assert "line one line two" in str(astate.messages[-1].content)
