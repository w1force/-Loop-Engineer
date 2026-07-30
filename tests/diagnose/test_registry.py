"""PlatformRegistry 与 DiagnosticPlatform Protocol 测试 - TDD RED 阶段

先写失败测试,锁定 Protocol 契约与 Registry 行为,再实现最小代码。

覆盖:
- DiagnosticPlatform Protocol 三成员契约 (descriptor / inspect_case / seed_hypotheses, 无 execute)
- PlatformRegistry.register / get / list_descriptors
- 领域异常: DiagnosisError 基类 + UnknownPlatformError + DuplicatePlatformError
"""

import pytest

from diagnose.model import (
    ArtifactKind,
    ArtifactRef,
    DiagnosisCase,
    DiagnosticPlatformDescriptor,
    DiagnosticTaxonomy,
    Hypothesis,
    PlatformStatus,
)


# --------------------------------------------------------------------------- #
# 测试用纯内存平台实现
# --------------------------------------------------------------------------- #
class FakePlatform:
    """纯内存 DiagnosticPlatform 实现,仅用于测试。

    不依赖任何外部资源;inspect_case 原样返回 case.artifacts,seed_hypotheses 返回空。
    """

    def __init__(self, descriptor: DiagnosticPlatformDescriptor) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> DiagnosticPlatformDescriptor:
        return self._descriptor

    def inspect_case(self, case: DiagnosisCase) -> list[ArtifactRef]:
        return list(case.artifacts)

    def seed_hypotheses(self, case: DiagnosisCase) -> list[Hypothesis]:
        return []


def _make_descriptor(pid: str = "fake") -> DiagnosticPlatformDescriptor:
    """构造一个最小可用的平台描述符。"""
    return DiagnosticPlatformDescriptor(
        id=pid,
        display_name=f"Fake {pid}",
        status=PlatformStatus.PLANNED,
        description=f"fake platform {pid}",
        taxonomy=DiagnosticTaxonomy(categories={}),
    )


# --------------------------------------------------------------------------- #
# 导入与异常层次
# --------------------------------------------------------------------------- #
class TestImportsAndErrors:
    """验证模块可导入,且异常继承关系正确。"""

    def test_imports(self):
        from diagnose.errors import (
            DiagnosisError,
            DuplicatePlatformError,
            UnknownPlatformError,
        )
        from diagnose.platform import DiagnosticPlatform
        from diagnose.registry import PlatformRegistry

        assert DiagnosticPlatform is not None
        assert PlatformRegistry is not None
        assert DiagnosisError is not None
        assert UnknownPlatformError is not None
        assert DuplicatePlatformError is not None

    def test_unknown_platform_error_inherits_diagnosis_error(self):
        from diagnose.errors import DiagnosisError, UnknownPlatformError

        assert issubclass(UnknownPlatformError, DiagnosisError)
        assert issubclass(UnknownPlatformError, Exception)

    def test_duplicate_platform_error_inherits_diagnosis_error(self):
        from diagnose.errors import DiagnosisError, DuplicatePlatformError

        assert issubclass(DuplicatePlatformError, DiagnosisError)
        assert issubclass(DuplicatePlatformError, Exception)


# --------------------------------------------------------------------------- #
# DiagnosticPlatform Protocol 契约
# --------------------------------------------------------------------------- #
class TestProtocolContract:
    """验证 DiagnosticPlatform Protocol 的三成员契约。"""

    def test_protocol_has_three_members(self):
        from diagnose.platform import DiagnosticPlatform

        # 只看公开成员 (过滤 dunder 与下划线前缀), 锁死业务成员集合:
        # descriptor / inspect_case / seed_hypotheses, 不允许多也不允许少。
        # 注意: 本断言只查 DiagnosticPlatform.__dict__ 自身成员;
        # 未来 ExecutableDiagnosticPlatform 继承基础 Protocol 并新增成员时, 需扩展此断言。
        public_members = {
            name for name in DiagnosticPlatform.__dict__ if not name.startswith("_")
        }
        assert public_members == {"descriptor", "inspect_case", "seed_hypotheses"}

    def test_protocol_does_not_define_execute(self):
        from diagnose.platform import DiagnosticPlatform

        # 基础协议绝不含 execute
        assert not hasattr(DiagnosticPlatform, "execute")

    def test_fake_platform_satisfies_protocol(self):
        from diagnose.platform import DiagnosticPlatform

        platform = FakePlatform(_make_descriptor())
        # runtime_checkable: isinstance 检查成员存在性
        assert isinstance(platform, DiagnosticPlatform)

    def test_protocol_is_a_typing_protocol(self):
        from typing import Protocol

        from diagnose.platform import DiagnosticPlatform

        # DiagnosticPlatform 应继承自 typing.Protocol (或 runtime_checkable 包装后仍为 Protocol 子类)
        assert issubclass(DiagnosticPlatform, Protocol)


# --------------------------------------------------------------------------- #
# PlatformRegistry 行为
# --------------------------------------------------------------------------- #
class TestRegistryRegisterAndGet:
    """register / get 的基本行为。"""

    def test_register_and_get_returns_same_instance(self):
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        platform = FakePlatform(_make_descriptor("fake"))
        registry.register(platform)

        assert registry.get("fake") is platform

    def test_get_returns_platform_with_correct_descriptor(self):
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        desc = _make_descriptor("java-jvm")
        registry.register(FakePlatform(desc))

        got = registry.get("java-jvm")
        assert got.descriptor.id == "java-jvm"
        assert got.descriptor.display_name == "Fake java-jvm"

    def test_register_multiple_distinct_platforms(self):
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        p1 = FakePlatform(_make_descriptor("java-jvm"))
        p2 = FakePlatform(_make_descriptor("python-runtime"))
        p3 = FakePlatform(_make_descriptor("node-runtime"))
        registry.register(p1)
        registry.register(p2)
        registry.register(p3)

        assert registry.get("java-jvm") is p1
        assert registry.get("python-runtime") is p2
        assert registry.get("node-runtime") is p3

    def test_register_duplicate_id_raises(self):
        from diagnose.errors import DuplicatePlatformError
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        registry.register(FakePlatform(_make_descriptor("java-jvm")))

        with pytest.raises(DuplicatePlatformError):
            registry.register(FakePlatform(_make_descriptor("java-jvm")))

    def test_duplicate_error_message_contains_id(self):
        from diagnose.errors import DuplicatePlatformError
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        registry.register(FakePlatform(_make_descriptor("java-jvm")))

        with pytest.raises(DuplicatePlatformError) as exc_info:
            registry.register(FakePlatform(_make_descriptor("java-jvm")))
        assert "java-jvm" in str(exc_info.value)


class TestRegistryGetUnknown:
    """get 的 fail-closed 行为。"""

    def test_get_unknown_id_raises(self):
        from diagnose.errors import UnknownPlatformError
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        with pytest.raises(UnknownPlatformError):
            registry.get("nonexistent")

    def test_get_unknown_does_not_return_none(self):
        """get 必须抛异常,绝不返回 None (fail closed)。"""
        from diagnose.errors import UnknownPlatformError
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        try:
            result = registry.get("nonexistent")
        except UnknownPlatformError:
            return  # 期望路径
        # 若没抛异常,则结果绝不应是 None,也不应是任何假值占位
        raise AssertionError(
            f"registry.get 必须抛 UnknownPlatformError, 实际返回: {result!r}"
        )

    def test_unknown_error_message_contains_id(self):
        from diagnose.errors import UnknownPlatformError
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        with pytest.raises(UnknownPlatformError) as exc_info:
            registry.get("ghost-platform")
        assert "ghost-platform" in str(exc_info.value)


class TestRegistryListDescriptors:
    """list_descriptors 的排序与稳定性。"""

    def test_empty_registry_returns_empty_list(self):
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        assert registry.list_descriptors() == []

    def test_list_descriptors_returns_all_registered(self):
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        registry.register(FakePlatform(_make_descriptor("java-jvm")))
        registry.register(FakePlatform(_make_descriptor("python-runtime")))

        ids = [d.id for d in registry.list_descriptors()]
        assert set(ids) == {"java-jvm", "python-runtime"}

    def test_list_descriptors_sorted_by_id(self):
        """按 platform ID 排序,保证测试/日志/prompt 稳定。"""
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        # 故意以非字母序注册
        registry.register(FakePlatform(_make_descriptor("zeta")))
        registry.register(FakePlatform(_make_descriptor("alpha")))
        registry.register(FakePlatform(_make_descriptor("middle")))

        ids = [d.id for d in registry.list_descriptors()]
        assert ids == ["alpha", "middle", "zeta"]

    def test_list_descriptors_returns_descriptor_objects(self):
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        registry.register(FakePlatform(_make_descriptor("java-jvm")))

        descriptors = registry.list_descriptors()
        assert len(descriptors) == 1
        assert isinstance(descriptors[0], DiagnosticPlatformDescriptor)
        assert descriptors[0].id == "java-jvm"


# --------------------------------------------------------------------------- #
# 端到端: 通过 Protocol 调用平台方法
# --------------------------------------------------------------------------- #
class TestEndToEndViaProtocol:
    """通过 DiagnosticPlatform Protocol 类型注解调用平台方法,验证整体可用。"""

    def test_inspect_case_returns_artifacts(self):
        from diagnose.platform import DiagnosticPlatform
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        registry.register(FakePlatform(_make_descriptor("fake")))

        platform: DiagnosticPlatform = registry.get("fake")
        case = DiagnosisCase(
            id="case-1",
            platform_id="fake",
            root_dir="/tmp",
            artifacts=[
                ArtifactRef(id="a1", kind=ArtifactKind.LOG, path="/app.log"),
                ArtifactRef(id="a2", kind=ArtifactKind.SOURCE, path="/App.java"),
            ],
        )
        refs = platform.inspect_case(case)
        assert len(refs) == 2
        assert refs[0].id == "a1"

    def test_seed_hypotheses_returns_list(self):
        from diagnose.platform import DiagnosticPlatform
        from diagnose.registry import PlatformRegistry

        registry = PlatformRegistry()
        registry.register(FakePlatform(_make_descriptor("fake")))

        platform: DiagnosticPlatform = registry.get("fake")
        case = DiagnosisCase(id="case-1", platform_id="fake", root_dir="/tmp")
        hypos = platform.seed_hypotheses(case)
        assert isinstance(hypos, list)
        assert hypos == []
