"""诊断平台基类。

DiagnosticPlatform (ABC) —— 描述一个能被诊断的目标平台 (语言 + 运行时 + 服务形态)
所提供的能力。具体平台**显式继承**本类并实现 abstract 方法; 可选 override
``validate_claim_proposal`` 加平台语义校验 (LSP 友好: 调用方直接调用该方法,
不必 getattr 防御)。

abstract 成员 (一期):
- descriptor:        返回平台描述符 (能力/taxonomy/动作声明)
- inspect_case:      对诊断案例做轻量、确定性的工件识别
- seed_hypotheses:   基于案例产出初始根因假设

具体方法:
- validate_claim_proposal: 高风险结论提案的最低证据要求; 基类默认无规则 (返回 []),
                     具体平台 override 加语义门槛 (如 Java/JVM 的 deadlock/heap-leak)。
- build_agent_guidance: 平台专属 Agent 诊断提示正文; 基类默认返回空串, 具体平台
                        override 注入 runtime 调查路径 (如 Java/JVM 的 TDA/heap 流程)。

具体分析执行接口 (execute) 不在基础协议中。后续 Java 或其他平台真正提供 action 时,
再以独立的 ExecutableDiagnosticPlatform 子类扩展加入 execute(request, context)。

使用约束 (来自 plan §5.2):
- inspect_case 只做轻量、确定性的文件识别 (存在性校验、size_bytes、sha256),
  不以后缀猜测 artifact kind;不得将大文件全文装入内存或发送模型。
- seed_hypotheses 只能产出平台 taxonomy 内的类别; 具体平台依据自身证据包决定
  是否产出 PENDING 假设, 没有足够信息时也可返回空列表。
"""
from abc import ABC, abstractmethod
from diagnose.model import (
    ArtifactRef,
    ClaimProposal,
    DiagnosisCase,
    DiagnosticPlatformDescriptor,
    EvidenceRecord,
    Hypothesis,
)
from diagnose.validation import ValidationIssue


class DiagnosticPlatform(ABC):
    """诊断平台基类。

    一组针对某类目标系统的证据识别、能力声明、诊断 taxonomy、分析动作和关联规则。
    覆盖「语言 + 运行时 + 服务形态」, 但不会迫使未来 Python、Node.js、数据库或
    Kubernetes 使用平台专有概念。

    具体平台**显式继承**本类并实现三个 abstract 成员; validate_claim_proposal 有基类默认
    (返回 []), 需要高风险语义门槛的平台 override。Agent 仍是主要判断者,
    validate_claim_proposal 是机器 backstop。分析执行能力由后续 ExecutableDiagnosticPlatform
    子类扩展, 不在一期范围。
    """

    @property
    @abstractmethod
    def descriptor(self) -> DiagnosticPlatformDescriptor:
        """返回平台描述符 (含能力、taxonomy、动作声明)。"""
        ...

    @abstractmethod
    def inspect_case(self, case: DiagnosisCase) -> list[ArtifactRef]:
        """对诊断案例做轻量、确定性的工件识别。

        只做存在性校验、size_bytes、sha256 等确定性识别,
        不以后缀猜测 artifact kind,不装入大文件全文,不调用模型。
        """
        ...

    @abstractmethod
    def seed_hypotheses(self, case: DiagnosisCase) -> list[Hypothesis]:
        """基于诊断案例产出初始根因假设。

        只能产出平台 taxonomy 内的类别; 没有足够信息时可返回空列表。
        """
        ...

    def validate_claim_proposal(
        self,
        proposal: ClaimProposal,
        evidence: list[EvidenceRecord],
    ) -> list[ValidationIssue]:
        """平台语义校验: 高风险结论提案的最低证据要求。

        基类默认无规则 (返回 []); 具体平台可 override 加语义门槛, 例如 Java/JVM
        要求 deadlock 结论必须有正向 lock-wait cycle、heap-leak 必须有多快照增长。
        返回的 blocking issue 会阻止 proposal 进入独立审查。
        """
        return []

    def build_agent_guidance(self, case: DiagnosisCase) -> str:
        """平台专属的 Agent 诊断提示正文 (供 diagnose/agent.py 注入 reminder)。

        基类默认返回空串 (无平台 guidance); 具体平台 override 注入 runtime 调查路径,
        例如 Java/JVM 的 TDA 死锁检查、堆 dominator/retention 流程。返回正文本身,
        外层 <system-reminder> 由 agent.py 统一负责; agent.py 直接调用本方法,
        基类保证存在, 无需 getattr 防御。
        """
        return ""
