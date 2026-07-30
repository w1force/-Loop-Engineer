"""平台注册表

PlatformRegistry 负责按 platform_id 注册、查找 DiagnosticPlatform。

设计约束:
- 以 platform.descriptor.id 为 key,平台 ID 必须唯一。
- register: 重复 ID 抛 DuplicatePlatformError (不隐式覆盖)。
- get: 未知 ID 抛 UnknownPlatformError (fail closed,绝不返回 None)。
- list_descriptors: 按 platform ID 排序,保证测试/日志/后续 prompt 稳定。

本模块不得 import 任何具体平台实现 (java_jvm 等);
builtin 平台的注册由 diagnose.platform_impl 的显式工厂完成 (Task 5),
避免在 import 阶段产生隐式副作用。
"""

from diagnose.errors import DuplicatePlatformError, UnknownPlatformError
from diagnose.model import DiagnosticPlatformDescriptor
from diagnose.platform import DiagnosticPlatform


class PlatformRegistry:
    """诊断平台注册表

    维护 platform_id -> DiagnosticPlatform 的映射,提供注册、查找与列举能力。
    注册表本身不实例化任何具体平台,平台对象由调用方传入。
    """

    def __init__(self) -> None:
        self._platforms: dict[str, DiagnosticPlatform] = {}

    def register(self, platform: DiagnosticPlatform) -> None:
        """注册一个诊断平台。

        以 platform.descriptor.id 为 key。若该 ID 已注册,抛
        DuplicatePlatformError,避免隐式覆盖既有平台。
        """
        platform_id = platform.descriptor.id
        if platform_id in self._platforms:
            raise DuplicatePlatformError(
                f"platform id already registered: {platform_id!r}"
            )
        self._platforms[platform_id] = platform

    def get(self, platform_id: str) -> DiagnosticPlatform:
        """按 platform_id 查找平台。

        未知 ID 抛 UnknownPlatformError (fail closed),绝不返回 None。
        """
        try:
            return self._platforms[platform_id]
        except KeyError:
            raise UnknownPlatformError(
                f"unknown platform id: {platform_id!r}"
            ) from None

    def list_descriptors(self) -> list[DiagnosticPlatformDescriptor]:
        """按 platform ID 排序返回所有已注册平台的描述符。

        排序保证测试、日志和后续 prompt 的稳定输出顺序。
        """
        return [
            self._platforms[pid].descriptor
            for pid in sorted(self._platforms)
        ]
