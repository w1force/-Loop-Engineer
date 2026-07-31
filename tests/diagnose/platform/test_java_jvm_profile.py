# tests/diagnose/platform/test_java_jvm_profile.py
"""Java 证据包画像测试 - TDD RED 阶段。

Profile 不是解析器, 只回答: 有哪些证据、每类能回答什么、哪些不能、
Agent 调 MCP 该用哪些绝对路径。HPROF 不被读入内存。
"""
import pytest

from diagnose.errors import InvalidArtifactPathError
from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
from diagnose.platform_impl.java_jvm.profile import (
    JavaEvidenceProfile,
    build_java_jvm_profile,
)


def _art(aid: str, kind: ArtifactKind, path: str, **meta) -> ArtifactRef:
    return ArtifactRef(id=aid, kind=kind, path=path, metadata=meta)


def _case(root, artifacts) -> DiagnosisCase:
    return DiagnosisCase(
        id="c", platform_id="java-jvm", root_dir=str(root), artifacts=artifacts
    )


class TestArtifactClassification:
    def test_classifies_all_kinds(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(
                tmp_path,
                [
                    _art("td", ArtifactKind.THREAD_SNAPSHOT, "td.txt"),
                    _art("hd", ArtifactKind.HEAP_SNAPSHOT, "hd.hprof", format="hprof"),
                    _art("hh", ArtifactKind.MEMORY_SUMMARY, "hh.txt"),
                    _art("src", ArtifactKind.SOURCE, "App.java"),
                    _art("log", ArtifactKind.LOG, "app.log"),
                    _art("mf", ArtifactKind.BUILD_METADATA, "manifest.txt"),
                    _art("crash", ArtifactKind.RUNTIME_CRASH_REPORT, "hs_err.log"),
                ],
            )
        )
        assert profile.thread_dump_ids == ["td"]
        assert profile.heap_dump_ids == ["hd"]
        assert profile.heap_histogram_ids == ["hh"]
        assert profile.source_ids == ["src"]
        assert profile.log_ids == ["log"]
        assert profile.manifest_ids == ["mf"]
        assert profile.crash_report_ids == ["crash"]

    def test_absolute_path_resolved_from_root_dir(self, tmp_path):
        (tmp_path / "nested").mkdir()
        (tmp_path / "nested/td.txt").write_bytes(b"x")
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("td", ArtifactKind.THREAD_SNAPSHOT, "nested/td.txt")])
        )
        assert profile.artifacts[0].absolute_path == str((tmp_path / "nested/td.txt").resolve())
        assert profile.artifacts[0].relative_path == "nested/td.txt"

    def test_rejects_path_outside_root_when_called_directly(self, tmp_path):
        outside = tmp_path.parent / "outside-thread.txt"
        outside.write_text("x")
        case = _case(tmp_path, [_art("td", ArtifactKind.THREAD_SNAPSHOT, "../outside-thread.txt")])
        with pytest.raises(InvalidArtifactPathError, match="escapes root_dir"):
            build_java_jvm_profile(case)

    def test_rejects_absolute_path_when_called_directly(self, tmp_path):
        case = _case(tmp_path, [_art("x", ArtifactKind.THREAD_SNAPSHOT, "/abs/thread.txt")])
        with pytest.raises(InvalidArtifactPathError, match="must be relative"):
            build_java_jvm_profile(case)

    def test_format_taken_from_metadata(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("hd", ArtifactKind.HEAP_SNAPSHOT, "hd.hprof", format="hprof")])
        )
        assert profile.artifacts[0].format == "hprof"

    def test_size_bytes_copied_from_inspected_artifact(self, tmp_path):
        data = b"abcdef"
        art = ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt", size_bytes=len(data))
        profile = build_java_jvm_profile(_case(tmp_path, [art]))
        assert profile.artifacts[0].size_bytes == len(data)


class TestLimitations:
    def test_single_heap_snapshot_limitation(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("hd", ArtifactKind.HEAP_SNAPSHOT, "hd.hprof")])
        )
        assert any("growth" in m.lower() for m in profile.limitations)

    def test_no_heap_dump_limitation(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("hh", ArtifactKind.MEMORY_SUMMARY, "hh.txt")])
        )
        assert any("retention path" in m.lower() or "gc-root" in m.lower() for m in profile.limitations)

    def test_single_thread_dump_limitation(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("td", ArtifactKind.THREAD_SNAPSHOT, "td.txt")])
        )
        assert any("single thread" in m.lower() or "duration" in m.lower() for m in profile.limitations)

    def test_no_source_limitation(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("td", ArtifactKind.THREAD_SNAPSHOT, "td.txt")])
        )
        assert any("source" in m.lower() or "ownership" in m.lower() for m in profile.limitations)

    def test_empty_case_has_no_artifacts_but_lists_limitations(self, tmp_path):
        profile = build_java_jvm_profile(_case(tmp_path, []))
        assert profile.artifacts == []
        assert len(profile.limitations) > 0


class TestHprofNotRead:
    def test_hprof_content_not_read_into_memory(self, tmp_path, monkeypatch):
        """对 HEAP_SNAPSHOT 只识别, 不打开文件读内容。"""
        hprof = tmp_path / "hd.hprof"
        hprof.write_bytes(b"not text")

        opened = []
        real_open = open

        def spy_open(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)

        import builtins

        monkeypatch.setattr(builtins, "open", spy_open)
        build_java_jvm_profile(_case(tmp_path, [_art("hd", ArtifactKind.HEAP_SNAPSHOT, "hd.hprof")]))
        assert not any("hd.hprof" in p for p in opened)
