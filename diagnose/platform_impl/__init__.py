"""平台实现包

存放所有具体诊断平台实现 (java_jvm 及未来 python/nodejs/db 等)。

设计约束 (来自 plan Task 5):
- import 本包不触发任何注册副作用; 内置平台的注册只能通过显式调用
  builtin_platform_registry() 完成。这样 import diagnose.platform_impl 用于
  类型/工具场景时, 不会污染任何全局 registry。
- builtin_platform_registry() 每次返回一个全新的 PlatformRegistry 实例,
  调用方拥有该实例, 互不影响。
"""

from diagnose.platform_impl.java_jvm.platform import JavaJvmDiagnosticPlatform
from diagnose.registry import PlatformRegistry


def builtin_platform_registry() -> PlatformRegistry:
    """构造并返回一个已注册内置平台的 PlatformRegistry。

    显式工厂: 只有调用本函数才会注册内置平台, import 本模块不产生副作用。
    每次调用返回全新实例, 调用方独占所有权。
    """
    registry = PlatformRegistry()
    registry.register(JavaJvmDiagnosticPlatform())
    return registry


__all__ = ["JavaJvmDiagnosticPlatform", "builtin_platform_registry"]
