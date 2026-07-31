"""Java/JVM runtime profile 平台测试

锁定 JavaJvmDiagnosticPlatform 的 descriptor 字段、inspect_case 路径安全与流式
hash 行为、builtin_platform_registry 工厂的显式语义, 以及 AVAILABLE runtime
profile 下的 taxonomy / seed_hypotheses / build_agent_guidance 行为。

覆盖 (对应 brief 验收):
- descriptor: status=AVAILABLE, capabilities=[], actions=[], taxonomy 含 8 类
  runtime 根因候选, artifact_kinds 含 7 种 (不含 UNKNOWN)。
- inspect_case 路径安全: 绝对路径/.. 越界 -> InvalidArtifactPathError (非静默忽略)。
- inspect_case 文件不存在 -> ArtifactNotFoundError。
- inspect_case 流式 hash: 分块 (>64KB 文件触发多次 update), size/sha256 正确,
  且不全量 read (用 hashlib.sha256.update 调用次数作为可观测副作用)。
- inspect_case 保留声明 kind, 不以后缀猜测。
- seed_hypotheses: 依据 artifact kind 生成 PENDING 假设 (可能非空)。
- build_agent_guidance: 返回 Java runtime 提示正文 (无 <system-reminder> 标签)。
- builtin_platform_registry: 显式工厂, import 不触发注册, 每次返回新实例。
- 集成: AVAILABLE Java case 经 create_diagnosis_session -> INCONCLUSIVE (无证据)。
- 无 execute 方法、无 NotImplementedError。
"""

import hashlib

import pytest

from diagnose.errors import ArtifactNotFoundError, InvalidArtifactPathError
from diagnose.model import (
    ArtifactKind,
    ArtifactRef,
    DiagnosisCase,
    DiagnosisStatus,
    HypothesisStatus,
    PlatformStatus,
)
from diagnose.platform import DiagnosticPlatform
from diagnose.platform_impl import builtin_platform_registry
from diagnose.platform_impl.java_jvm.platform import JavaJvmDiagnosticPlatform
from diagnose.registry import PlatformRegistry

# 所有合法 artifact kind (不含 UNKNOWN), 与 descriptor.artifact_kinds 对齐。
_ALL_KINDS_NO_UNKNOWN = {
    ArtifactKind.LOG,
    ArtifactKind.SOURCE,
    ArtifactKind.BUILD_METADATA,
    ArtifactKind.THREAD_SNAPSHOT,
    ArtifactKind.HEAP_SNAPSHOT,
    ArtifactKind.MEMORY_SUMMARY,
    ArtifactKind.RUNTIME_CRASH_REPORT,
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# 导入与协议满足
# --------------------------------------------------------------------------- #
class TestImportAndProtocol:
    def test_platform_is_importable_and_instantiable(self):
        platform = JavaJvmDiagnosticPlatform()
        assert platform is not None

    def test_platform_satisfies_protocol(self):
        platform = JavaJvmDiagnosticPlatform()
        # runtime_checkable Protocol: 缺方法会 False, 能区分 "未实现" 与 "实现"。
        assert isinstance(platform, DiagnosticPlatform)

    def test_platform_has_no_execute_method(self):
        """一期占位不提供 execute (基础 Protocol 无此成员)。"""
        platform = JavaJvmDiagnosticPlatform()
        assert not hasattr(platform, "execute")


# --------------------------------------------------------------------------- #
# descriptor
# --------------------------------------------------------------------------- #
class TestDescriptor:
    def test_id_is_java_jvm(self):
        assert JavaJvmDiagnosticPlatform().descriptor.id == "java-jvm"

    def test_status_is_available(self):
        assert JavaJvmDiagnosticPlatform().descriptor.status == PlatformStatus.AVAILABLE

    def test_display_name_and_description_non_empty(self):
        d = JavaJvmDiagnosticPlatform().descriptor
        assert d.display_name
        assert d.description

    def test_capabilities_empty(self):
        assert JavaJvmDiagnosticPlatform().descriptor.capabilities == []

    def test_actions_empty(self):
        assert JavaJvmDiagnosticPlatform().descriptor.actions == []

    def test_taxonomy_contains_runtime_categories(self):
        cats = JavaJvmDiagnosticPlatform().descriptor.taxonomy.categories
        # 8 类 root-cause 候选
        for key in ("deadlock", "lock_contention", "memory_retention",
                    "heap_leak", "cpu_hotspot", "thread_starvation",
                    "runtime_crash", "inconclusive"):
            assert key in cats
        # unknown_category 保持 "unknown", 与 DiagnosisResult.root_cause_category 缺省一致。
        assert JavaJvmDiagnosticPlatform().descriptor.taxonomy.unknown_category == "unknown"

    def test_artifact_kinds_exclude_unknown(self):
        kinds = JavaJvmDiagnosticPlatform().descriptor.artifact_kinds
        # 必须含 7 种声明 kind, 且不含 UNKNOWN (UNKNOWN 是兜底, 不在平台声明里)。
        assert ArtifactKind.UNKNOWN not in kinds
        assert kinds == _ALL_KINDS_NO_UNKNOWN

    def test_descriptor_is_stable_across_reads(self):
        """多次读 descriptor, id 不变 (区分 "返回乱值" 与 "实现正确")。"""
        platform = JavaJvmDiagnosticPlatform()
        assert platform.descriptor.id == platform.descriptor.id == "java-jvm"


# --------------------------------------------------------------------------- #
# inspect_case: 正常路径 + 流式 hash
# --------------------------------------------------------------------------- #
class TestInspectCaseHappyPath:
    def _case(self, root, artifacts) -> DiagnosisCase:
        return DiagnosisCase(
            id="case-jvm-1",
            platform_id="java-jvm",
            root_dir=str(root),
            artifacts=artifacts,
        )

    def test_fills_size_and_sha256(self, tmp_path):
        data = b"java hot spot crash dump\n"
        (tmp_path / "hs_err.log").write_bytes(data)
        art = ArtifactRef(id="a1", kind=ArtifactKind.LOG, path="hs_err.log")

        out = JavaJvmDiagnosticPlatform().inspect_case(self._case(tmp_path, [art]))

        assert len(out) == 1
        filled = out[0]
        # 区分 "未填" (None) / "填错" / "填对"。
        assert filled.size_bytes == len(data)
        assert filled.sha256 == _sha256_bytes(data)
        assert filled.sha256 is not None and len(filled.sha256) == 64

    def test_preserves_declared_kind_regardless_of_suffix(self, tmp_path):
        """不以后缀猜测 kind: .log 文件声明 HEAP_SNAPSHOT -> 仍是 HEAP_SNAPSHOT。"""
        (tmp_path / "dump.log").write_bytes(b"x")
        art = ArtifactRef(
            id="a1", kind=ArtifactKind.HEAP_SNAPSHOT, path="dump.log"
        )
        out = JavaJvmDiagnosticPlatform().inspect_case(self._case(tmp_path, [art]))
        assert out[0].kind == ArtifactKind.HEAP_SNAPSHOT

    def test_preserves_id_and_path(self, tmp_path):
        (tmp_path / "app.log").write_bytes(b"hi")
        art = ArtifactRef(id="a1", kind=ArtifactKind.LOG, path="app.log")
        out = JavaJvmDiagnosticPlatform().inspect_case(self._case(tmp_path, [art]))
        assert out[0].id == "a1"
        assert out[0].path == "app.log"

    def test_multiple_artifacts_order_preserved(self, tmp_path):
        (tmp_path / "a.log").write_bytes(b"a")
        (tmp_path / "b.log").write_bytes(b"bb")
        arts = [
            ArtifactRef(id="a1", kind=ArtifactKind.LOG, path="a.log"),
            ArtifactRef(id="a2", kind=ArtifactKind.SOURCE, path="b.log"),
        ]
        out = JavaJvmDiagnosticPlatform().inspect_case(self._case(tmp_path, arts))
        assert [a.id for a in out] == ["a1", "a2"]

    def test_sha256_matches_independent_hash(self, tmp_path):
        data = bytes(range(256)) * 4  # 1024 bytes
        (tmp_path / "big.bin").write_bytes(data)
        art = ArtifactRef(id="a1", kind=ArtifactKind.HEAP_SNAPSHOT, path="big.bin")
        out = JavaJvmDiagnosticPlatform().inspect_case(self._case(tmp_path, [art]))
        assert out[0].sha256 == hashlib.sha256(data).hexdigest()
        assert out[0].size_bytes == len(data)

    def test_nested_relative_path_allowed(self, tmp_path):
        nested = tmp_path / "logs" / "app"
        nested.mkdir(parents=True)
        (nested / "trace.log").write_bytes(b"trace")
        art = ArtifactRef(
            id="a1", kind=ArtifactKind.LOG, path="logs/app/trace.log"
        )
        out = JavaJvmDiagnosticPlatform().inspect_case(self._case(tmp_path, [art]))
        assert out[0].size_bytes == 5


# --------------------------------------------------------------------------- #
# inspect_case: 流式分块 (可观测副作用)
# --------------------------------------------------------------------------- #
class TestInspectCaseStreamingHash:
    """用 hashlib.sha256.update 的调用次数作为 "是否分块" 的可观测证据。

    若实现一次性 read 整个文件再 update, 调用次数恒为 1;
    若分块, 对 >chunk_size 的文件, update 应被调用 >1 次, 且每次都不超过 chunk。
    """

    def _case(self, root, artifacts) -> DiagnosisCase:
        return DiagnosisCase(
            id="case-jvm-stream",
            platform_id="java-jvm",
            root_dir=str(root),
            artifacts=artifacts,
        )

    def test_large_file_hashes_in_multiple_chunks(self, tmp_path, monkeypatch):
        # 默认 64KB chunk: 写 150KB 文件, 至少应分 3 块。
        payload = b"j" * (150 * 1024)
        (tmp_path / "heap.hprof").write_bytes(payload)
        art = ArtifactRef(
            id="a1", kind=ArtifactKind.HEAP_SNAPSHOT, path="heap.hprof"
        )

        update_sizes: list[int] = []
        original_sha256 = hashlib.sha256

        class SpySha256:
            def __init__(self) -> None:
                self._real = original_sha256()

            def update(self, data: bytes) -> None:
                update_sizes.append(len(data))
                self._real.update(data)

            def hexdigest(self) -> str:
                return self._real.hexdigest()

        monkeypatch.setattr(hashlib, "sha256", lambda: SpySha256())

        out = JavaJvmDiagnosticPlatform().inspect_case(self._case(tmp_path, [art]))

        # 真分块: 多次 update; 单块不超 64KB; 总量等于文件大小; hash 正确。
        assert len(update_sizes) > 1, "expected chunked hashing, got single update"
        assert max(update_sizes) <= 64 * 1024
        assert sum(update_sizes) == len(payload)
        assert out[0].size_bytes == len(payload)
        assert out[0].sha256 == original_sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# inspect_case: 路径安全
# --------------------------------------------------------------------------- #
class TestInspectCasePathSafety:
    def _case(self, root, path, kind=ArtifactKind.LOG) -> DiagnosisCase:
        return DiagnosisCase(
            id="case-jvm-path",
            platform_id="java-jvm",
            root_dir=str(root),
            artifacts=[ArtifactRef(id="a1", kind=kind, path=path)],
        )

    def test_absolute_path_rejected(self, tmp_path):
        # 绝对路径: 即使文件真实存在也必须拒绝 (脱离 root_dir)。
        victim = tmp_path / "secret.log"
        victim.write_bytes(b"secret")
        abs_path = str(victim)
        with pytest.raises(InvalidArtifactPathError):
            JavaJvmDiagnosticPlatform().inspect_case(
                self._case(tmp_path, abs_path)
            )

    def test_dotdot_escape_rejected(self, tmp_path):
        # 在 root 下建一个兄弟目录, 用 .. 越界读取。
        sibling = tmp_path.parent / "sibling_secret"
        sibling.mkdir(exist_ok=True)
        try:
            (sibling / "stolen.log").write_bytes(b"stolen")
            escape = "../sibling_secret/stolen.log"
            with pytest.raises(InvalidArtifactPathError):
                JavaJvmDiagnosticPlatform().inspect_case(
                    self._case(tmp_path, escape)
                )
        finally:
            (sibling / "stolen.log").unlink(missing_ok=True)
            sibling.rmdir()

    def test_missing_file_rejected(self, tmp_path):
        # 文件不存在 -> 明确报错 (非静默忽略, 也非 InvalidArtifactPathError)。
        with pytest.raises(ArtifactNotFoundError):
            JavaJvmDiagnosticPlatform().inspect_case(
                self._case(tmp_path, "does_not_exist.log")
            )

    def test_absolute_path_raises_distinct_error_from_missing(self, tmp_path):
        """绝对路径错误 != 文件不存在错误, 两者必须可区分。"""
        from diagnose.errors import DiagnosisError

        # 绝对路径
        with pytest.raises(InvalidArtifactPathError):
            JavaJvmDiagnosticPlatform().inspect_case(
                self._case(tmp_path, "/etc/hostname")
            )
        # 缺文件
        with pytest.raises(ArtifactNotFoundError):
            JavaJvmDiagnosticPlatform().inspect_case(
                self._case(tmp_path, "nope.log")
            )
        # 两者都是 DiagnosisError 子类
        assert issubclass(InvalidArtifactPathError, DiagnosisError)
        assert issubclass(ArtifactNotFoundError, DiagnosisError)
        assert InvalidArtifactPathError is not ArtifactNotFoundError

    def test_directory_path_raises_not_found(self, tmp_path):
        """路径指向目录 (非普通文件) -> ArtifactNotFoundError (非 IsADirectoryError)。"""
        some_dir = tmp_path / "logs"
        some_dir.mkdir()
        # 目录存在, 但不是普通文件, 应抛 ArtifactNotFoundError (领域错误),
        # 而非 IsADirectoryError (OS 异常向上传播)。
        with pytest.raises(ArtifactNotFoundError):
            JavaJvmDiagnosticPlatform().inspect_case(
                self._case(tmp_path, "logs")
            )


# --------------------------------------------------------------------------- #
# seed_hypotheses
# --------------------------------------------------------------------------- #
class TestSeedHypotheses:
    def test_seeds_pending_hypotheses_for_thread_dump(self, tmp_path):
        (tmp_path / "td.txt").write_text("x")
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")])
        result = JavaJvmDiagnosticPlatform().seed_hypotheses(case)
        categories = {h.category for h in result}
        assert {"deadlock", "lock_contention", "cpu_hotspot"} <= categories
        assert all(h.status == HypothesisStatus.PENDING for h in result)

    def test_seeds_heap_hypotheses_for_heap_dump(self, tmp_path):
        (tmp_path / "hd.hprof").write_bytes(b"x")
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="hd", kind=ArtifactKind.HEAP_SNAPSHOT, path="hd.hprof")])
        categories = {h.category for h in JavaJvmDiagnosticPlatform().seed_hypotheses(case)}
        assert {"memory_retention", "heap_leak"} <= categories

    def test_no_hypotheses_for_empty_case(self, tmp_path):
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path), artifacts=[])
        assert JavaJvmDiagnosticPlatform().seed_hypotheses(case) == []


# --------------------------------------------------------------------------- #
# builtin_platform_registry
# --------------------------------------------------------------------------- #
class TestBuiltinRegistry:
    def test_factory_returns_registry_with_java_jvm(self):
        reg = builtin_platform_registry()
        assert isinstance(reg, PlatformRegistry)
        d = reg.get("java-jvm").descriptor
        assert d.id == "java-jvm"
        assert d.status == PlatformStatus.AVAILABLE

    def test_factory_lists_java_jvm(self):
        reg = builtin_platform_registry()
        ids = [d.id for d in reg.list_descriptors()]
        assert ids == ["java-jvm"]

    def test_import_does_not_register_implicitly(self):
        """关键: import platform_impl 不触发注册, 无隐式全局副作用。

        检查模块级不含任何 PlatformRegistry 实例; 模块只应暴露工厂函数
        builtin_platform_registry, 不应有模块级 registry 实例。
        """
        import diagnose.platform_impl as p

        # 结构断言: 模块中不应有 PlatformRegistry 实例 (isinstance 只匹配实例,
        # 不误判 PlatformRegistry 类本身)
        for v in vars(p).values():
            if isinstance(v, PlatformRegistry):
                pytest.fail(
                    f"模块不应有 PlatformRegistry 实例，但发现: {type(v).__name__}"
                )

    def test_factory_returns_fresh_instance_each_call(self):
        a = builtin_platform_registry()
        b = builtin_platform_registry()
        assert a is not b
        # 互不影响: 都各自含 java-jvm, 但是独立实例。
        assert a.get("java-jvm") is not b.get("java-jvm")


# --------------------------------------------------------------------------- #
# 集成: create_diagnosis_session -> INCONCLUSIVE (AVAILABLE 无证据)
# --------------------------------------------------------------------------- #
class TestJavaCaseSessionIntegration:
    def test_java_case_yields_inconclusive_when_available(self, tmp_path):
        from diagnose.api import create_diagnosis_session

        (tmp_path / "app.log").write_bytes(b"x")
        case = DiagnosisCase(id="case-java-1", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="a-log", kind=ArtifactKind.LOG, path="app.log")])
        session = create_diagnosis_session(case, builtin_platform_registry())
        result = session.build_result()
        # AVAILABLE 平台不再返回 INSUFFICIENT_CAPABILITY; 无证据 -> INCONCLUSIVE
        assert result.status == DiagnosisStatus.INCONCLUSIVE
        assert result.root_cause is None
        assert result.missing_capabilities == []

    def test_session_carries_inspected_artifacts(self, tmp_path):
        """inspect_case 补全的 size/sha256 应进入 session.case.artifacts。"""
        from diagnose.api import create_diagnosis_session

        data = b"stacktrace"
        (tmp_path / "app.log").write_bytes(data)
        case = DiagnosisCase(
            id="case-java-2",
            platform_id="java-jvm",
            root_dir=str(tmp_path),
            artifacts=[
                ArtifactRef(id="a-log", kind=ArtifactKind.LOG, path="app.log")
            ],
        )
        reg = builtin_platform_registry()
        session = create_diagnosis_session(case, reg)

        arts = session.case.artifacts
        assert len(arts) == 1
        assert arts[0].size_bytes == len(data)
        assert arts[0].sha256 == _sha256_bytes(data)


# --------------------------------------------------------------------------- #
# build_agent_guidance (基类成员 override; agent.py 直接调用)
# --------------------------------------------------------------------------- #
class TestBuildAgentGuidance:
    def test_guidance_returns_text_with_methodology(self, tmp_path):
        (tmp_path / "td.txt").write_text("x")
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")])
        txt = JavaJvmDiagnosticPlatform().build_agent_guidance(case)
        # 方法论保留 (BLOCKED≠deadlock), MCP 工具名不进 guidance
        assert "BLOCKED" in txt or "deadlock" in txt.lower()
        assert "parse_log" not in txt
        assert "<system-reminder>" not in txt
