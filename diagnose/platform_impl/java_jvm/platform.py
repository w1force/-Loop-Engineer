"""Java/JVM runtime profile 诊断平台

JavaJvmDiagnosticPlatform 是诊断内核落地的第一个具体平台。本模块将其从一期
PLANNED 占位升级为 AVAILABLE runtime profile:
- status = AVAILABLE: 已具备可运行的诊断 profile、工件规则、Agent 调查路径和
  外部分析工具 (TDA/memory-analyzer) 接入; 但分析仍由 Agent 经 MCP 驱动,
  capabilities / actions 仍为空 (不走 platform.execute)。
- taxonomy: 8 类 runtime 根因候选 (deadlock/lock_contention/memory_retention/
  heap_leak/cpu_hotspot/thread_starvation/runtime_crash/inconclusive)。
- inspect_case: 只做轻量、确定性的工件识别 (路径安全 + 流式 size/sha256)。
- seed_hypotheses: 依据 case.artifacts 的 kind 生成 PENDING 假设 (不直接确认根因)。
- build_agent_guidance: override 基类, 生成 Java runtime 提示正文 (供 agent.py
  注入 reminder; 基类默认返回空串)。
- 不提供 execute: DiagnosticPlatform 基类无 execute 成员。

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
    ClaimProposal,
    DiagnosisCase,
    DiagnosticPlatformDescriptor,
    DiagnosticTaxonomy,
    EvidenceRecord,
    EvidenceTimeBasis,
    FindingOutcome,
    Hypothesis,
    HypothesisStatus,
    PlatformStatus,
)
from diagnose.platform import DiagnosticPlatform
from diagnose.platform_impl.java_jvm.guidance import build_java_jvm_reminder_text
from diagnose.platform_impl.java_jvm.profile import build_java_jvm_profile
from diagnose.validation import ValidationIssue

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
        status=PlatformStatus.AVAILABLE,
        description=(
            "Java/JVM service offline evidence-bundle diagnosis platform "
            "(Agent-driven: analysis via TDA/memory-analyzer MCP; runtime provides "
            "profile, taxonomy, guidance and claim guardrails)"
        ),
        taxonomy=DiagnosticTaxonomy(
            categories={
                "lock_contention": "Threads are blocked waiting for shared synchronization.",
                "deadlock": "A lock-wait cycle prevents involved threads from progressing.",
                "memory_retention": "Objects are retained and occupy material heap space.",
                "heap_leak": "Heap usage grows because objects remain unintentionally reachable.",
                "cpu_hotspot": "Application execution consumes notable CPU.",
                "thread_starvation": "Work cannot obtain executor threads or another limited resource.",
                "runtime_crash": "JVM/runtime crash or fatal runtime failure.",
                "inconclusive": "Available evidence cannot establish a root cause.",
            },
            # DiagnosisResult.root_cause_category 现有缺省值为 "unknown"; 保持一致,
            # 不为了 Java runtime 改动语言无关结果模型。
            unknown_category="unknown",
        ),
        artifact_kinds=set(_JAVA_ARTIFACT_KINDS),
        capabilities=[],
        actions=[],
    )


class JavaJvmDiagnosticPlatform(DiagnosticPlatform):
    """Java/JVM runtime profile 诊断平台

    AVAILABLE runtime profile:
    1. 声明平台描述符 (AVAILABLE, 无 capability/action, taxonomy 含 8 类根因候选);
    2. 对 case.artifacts 做轻量确定性识别 (inspect_case);
    3. 依据 artifact kind 产出 PENDING 根因假设 (seed_hypotheses);
    4. override 基类 build_agent_guidance, 生成 Java runtime 调查提示正文。

    分析由 Agent 经 MCP 驱动, 不实现 execute, 不依赖 core/。
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
        """依据 case.artifacts 的 kind 生成 PENDING 假设 (不直接确认根因)。

        只产出平台 taxonomy 内的类别; 假设状态恒为 PENDING, 留给 Agent + MCP
        证据收敛到 SUPPORTED / CONTRADICTED / INCONCLUSIVE。
        """
        kinds = {art.kind for art in case.artifacts}
        seeds: list[Hypothesis] = []

        def add(hid: str, category: str, statement: str) -> None:
            seeds.append(Hypothesis(id=hid, category=category, statement=statement,
                                    status=HypothesisStatus.PENDING))

        if ArtifactKind.THREAD_SNAPSHOT in kinds:
            add("seed-lock-contention", "lock_contention",
                "Potential lock contention among threads; pending TDA verification.")
            add("seed-deadlock", "deadlock",
                "Potential deadlock; pending TDA check_deadlocks verification.")
            add("seed-cpu-hotspot", "cpu_hotspot",
                "Potential CPU hotspot; pending thread-state verification.")
        if ArtifactKind.HEAP_SNAPSHOT in kinds or ArtifactKind.MEMORY_SUMMARY in kinds:
            add("seed-memory-retention", "memory_retention",
                "Potential heap retention; pending heap/dominator evidence.")
            add("seed-heap-leak", "heap_leak",
                "Potential heap leak; pending retention-path or multi-snapshot growth evidence.")
        if ArtifactKind.RUNTIME_CRASH_REPORT in kinds:
            add("seed-runtime-crash", "runtime_crash",
                "Potential JVM/runtime crash; pending crash-report verification.")
        return seeds

    def build_agent_guidance(self, case: DiagnosisCase) -> str:
        """override 基类: 生成 Java runtime 调查提示正文 (供 diagnose/agent.py 注入)。

        基类默认返回空串; 本平台注入 TDA/heap 调查路径。返回正文本身, 外层
        <system-reminder> 由 agent.py 统一负责; agent.py 直接调用本方法。
        """
        profile = build_java_jvm_profile(case)
        return build_java_jvm_reminder_text(profile)

    def validate_claim_proposal(
        self,
        proposal: ClaimProposal,
        evidence: list[EvidenceRecord],
    ) -> list[ValidationIssue]:
        """按结构化 finding 和时间依据检查 Java/JVM 结论最低门槛。"""
        findings = [record.finding for record in evidence if record.finding is not None]

        def present(*kinds: str) -> bool:
            return any(
                finding.kind in kinds and finding.outcome == FindingOutcome.PRESENT
                for finding in findings
            )

        observed = ", ".join(
            sorted(
                {
                    f"{finding.kind}/{finding.outcome.value} (scope={finding.scope})"
                    for finding in findings
                }
            )
        ) or "none"

        def requires(requirement: str, *, include_time_basis: bool = False) -> str:
            message = (
                f"java {proposal.category} proposal requires {requirement}; "
                f"referenced evidence observed findings: {observed}"
            )
            if include_time_basis:
                message += f"; observed time_basis={proposal.time_basis.value}"
            return message

        def refutation_or_limit(next_step: str) -> str:
            return f"; this is not a positive claim — represent it as a hypothesis with {next_step}"

        category = proposal.category
        valid = True
        code = "insufficient_java_evidence"
        message = f"java {category} proposal lacks a supported structured finding"

        if category == "deadlock":
            valid = present("deadlock_cycle")
            code = "deadlock_cycle_required"
            message = requires("finding=deadlock_cycle/present") + refutation_or_limit(
                "status=CONTRADICTED for a no-cycle result"
            )
        elif category == "lock_contention":
            valid = present("monitor_contention")
            code = "monitor_contention_required"
            message = requires("finding=monitor_contention/present") + refutation_or_limit(
                "status=INCONCLUSIVE when contention was not directly observed"
            )
        elif category == "memory_retention":
            valid = present("retained_objects", "dominator", "retention_path")
            code = "retention_finding_required"
            message = requires(
                "one of findings=[retained_objects/present, dominator/present, retention_path/present]"
            ) + refutation_or_limit(
                "status=INCONCLUSIVE when material retention was not directly observed"
            )
        elif category == "heap_leak":
            valid = present("heap_growth") and proposal.time_basis in {
                EvidenceTimeBasis.MULTI_SNAPSHOT,
                EvidenceTimeBasis.EVENT_SEQUENCE,
            }
            code = "heap_growth_over_time_required"
            message = requires(
                "finding=heap_growth/present and time_basis in [multi_snapshot, event_sequence]",
                include_time_basis=True,
            ) + refutation_or_limit(
                "status=INCONCLUSIVE and inconclusive_evidence_ids for a single-snapshot limit"
            )
        elif category == "cpu_hotspot":
            valid = (
                present("cpu_profile") and proposal.time_basis == EvidenceTimeBasis.INTERVAL_PROFILE
            ) or (
                present("repeated_hot_stack") and proposal.time_basis == EvidenceTimeBasis.MULTI_SNAPSHOT
            )
            code = "cpu_duration_evidence_required"
            message = requires(
                "either finding=cpu_profile/present with time_basis=interval_profile or "
                "finding=repeated_hot_stack/present with time_basis=multi_snapshot",
                include_time_basis=True,
            ) + refutation_or_limit(
                "status=INCONCLUSIVE when only a point-in-time CPU snapshot exists"
            )
        elif category == "thread_starvation":
            valid = present("thread_pool_starvation", "resource_starvation")
            code = "starvation_finding_required"
            message = requires(
                "one of findings=[thread_pool_starvation/present, resource_starvation/present]"
            ) + refutation_or_limit(
                "status=INCONCLUSIVE when starvation was not directly observed"
            )
        elif category == "runtime_crash":
            valid = present("jvm_fatal_error", "fatal_log_sequence")
            code = "fatal_runtime_finding_required"
            message = requires(
                "one of findings=[jvm_fatal_error/present, fatal_log_sequence/present]"
            ) + refutation_or_limit(
                "status=INCONCLUSIVE when a fatal event was not directly observed"
            )
        elif category == "inconclusive":
            valid = False
            code = "inconclusive_is_not_claim"
            message = (
                "inconclusive must be represented as a hypothesis state, not a positive claim; "
                "use status=INCONCLUSIVE and inconclusive_evidence_ids"
            )

        if valid:
            return []
        return [ValidationIssue(code=code, message=message, target_id=proposal.id)]

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
