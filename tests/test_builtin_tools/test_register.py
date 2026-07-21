"""Task 7/Task 3: builtin_tools() 工厂注册测试。

Task 3 起工厂无参,返回 5 个 Tool(含 load_skill),func 从 ctx.agent_state 取。
"""
from core.registry import get_tools
from core.tools import Tool


def test_builtin_tools_returns_five():
    tools = get_tools(False)
    names = [t.name for t in tools]
    assert sorted(names) == ["Glob", "Grep", "Load_Skill", "Read", "Write"]
    for t in tools:
        assert isinstance(t, Tool)


def test_builtin_tools_repeatable():
    """registry._BASE_TOOLS 是模块级单例;get_tools() 浅拷贝 list 但 Tool 元素共享,两次返回同一批对象。"""
    a = get_tools(False)
    b = get_tools(False)
    assert [t.name for t in a] == [t.name for t in b]
    a_by_name = {t.name: t for t in a}
    b_by_name = {t.name: t for t in b}
    # registry 单例:两次 get_tools 返回同一 Tool 对象
    assert a_by_name["Load_Skill"] is b_by_name["Load_Skill"]
    assert a_by_name["Read"] is b_by_name["Read"]


def test_builtin_tools_read_write_share_agent_state_via_ctx():
    """read/write 不再闭包捕获 read_state;Task 3 起从 ctx.agent_state 取。
    同一 agent_state → read 记录后 write 在同 agent_state 上能看到(集成测在 test_read/test_write 覆盖)。
    此处仅校验 read/write 的 func 不带 closure(已退场)。"""
    tools = {t.name: t for t in get_tools(False)}
    # 闭包退场: func.__closure__ 应为 None(不再捕获 read_state/cwd)
    assert tools["Read"].func.__closure__ is None
    assert tools["Write"].func.__closure__ is None
    assert tools["Glob"].func.__closure__ is None
    assert tools["Grep"].func.__closure__ is None
