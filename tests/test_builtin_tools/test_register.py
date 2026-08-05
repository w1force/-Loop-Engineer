"""Task 7/Task 3: builtin_tools() 工厂注册测试。

工厂无参,返回内置 Tool 集合(Read/Write/Edit/Bash/Glob/Grep/LSP/Load_Skill),
func 从 ctx.agent_state 取。新增工具时更新下方期望列表即可。
"""
from core.registry import get_tools
from core.tools import Tool

_EXPECTED_TOOL_NAMES = {"Read", "Write", "Edit", "Bash", "Glob", "Grep", "LSP", "Load_Skill"}


def test_builtin_tools_returns_expected_set():
    tools = get_tools(False)
    names = {t.name for t in tools}
    assert names == _EXPECTED_TOOL_NAMES
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
    assert a_by_name["LSP"] is b_by_name["LSP"]
    assert a_by_name["Read"] is b_by_name["Read"]


def test_search_and_lsp_descriptions_express_soft_division():
    tools = {tool.name: tool for tool in get_tools(False)}
    assert "业务逻辑入口" in tools["Grep"].description
    assert "尚不知道准确路径" in tools["Glob"].description
    assert "追踪定义、引用、接口实现" in tools["LSP"].description
    assert "LSP" not in tools["Grep"].description
    assert "LSP" not in tools["Glob"].description
    assert "Grep" not in tools["LSP"].description
    assert "Glob" not in tools["LSP"].description


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
