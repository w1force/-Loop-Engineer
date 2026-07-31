"""diagnose 包公共 API 测试

仅通过 `from diagnose import ...` 暴露的公共符号构造一个 Java/JVM case +
session, 断言 AVAILABLE runtime profile 下的安全明确结果。

本文件**不 import diagnose 的任何内部子模块** (如 diagnose.api / diagnose.model),
只依赖 diagnose 包顶层导出的公共 API, 以此锁定 Task 6 暴露的最小入口面。

覆盖 (对应 brief):
- 公共 API 符号可从 `diagnose` 顶层导入。
- `__all__` 声明与导出符号一致 (调用方按 __all__ 即可枚举入口)。
- 调用方用最小代码构造合法 DiagnosisCase (需要 ArtifactRef / ArtifactKind)。
- java-jvm case 经 create_diagnosis_session -> build_result 得到
  INCONCLUSIVE, root_cause 为 None, missing_capabilities 为空
  (AVAILABLE 平台无 validated claim, 不再声明缺失能力)。
- 未知 platform_id 经公共入口仍 fail closed (抛 UnknownPlatformError)。
"""

import pytest

# 仅使用 diagnose 顶层公共 API, 不 import 内部子模块。
from diagnose import (  # noqa: I100  顶层公共 API 导入
    ArtifactKind,
    ArtifactRef,
    DiagnosisCase,
    DiagnosisStatus,
    PlatformRegistry,
    builtin_platform_registry,
    create_diagnosis_session,
)
from diagnose import __all__ as diagnose_all


# --------------------------------------------------------------------------- #
# 公共 API 符号与 __all__
# --------------------------------------------------------------------------- #
class TestPublicApiSurface:
    def test_all_public_symbols_are_importable(self):
        """brief 指定的 4 个核心入口 + 构造 case 所需最小类型都可从顶层导入。"""
        # 核心入口
        assert create_diagnosis_session is not None
        assert builtin_platform_registry is not None
        assert isinstance(PlatformRegistry, type)
        assert isinstance(DiagnosisCase, type)
        # 构造 case 所需最小类型
        assert isinstance(ArtifactRef, type)
        assert isinstance(ArtifactKind, type)
        # 结果断言所需枚举
        assert isinstance(DiagnosisStatus, type)

    def test_all_declares_minimal_public_api(self):
        """__all__ 必须显式声明 brief 要求的最小入口集合。"""
        required = {
            "DiagnosisCase",
            "PlatformRegistry",
            "builtin_platform_registry",
            "create_diagnosis_session",
            "ArtifactRef",
            "ArtifactKind",
        }
        declared = set(diagnose_all)
        missing = required - declared
        assert not missing, f"public API __all__ missing: {sorted(missing)}"

    def test_all_entries_are_actual_attributes(self):
        """__all__ 中每个名字都必须是 diagnose 包真实可访问属性。

        防止 __all__ 写了名字但实际未导入 (typo / 漏 import) 的假绿。
        """
        import diagnose

        for name in diagnose_all:
            assert hasattr(diagnose, name), f"__all__ entry not importable: {name!r}"


# --------------------------------------------------------------------------- #
# 端到端: Java/JVM case -> session -> INCONCLUSIVE (AVAILABLE 无 claim)
# --------------------------------------------------------------------------- #
class TestJavaJvmPublicFlow:
    """调用方用公共 API 几行代码即可得到 AVAILABLE runtime 的安全结果。"""

    def test_java_jvm_case_yields_inconclusive_when_available(self, tmp_path):
        # 准备一个真实存在的 artifact 文件 (inspect_case 会读取并补 size/sha256)。
        log_file = tmp_path / "app.log"
        log_file.write_bytes(b"java.lang.OutOfMemoryError: Java heap space\n")

        case = DiagnosisCase(
            id="case-java-public-1",
            platform_id="java-jvm",
            root_dir=str(tmp_path),
            artifacts=[
                ArtifactRef(id="a-log", kind=ArtifactKind.LOG, path="app.log"),
            ],
        )
        registry = builtin_platform_registry()

        session = create_diagnosis_session(case, registry)
        result = session.build_result()

        # AVAILABLE runtime profile 无 validated claim -> INCONCLUSIVE
        # (非 COMPLETE, 非 INSUFFICIENT_CAPABILITY, 非异常)。
        assert result.status == DiagnosisStatus.INCONCLUSIVE
        # 一期绝不构造 root_cause, 必须为 None (区分 "未实现" 与 "猜了一个")。
        assert result.root_cause is None
        # AVAILABLE 平台不再声明缺失能力, missing_capabilities 为空。
        assert result.missing_capabilities == []
        # case / platform id 透传, 便于上层关联。
        assert result.case_id == "case-java-public-1"
        assert result.platform_id == "java-jvm"

    def test_java_jvm_session_does_not_fake_root_cause(self, tmp_path):
        """一期不假装能诊断: 没有 validated_claims / causal_chain 被捏造。"""
        (tmp_path / "app.log").write_bytes(b"stacktrace here\n")
        case = DiagnosisCase(
            id="case-java-public-2",
            platform_id="java-jvm",
            root_dir=str(tmp_path),
            artifacts=[
                ArtifactRef(id="a-log", kind=ArtifactKind.LOG, path="app.log"),
            ],
        )
        session = create_diagnosis_session(case, builtin_platform_registry())
        result = session.build_result()

        assert result.root_cause is None
        assert result.validated_claims == []
        assert result.causal_chain == []

    def test_builtin_registry_returns_fresh_owned_instance(self):
        """builtin_platform_registry 每次返回新实例, 调用方独占所有权。"""
        a = builtin_platform_registry()
        b = builtin_platform_registry()
        assert isinstance(a, PlatformRegistry)
        assert isinstance(b, PlatformRegistry)
        assert a is not b

    def test_unknown_platform_fails_closed(self, tmp_path):
        """未知 platform_id 经公共入口 fail closed: 抛异常而非返回 None 或静默通过。

        纯黑盒断言 (不 import 内部模块): 捕获异常并核对类型名, 既区分 "抛了" 与
        "没抛", 又避免依赖 diagnose.errors 这一内部子模块。
        """
        case = DiagnosisCase(
            id="case-ghost",
            platform_id="not-a-real-platform",
            root_dir=str(tmp_path),
            artifacts=[],
        )
        registry = builtin_platform_registry()
        with pytest.raises(Exception) as exc_info:
            create_diagnosis_session(case, registry)
        # 类型名锁定 fail-closed 语义, 防止被改成 ValueError/返回 None 等假绿。
        assert type(exc_info.value).__name__ == "UnknownPlatformError"
