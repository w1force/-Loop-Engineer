"""Task 7/Task 3: builtin_tools() 工厂注册测试。

Task 3 起工厂无参,返回 5 个 Tool(含 load_skill),func 从 ctx.agent_state 取。
"""
from core.builtin_tools import builtin_tools
from core.tools import Tool


def test_builtin_tools_returns_five():
    tools = builtin_tools()
    names = [t.name for t in tools]
    assert sorted(names) == ["glob", "grep", "load_skill", "read", "write"]
    for t in tools:
        assert isinstance(t, Tool)


def test_builtin_tools_repeatable():
    """工厂无状态: 两次调用各自得到独立的 Tool 实例(load_skill_tool 是常量,其余 4 个每次新建)。"""
    a = builtin_tools()
    b = builtin_tools()
    assert [t.name for t in a] == [t.name for t in b]
    # load_skill_tool 是模块级常量,两次返回同一对象;其余 4 个工厂每次 new 一个
    a_by_name = {t.name: t for t in a}
    b_by_name = {t.name: t for t in b}
    assert a_by_name["load_skill"] is b_by_name["load_skill"]
    assert a_by_name["read"] is not b_by_name["read"]


def test_builtin_tools_read_write_share_agent_state_via_ctx():
    """read/write 不再闭包捕获 read_state;Task 3 起从 ctx.agent_state 取。
    同一 agent_state → read 记录后 write 在同 agent_state 上能看到(集成测在 test_read/test_write 覆盖)。
    此处仅校验 read/write 的 func 不带 closure(已退场)。"""
    tools = {t.name: t for t in builtin_tools()}
    # 闭包退场: func.__closure__ 应为 None(不再捕获 read_state/cwd)
    assert tools["read"].func.__closure__ is None
    assert tools["write"].func.__closure__ is None
    assert tools["glob"].func.__closure__ is None
    assert tools["grep"].func.__closure__ is None
