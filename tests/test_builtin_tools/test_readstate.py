from core.file_state import FileState, FileStateCache


def _state(content: str = "content", timestamp: int = 100) -> FileState:
    return FileState(content=content, timestamp=timestamp, offset=1, limit=10)


def test_set_get_roundtrip():
    cache = FileStateCache()
    cache.set("/a", _state())

    record = cache.get("/a")

    assert record == _state()


def test_paths_are_normalized(tmp_path):
    cache = FileStateCache()
    path = tmp_path / "a.txt"
    cache.set(str(path), _state())

    assert cache.get(str(tmp_path / "." / "a.txt")) == _state()


def test_lru_evicts_oldest_entry():
    cache = FileStateCache(capacity=2)
    cache.set("/a", _state("a"))
    cache.set("/b", _state("b"))
    cache.get("/a")
    cache.set("/c", _state("c"))

    assert cache.get("/a") is not None
    assert cache.get("/b") is None
    assert cache.get("/c") is not None


def test_delete_removes_entry():
    cache = FileStateCache()
    cache.set("/a", _state())

    cache.delete("/a")

    assert cache.get("/a") is None
