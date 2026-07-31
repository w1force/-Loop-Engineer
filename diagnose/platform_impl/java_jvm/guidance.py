"""Java runtime 提示正文生成。

根据 JavaEvidenceProfile 决定注入 Agent 的 Java 诊断方法论与边界 (lock contention
vs deadlock、单快照局限、retention vs leak 等)。不列举具体 MCP 工具名或调用步骤 ——
那些由 tool 数组里各工具的 description 提供; guidance 只保留 tool description 无法
表达的语义判断。

本函数只返回正文; 外层 <system-reminder> 由 diagnose/agent.py 统一负责。
"""
from __future__ import annotations

from diagnose.platform_impl.java_jvm.profile import JavaEvidenceProfile

_GENERAL = (
    "# Java/JVM Runtime Guidance\n"
    "Register every material observation via CaptureDiagnosisEvidence. Put kind, outcome, scope, "
    "and details in the top-level finding field; keep a decisive tool-output excerpt in data for "
    "independent review.\n"
    "- finding.outcome must be exactly present, absent, or unknown.\n"
    "- present means the phenomenon was directly observed; absent means it was explicitly checked "
    "and not observed; unknown means this evidence cannot decide.\n"
    "- Never use confirmed, validated, likely, suspected, or a tool-native phrase such as "
    "none_reported as an outcome. Preserve native wording in data and map its observation polarity "
    "to present/absent/unknown.\n"
    "- Diagnosis confirmation belongs to hypotheses, claim proposals, and independent review, not "
    "to evidence findings."
)


def _tda_section(profile: JavaEvidenceProfile) -> str:
    if not profile.thread_dump_ids:
        return ""
    # 不列举 MCP 工具名/调用步骤 (tool 数组的 description 已提供); 只留诊断方法论与边界。
    return (
        "\n\n## Thread dump\n"
        "- BLOCKED alone is lock contention, not deadlock. A deadlock requires an explicit lock-wait cycle.\n"
        "- Record an observed cycle as deadlock_cycle/present and an explicit no-cycle result as "
        "deadlock_cycle/absent. Record observed holder/waiter contention as monitor_contention/present.\n"
        "- A single thread snapshot cannot establish duration or continuous CPU impact. It may record "
        "cpu_busy_snapshot/present, but a CPU-hotspot claim needs cpu_profile/present from an interval "
        "profile or repeated_hot_stack/present across snapshots."
    )


def _heap_dump_section(profile: JavaEvidenceProfile) -> str:
    if not profile.heap_dump_ids:
        return ""
    # 不列举 MCP 工具名/调用步骤 (tool 数组的 description 已提供); 只留诊断方法论与边界。
    return (
        "\n\n## Heap dump (HPROF)\n"
        "- Prefer retained-size, dominator, incoming-reference, or GC-root / retention-path findings.\n"
        "- Do NOT read the binary HPROF directly with generic text tools.\n"
        "- A single heap dump can support retained_objects/present, dominator/present, or "
        "retention_path/present, but cannot establish heap_growth over time."
    )


def _histogram_section(profile: JavaEvidenceProfile) -> str:
    if not profile.heap_histogram_ids:
        return ""
    return (
        "\n\n## Histogram\n"
        "- A histogram shows current instance counts and shallow sizes; record a direct retained-object "
        "observation as finding.kind=retained_objects with outcome=present when it supports a "
        "memory_retention claim.\n"
        "- It cannot independently confirm heap leak (no growth over time)."
    )


def _source_section(profile: JavaEvidenceProfile) -> str:
    if not profile.source_ids:
        return ""
    return (
        "\n\n## Source\n"
        "- Use source only to explain observed runtime facts (e.g. a static collection holding retained objects).\n"
        "- Do not infer a production fault from source code without matching runtime evidence."
    )


def _limitations_section(profile: JavaEvidenceProfile) -> str:
    if not profile.limitations:
        return ""
    items = "\n".join(f"- {m}" for m in profile.limitations)
    return f"\n\n## Investigation limits\n{items}"


def build_java_jvm_reminder_text(profile: JavaEvidenceProfile) -> str:
    """根据证据包画像生成 Java runtime 提示正文 (不含 <system-reminder> 标签)。"""
    sections = [
        _GENERAL,
        _tda_section(profile),
        _heap_dump_section(profile),
        _histogram_section(profile),
        _source_section(profile),
        _limitations_section(profile),
    ]
    return "".join(sections).rstrip() + "\n"
