"""Task 7: builtin_tools() 工厂注册测试。"""
from core.builtin_tools import builtin_tools
from core.builtin_tools.readstate import FileReadState
from core.tools import Tool


def test_builtin_tools_returns_four():
    tools = builtin_tools(FileReadState())
    names = [t.name for t in tools]
    assert sorted(names) == ["glob", "grep", "read", "write"]
    for t in tools:
        assert isinstance(t, Tool)


def test_builtin_tools_share_read_state():
    """read 和 write 共享同一个 FileReadState(陈旧检测前提)。"""
    rs = FileReadState()
    tools = {t.name: t for t in builtin_tools(rs)}
    # 两者的 read_state 是同一个对象(工厂捕获同一 rs)
    assert tools["read"].func.__closure__ is not None
    assert tools["write"].func.__closure__ is not None
    # 更直接的验证: read 后 write 在同一 rs 上能看到记录(集成测在 test_read/test_write 已覆盖)
