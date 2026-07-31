"""runtime-evidence-demo 端到端诊断 runner (手工/集成, 非 pytest)。

用法: UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run python scripts/diagnose_runtime_demo.py
预期: Agent 在 Java runtime 规则引导下, 用 TDA/memory-analyzer 得出不越界的结论。
"""
import asyncio
import logging
import sys
from pathlib import Path

# 直接 `python scripts/xxx.py` 运行时 sys.path 不含项目根; 注入以便导入 core/diagnose/config/telemetry。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from typing import cast

from core.agent_loop import AgentConfig
from core.mcp import MCPManager
from core.prompts import build_diagnose_system_prompt
from core.providers.anthropic import AnthropicAdapter
from core.session_memory import await_pending_extractions
from telemetry.file_tracer import FileTracer
from config import get_settings

from diagnose.api import create_diagnosis_session
from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase, DiagnosisReviewPolicy
from diagnose.reviewer import configure_diagnosis_review_agent
from diagnose.workflow import DiagnosisWorkflow
from diagnose.platform_impl import builtin_platform_registry
from diagnose.platform_impl.java_jvm import configure_java_jvm_diagnosis_agent

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = PROJECT_ROOT / "tmp-dir/dump_res/runtime-evidence-demo"
ART = "artifacts/20260729-143806"


def _build_case() -> DiagnosisCase:
    return DiagnosisCase(
        id="runtime-evidence-demo",
        platform_id="java-jvm",
        root_dir=str(DEMO_ROOT),
        artifacts=[
            ArtifactRef(id="source", kind=ArtifactKind.SOURCE, path="src/RuntimeEvidenceDemo.java"),
            ArtifactRef(id="log", kind=ArtifactKind.LOG, path="runtime/application.log"),
            ArtifactRef(id="thread-dump", kind=ArtifactKind.THREAD_SNAPSHOT, path=f"{ART}/thread-dump.txt"),
            ArtifactRef(id="heap-histogram", kind=ArtifactKind.MEMORY_SUMMARY, path=f"{ART}/heap-histogram.txt"),
            ArtifactRef(id="heap-dump", kind=ArtifactKind.HEAP_SNAPSHOT, path=f"{ART}/heap-dump.hprof",
                        metadata={"format": "hprof"}),
            ArtifactRef(id="manifest", kind=ArtifactKind.BUILD_METADATA, path=f"{ART}/manifest.txt"),
        ],
    )


async def main() -> None:
    session = create_diagnosis_session(_build_case(), builtin_platform_registry())
    settings = get_settings()
    if not settings.api_key:
        raise RuntimeError("set LOOP_ENGINEER_API_KEY before running the demo")
    provider = AnthropicAdapter(
        api_key=settings.api_key,
        base_url=settings.base_url,
        debug_sse=settings.debug_sse,
    )
    base = AgentConfig(
        provider=provider,
        system=build_diagnose_system_prompt(),
        model=settings.model,
        max_tokens=settings.max_tokens,
        max_turns=settings.max_turns,
        cwd=str(PROJECT_ROOT),
    )
    config = configure_java_jvm_diagnosis_agent(base, session, project_root=PROJECT_ROOT)
    review_base = AgentConfig(
        provider=provider,
        system=build_diagnose_system_prompt(),
        model=settings.review_model or settings.model,
        max_tokens=settings.max_tokens,
        max_turns=settings.max_turns,
        cwd=str(PROJECT_ROOT),
        mcp_manager=config.mcp_manager,
        transcript_path="review.transcript.jsonl",
    )
    review_policy = DiagnosisReviewPolicy(
        max_rework_rounds=settings.diagnosis_max_rework_rounds
    )
    review_config = configure_diagnosis_review_agent(review_base, session, review_policy)

    manager = config.mcp_manager
    assert manager is not None
    # AgentConfig.mcp_manager 声明为 ToolProvider | None; configure_java_jvm_diagnosis_agent
    # 注入的实际是 MCPManager (具备 start/close 生命周期)。
    mcp = cast(MCPManager, manager)
    # 结构化运行日志 (FileTracer): 默认写 logs/{时间戳}.jsonl, 事后可用 log-query skill 用 jq 查。
    # NoopTracer 不落盘, 无法事后排查 Agent 的工具调用/错误探索。
    tracer = FileTracer(
        ctx={"chain_id": "runtime-evidence-demo"},
        enabled=settings.run_log_enabled,
    )
    print(f"[tracer] structured run log -> {tracer._path}")
    try:
        await mcp.start()
        workflow = DiagnosisWorkflow(
            session=session,
            diagnosis_config=config,
            review_config=review_config,
            review_policy=review_policy,
            tracer=tracer,
        )
        result = await workflow.run(
            "Diagnose this Java service evidence bundle. Confirm/refute deadlock, lock contention, "
            "and heap retention; do not over-claim heap leak from a single snapshot."
        )
        print(result.model_dump_json(indent=2))
        # submit 结束: 把最终 DiagnosisResult 落盘到 tmp-dir/, 作为下游 (修复) 环节的输入上下文。
        out_path = PROJECT_ROOT / "tmp-dir" / "diagnosis-output.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        print(f"\n[diagnosis] 最终输出已写入: {out_path}")
    finally:
        await mcp.close()
        await await_pending_extractions()
        await provider.aclose()


def log_config():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # 结构化运行日志改由 FileTracer 直接写 logs/run.jsonl(不经 logging);此处只配业务 logger 控制台输出。
    logging.getLogger("anthropic").setLevel(logging.DEBUG)
    logging.getLogger("tool_executor").setLevel(logging.DEBUG)
    logging.getLogger("query_loop").setLevel(logging.DEBUG)


if __name__ == "__main__":
    log_config()
    asyncio.run(main())
