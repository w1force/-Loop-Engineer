# diagnose/platform_impl/java_jvm/profile.py
"""Java 证据包画像。

Profile 不是解析器, 也不是诊断器。它只回答: 本 case 里有哪些证据、
每类证据能回答什么问题、哪些问题不能回答、Agent 调用 MCP 时应使用哪些绝对路径。

绝对路径必须从 case.artifacts (已经过 platform.inspect_case 校验) 计算,
不从 Agent 输入或 manifest 内的采集机路径采信。HPROF 不被读入内存。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from diagnose.errors import InvalidArtifactPathError
from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase

_LIMIT_SINGLE_HEAP = "single heap snapshot does not establish growth over time"
_LIMIT_NO_HEAP_DUMP = "retention path / GC-root analysis is unavailable (no heap dump)"
_LIMIT_SINGLE_THREAD = "single thread snapshot cannot establish persistent CPU impact or duration"
_LIMIT_NO_SOURCE = "runtime observations cannot be fully traced back to application ownership (no source)"


@dataclass(frozen=True)
class JavaArtifactProfile:
    """单个 Java artifact 的画像条目。"""

    artifact_id: str
    kind: ArtifactKind
    relative_path: str
    absolute_path: str
    format: str | None
    size_bytes: int | None


@dataclass(frozen=True)
class JavaEvidenceProfile:
    """整个 Java case 的证据包画像。"""

    artifacts: list[JavaArtifactProfile]
    thread_dump_ids: list[str]
    heap_dump_ids: list[str]
    heap_histogram_ids: list[str]
    source_ids: list[str]
    log_ids: list[str]
    manifest_ids: list[str]
    crash_report_ids: list[str]
    limitations: list[str] = field(default_factory=list)


def _profile_artifact(root: Path, art: ArtifactRef) -> JavaArtifactProfile:
    declared = Path(art.path)
    if declared.is_absolute():
        raise InvalidArtifactPathError(
            f"artifact path must be relative to root_dir: {art.path!r}"
        )
    absolute = (root / declared).resolve()
    if not absolute.is_relative_to(root):
        raise InvalidArtifactPathError(f"artifact path escapes root_dir: {art.path!r}")
    return JavaArtifactProfile(
        artifact_id=art.id,
        kind=art.kind,
        relative_path=art.path,
        absolute_path=str(absolute),
        format=art.metadata.get("format") if isinstance(art.metadata, dict) else None,
        size_bytes=art.size_bytes,
    )


def _ids_by_kind(profiled: list[JavaArtifactProfile], kind: ArtifactKind) -> list[str]:
    return [p.artifact_id for p in profiled if p.kind == kind]


def _build_limitations(profile: "JavaEvidenceProfile") -> list[str]:
    limits: list[str] = []
    if len(profile.heap_dump_ids) == 1:
        limits.append(_LIMIT_SINGLE_HEAP)
    if not profile.heap_dump_ids:
        limits.append(_LIMIT_NO_HEAP_DUMP)
    if len(profile.thread_dump_ids) == 1:
        limits.append(_LIMIT_SINGLE_THREAD)
    if not profile.source_ids:
        limits.append(_LIMIT_NO_SOURCE)
    return limits


def build_java_jvm_profile(case: DiagnosisCase) -> JavaEvidenceProfile:
    """基于 case.artifacts 建立 Java 证据包画像 (纯函数, 不读文件内容)。"""
    root = Path(case.root_dir).resolve()
    profiled = [_profile_artifact(root, art) for art in case.artifacts]
    thread_dump_ids = _ids_by_kind(profiled, ArtifactKind.THREAD_SNAPSHOT)
    heap_dump_ids = _ids_by_kind(profiled, ArtifactKind.HEAP_SNAPSHOT)
    heap_histogram_ids = _ids_by_kind(profiled, ArtifactKind.MEMORY_SUMMARY)
    source_ids = _ids_by_kind(profiled, ArtifactKind.SOURCE)
    log_ids = _ids_by_kind(profiled, ArtifactKind.LOG)
    manifest_ids = _ids_by_kind(profiled, ArtifactKind.BUILD_METADATA)
    crash_report_ids = _ids_by_kind(profiled, ArtifactKind.RUNTIME_CRASH_REPORT)
    partial = JavaEvidenceProfile(
        artifacts=profiled,
        thread_dump_ids=thread_dump_ids,
        heap_dump_ids=heap_dump_ids,
        heap_histogram_ids=heap_histogram_ids,
        source_ids=source_ids,
        log_ids=log_ids,
        manifest_ids=manifest_ids,
        crash_report_ids=crash_report_ids,
    )
    return replace(partial, limitations=_build_limitations(partial))
