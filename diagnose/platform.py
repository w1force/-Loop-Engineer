"""诊断平台协议

定义 DiagnosticPlatform Protocol —— 描述一个能被诊断的目标平台
(语言 + 运行时 + 服务形态)所提供的能力。

本 Task (一期) 的 Protocol 仅含三个成员:
- descriptor:        返回平台描述符 (能力/taxonomy/动作声明)
- inspect_case:      对诊断案例做轻量、确定性的工件识别
- seed_hypotheses:   基于案例产出初始根因假设

具体分析执行接口 (execute) 不在基础协议中。后续 Java 或其他平台真正提供
action 时,再以独立的 ExecutableDiagnosticPlatform 扩展协议加入
`execute(request, context) -> list[EvidenceDraft]`。

PlatformExecutionContext 同样 defer 到 execute 落地时与
ExecutableDiagnosticPlatform 一起定义,本模块不实现。

使用约束 (来自 plan §5.2):
- inspect_case 只做轻量、确定性的文件识别 (存在性校验、size_bytes、sha256),
  不以后缀猜测 artifact kind;不得将大文件全文装入内存或发送模型。
- seed_hypotheses 只能产出平台 taxonomy 内的类别;一期 Java 占位返回空列表。
"""

from typing import Protocol, runtime_checkable

from diagnose.model import (
    ArtifactRef,
    DiagnosisCase,
    DiagnosticPlatformDescriptor,
    Hypothesis,
)


@runtime_checkable
class DiagnosticPlatform(Protocol):
    """诊断平台协议

    一组针对某类目标系统的证据识别、能力声明、诊断 taxonomy、
    分析动作和关联规则。覆盖"语言 + 运行时 + 服务形态",
    但不会迫使未来 Python、Node.js、数据库或 Kubernetes 使用平台专有概念。

    实现方只需提供本协议的三个成员;具体分析执行能力由后续
    ExecutableDiagnosticPlatform 子协议扩展,不在一期范围内。
    """

    @property
    def descriptor(self) -> DiagnosticPlatformDescriptor:
        """返回平台描述符 (含能力、taxonomy、动作声明)。"""
        ...

    def inspect_case(self, case: DiagnosisCase) -> list[ArtifactRef]:
        """对诊断案例做轻量、确定性的工件识别。

        只做存在性校验、size_bytes、sha256 等确定性识别,
        不以后缀猜测 artifact kind,不装入大文件全文,不调用模型。
        """
        ...

    def seed_hypotheses(self, case: DiagnosisCase) -> list[Hypothesis]:
        """基于案例产出初始根因假设。

        只能产出平台 taxonomy 内的类别;一期 Java 占位可返回空列表。
        """
        ...
