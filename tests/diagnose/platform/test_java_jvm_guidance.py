"""Java runtime guidance 测试 - TDD RED 阶段。"""
from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
from diagnose.platform_impl.java_jvm.guidance import build_java_jvm_reminder_text
from diagnose.platform_impl.java_jvm.profile import build_java_jvm_profile


def _profile(tmp_path, arts):
    case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path), artifacts=arts)
    return build_java_jvm_profile(case)


class TestGuidanceContent:
    def test_thread_dump_methodology_when_thread_dump(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")
        ]))
        # 方法论保留: BLOCKED≠deadlock
        assert "BLOCKED" in txt
        assert "deadlock" in txt.lower()

    def test_tda_section_omits_mcp_tool_names(self, tmp_path):
        (tmp_path / "td.txt").write_text("x")
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")
        ]))
        # MCP 工具描述由 tool 数组提供, guidance 不重复 (也不内嵌具体地址)
        for tool_name in ("parse_log", "check_deadlocks", "get_summary", "find_long_running", "absolute_path"):
            assert tool_name not in txt, f"{tool_name} 不应出现在 guidance"
        assert str((tmp_path / "td.txt").resolve()) not in txt

    def test_blocked_is_not_deadlock(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")
        ]))
        assert "BLOCKED" in txt
        assert "deadlock" in txt.lower()

    def test_heap_dump_methodology_when_hprof(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="hd", kind=ArtifactKind.HEAP_SNAPSHOT, path="hd.hprof")
        ]))
        assert "HPROF" in txt or "hprof" in txt.lower()
        # 方法论保留: retention 边界
        assert "retention" in txt.lower() or "retained" in txt.lower()
        # MCP 工具名不进 guidance
        for tool_name in ("memory-analyzer", "get_leak_suspects", "open_heap_dump", "get_dominator_tree"):
            assert tool_name not in txt

    def test_no_heap_section_when_no_hprof(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="hh", kind=ArtifactKind.MEMORY_SUMMARY, path="hh.txt")
        ]))
        # 无 heap dump -> 不输出 heap dump section
        assert "HPROF" not in txt and "hprof" not in txt.lower()

    def test_histogram_cannot_prove_heap_leak(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="hh", kind=ArtifactKind.MEMORY_SUMMARY, path="hh.txt")
        ]))
        assert "histogram" in txt.lower()
        assert "heap leak" in txt.lower()

    def test_source_section_when_source(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="s", kind=ArtifactKind.SOURCE, path="App.java")
        ]))
        assert "source" in txt.lower()

    def test_returns_plain_text_without_system_reminder_tag(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")
        ]))
        assert "<system-reminder>" not in txt
