from core.builtin_tools.readstate import FileReadState


def test_set_get_roundtrip():
    rs = FileReadState()
    rs.set("/a", "content", 100.0, 1, 10)
    rec = rs.get("/a")
    assert rec is not None
    assert rec.content == "content" and rec.mtime == 100.0
    assert rec.offset == 1 and rec.limit == 10


def test_is_unchanged_true_when_same_range_and_mtime():
    rs = FileReadState()
    rs.set("/a", "c", 100.0, 1, 10)
    assert rs.is_unchanged("/a", 1, 10, 100.0) is True


def test_is_unchanged_false_when_mtime_changed():
    rs = FileReadState()
    rs.set("/a", "c", 100.0, 1, 10)
    assert rs.is_unchanged("/a", 1, 10, 101.0) is False


def test_is_unchanged_false_when_no_record():
    rs = FileReadState()
    assert rs.is_unchanged("/a", 1, 10, 100.0) is False


def test_is_stale_true_when_modified_after_read():
    rs = FileReadState()
    rs.set("/a", "c", 100.0, 1, None)
    assert rs.is_stale("/a", 101.0) is True


def test_is_stale_false_when_not_modified():
    rs = FileReadState()
    rs.set("/a", "c", 100.0, 1, None)
    assert rs.is_stale("/a", 100.0) is False


def test_is_stale_false_when_never_read():
    """没读过的文件允许直接写(CC 行为)。"""
    rs = FileReadState()
    assert rs.is_stale("/a", 100.0) is False
