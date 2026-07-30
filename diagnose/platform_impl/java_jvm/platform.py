"""Java/JVM 占位诊断平台 (一期)

JavaJvmDiagnosticPlatform 是诊断内核落地的第一个具体平台, 一期仅占位:
- status = PLANNED: 已知该平台, 但尚无可执行分析能力;
- capabilities / actions 为空: 不承诺任何 action;
- taxonomy 只含 unknown: 不提前固化未实现的根因类别;
- inspect_case: 只做轻量、确定性的工件识别 (路径安全 + 流式 size/sha256);
- seed_hypotheses: 返回空列表;
- 不提供 execute: 基础 DiagnosticPlatform Protocol 无 execute 成员。

路径安全 (来自 plan Task 5 + brief):
- path 必须相对 case.root_dir, 绝对路径与 .. 越界一律抛 InvalidArtifactPathError;
- 文件不存在抛 ArtifactNotFoundError, 不静默忽略;
- 流式分块读取算 size_bytes 与 sha256, 不将大文件全文装入内存。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from diagnose.errors import ArtifactNotFoundError, InvalidArtifactPathError
from diagnose.model import (
    ArtifactKind,
    ArtifactRef,
    DiagnosisCase,
    DiagnosticPlatformDescriptor,
    DiagnosticTaxonomy,
    Hypothesis,
    PlatformStatus,
)

# 流式读取的块大小: 64KB, 平衡 IO 次数与内存占用。
_CHUNK_SIZE = 64 * 1024

# 该平台已知会处理的工件种类 (不含 UNKNOWN: UNKNOWN 是兜底, 不在平台声明里)。
_JAVA_ARTIFACT_KINDS: set[ArtifactKind] = {
    ArtifactKind.LOG,
    ArtifactKind.SOURCE,
    ArtifactKind.BUILD_METADATA,
    ArtifactKind.THREAD_SNAPSHOT,
    ArtifactKind.HEAP_SNAPSHOT,
    ArtifactKind.MEMORY_SUMMARY,
    ArtifactKind.RUNTIME_CRASH_REPORT,
}


def _build_descriptor() -> DiagnosticPlatformDescriptor:
    """构造 Java/JVM 平台描述符。

    独立为模块函数, 便于单测与将来扩展; descriptor 在平台实例上缓存, 避免重复构造。
    """
    return DiagnosticPlatformDescriptor(
        id="java-jvm",
        display_name="Java/JVM",
        status=PlatformStatus.PLANNED,
        description=(
            "Java/JVM service offline evidence-bundle diagnosis platform "
            "(phase 1: placeholder, no executable analysis capability)"
        ),
        taxonomy=DiagnosticTaxonomy(categories={}, unknown_category="unknown"),
        artifact_kinds=set(_JAVA_ARTIFACT_KINDS),
        capabilities=[],
        actions=[],
    )


class JavaJvmDiagnosticPlatform:
    """Java/JVM 占位诊断平台

    一期不提供可执行分析能力, 仅做:
    1. 声明平台描述符 (PLANNED, 无 capability/action, taxonomy 仅 unknown);
    2. 对 case.artifacts 做轻量确定性识别 (inspect_case);
    3. 返回空假设列表。

    不实现 execute, 不依赖 core/。
    """

    def __init__(self) -> None:
        # 缓存 descriptor, 避免每次访问都重建 pydantic 模型。
        self._descriptor: DiagnosticPlatformDescriptor = _build_descriptor()

    @property
    def descriptor(self) -> DiagnosticPlatformDescriptor:
        """返回平台描述符 (含能力、taxonomy、动作声明)。"""
        return self._descriptor

    def inspect_case(self, case: DiagnosisCase) -> list[ArtifactRef]:
        """对 case.artifacts 做轻量、确定性的工件识别。

        对每个 ArtifactRef:
        1. 校验 path 相对 case.root_dir, 绝对路径/越界抛 InvalidArtifactPathError;
        2. 文件必须存在, 否则抛 ArtifactNotFoundError;
        3. 流式分块累加 size_bytes 并计算 sha256;
        4. 原样保留调用方声明的 kind (不以后缀猜测)。
        """
        root = Path(case.root_dir).resolve()
        resolved: list[ArtifactRef] = []
        for art in case.artifacts:
            full = self._resolve_within_root(root, art.path)
            size_bytes, sha256 = self._hash_and_size(full)
            resolved.append(
                art.model_copy(
                    update={
                        "size_bytes": size_bytes,
                        "sha256": sha256,
                    }
                )
            )
        return resolved

    def seed_hypotheses(self, case: DiagnosisCase) -> list[Hypothesis]:
        """返回空假设列表。

        一期 Java 占位不产出根因假设, 留给后续分析器接入。
        """
        return []

    # ------------------------------------------------------------------ #
    # 内部: 路径安全解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_within_root(root: Path, declared_path: str) -> Path:
        """把 declared_path 解析到 root 之内, 越界/绝对路径/缺失都抛领域错误。

        - 绝对路径: Path(declared_path).is_absolute() 为真时, 即使用 / 拼到 root
          也会被右操作数覆盖, 必须在拼接前显式拒绝。
        - 越界: (root / declared_path).resolve() 后若不在 root 内, 抛
          InvalidArtifactPathError (覆盖 .. 越界与符号链接逃逸)。
        - 缺失: 文件不存在抛 ArtifactNotFoundError。
        """
        if Path(declared_path).is_absolute():
            raise InvalidArtifactPathError(
                f"artifact path must be relative to root_dir, "
                f"got absolute path: {declared_path!r}"
            )

        full = (root / declared_path).resolve()
        if not full.is_relative_to(root):
            raise InvalidArtifactPathError(
                f"artifact path escapes root_dir: {declared_path!r} "
                f"-> {full!s}"
            )
        if not full.exists():
            raise ArtifactNotFoundError(
                f"artifact file not found under root_dir: {declared_path!r}"
            )
        if not full.is_file():
            raise ArtifactNotFoundError(
                f"artifact path is not a regular file: {declared_path!r}"
            )
        return full

    # ------------------------------------------------------------------ #
    # 内部: 流式 hash / size
    # ------------------------------------------------------------------ #
    @staticmethod
    def _hash_and_size(path: Path) -> tuple[int, str]:
        """流式分块读取, 返回 (size_bytes, sha256_hex)。

        按 _CHUNK_SIZE 分块累加, 不一次性 read() 整个文件, 避免大堆转储撑爆内存。
        """
        size = 0
        hasher = hashlib.sha256()
        with path.open("rb") as fp:
            while True:
                chunk = fp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                hasher.update(chunk)
        return size, hasher.hexdigest()
