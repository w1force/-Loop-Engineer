"""Java/JVM 占位诊断平台子包。

显式 import 不触发任何注册副作用; 平台实例由调用方按需构造,
或通过 diagnose.platform_impl.builtin_platform_registry() 显式注册。
"""

from diagnose.platform_impl.java_jvm.platform import JavaJvmDiagnosticPlatform

__all__ = ["JavaJvmDiagnosticPlatform"]
