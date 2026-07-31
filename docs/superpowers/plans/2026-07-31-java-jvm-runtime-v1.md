# Java/JVM Runtime v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Agent 能针对一个 Java 服务的离线证据包，直接调用 TDA 和 JProfiler MCP 完成调查，并在 `diagnose` 中沉淀证据、假设和结论——Agent 是诊断决策主体，Java runtime 只提供「证据包画像 + 诊断规则 + 提示注入 + 结论护栏」。

**Architecture:** Java runtime 是 `diagnose.platform_impl.java_jvm` 内的一组纯 Python 模块（MCP 工厂 / 证据包画像 / 提示生成 / 平台 profile）。它**不解析 dump**：thread dump 交给 TDA MCP、heap dump 交给 JProfiler MCP，由 Agent 经 `resolve_tools()` 直接调用。诊断 session 接 Agent runtime 的骨架（`configure_diagnosis_agent`）已在一期建好，本计划只追加 Java 专属的 MCP manager、platform guidance 和 claim 护栏。

**Tech Stack:** Python ≥3.12 · pydantic v2 · dataclasses · pytest · 现有 `core.mcp`（stdio MCPManager）· TDA `tda-3.2.jar`（`--mcp` 模式）· JProfiler MCP（`@ej-technologies/jprofiler-mcp@latest`，npx）。

## Global Constraints

逐字取自方案 §1/§2/§10，每个 Task 隐式遵守：

- **不**在 `diagnose/platform_impl/java_jvm` 内重写 TDA 的 thread dump 解析。
- **不**实现 HPROF 二进制解析、dominator tree 或 GC Root 分析。
- **不**把 MCP 工具包装成 `AnalyzeThreadDump`/`AnalyzeHeapDump` 一类固定诊断工具——MCP 工具由 `resolve_tools()` 原样注入。
- **不**恢复 `ToolExecutionObserver` / `OBS-*`。
- **不**改动 `core` 的通用 system prompt，**不**修改 `core.mcp` / `core.agent_loop` / `core.tool_executor` / MCP tool adapter / MCP tool schema。
- 暂**不**固定 JProfiler MCP 的 npm 版本，直接用 `@latest`。
- 暂**不**做复杂的 MCP 健康检查、能力探测缓存或动态工具映射层。
- TDA/JProfiler 的工具调用记录或工具名本身**不是**问题成立的证据；高风险结论
  必须由结果的肯定性语义和证据限制共同支持。
- **不**实现 `DiagnosticPlatform.execute`（基础 Protocol 无此成员）；`AVAILABLE` 在本产品中的含义是「具备可运行的诊断 profile、工件规则、Agent 调查路径和外部分析工具接入」，**不是**「平台自己执行 action」。
- **不**把 `build_agent_guidance` 加入基础 `DiagnosticPlatform` Protocol（它是 Agent-first 产品的组合能力，不是一期稳定基础协议）。
- 所有新代码：**字符串字面量 ASCII**；**docstring/注释中文**（与一期 Task 3 fix 一致）。
- `Hypothesis` 用 `statement`（**没有** `description` 字段）、证据用 `supporting_evidence_ids`/`contradicting_evidence_ids`（**没有** `evidence_ids`）——方案 §7.3 笔误，以源码 `diagnose/model/hypothesis.py` 为准。
- `Claim` 当前没有 polarity 字段。对某个候选根因的**反证**必须通过
  `UpdateDiagnosisHypothesis(status=CONTRADICTED, contradicting_evidence_ids=[...])`
  表达；`SubmitDiagnosisClaim(status=VALIDATED)` 只用于正向确认的根因/事实。这避免
  Java guard 把 “no deadlock” 当成“确认 deadlock”。本期不为此修改语言无关 Claim 模型。
- MCPManager **无 `__aenter__`**，生命周期必须显式 `await manager.start()` / `await manager.close()`。
- `build_java_jvm_profile()` 是可被直接调用的公开纯函数，必须自行拒绝绝对路径和
  `root_dir` 外路径；不能仅依赖调用者已经调用 `inspect_case()` 的约定。

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `diagnose/platform_impl/java_jvm/mcp.py` | 构造带 `tda`+`jprofiler` 的 `MCPManager`，不启动 | 新增 |
| `diagnose/platform_impl/java_jvm/profile.py` | 证据包画像：识别 artifact、算绝对路径、列调查限制 | 新增 |
| `diagnose/platform_impl/java_jvm/guidance.py` | 根据 profile 生成 Java runtime 提示正文（不含外层标签） | 新增 |
| `diagnose/platform_impl/java_jvm/platform.py` | descriptor→AVAILABLE+taxonomy、`seed_hypotheses` 非空、`build_agent_guidance`、收紧 `validate_claim` | 修改 |
| `diagnose/platform_impl/java_jvm/__init__.py` | 导出 `configure_java_jvm_diagnosis_agent` | 修改 |
| `diagnose/agent.py` | `_diagnosis_reminder` 经 `session.platform.build_agent_guidance` 注入 Java 提示 | 修改 |
| `tests/diagnose/platform/test_java_jvm_mcp.py` | MCP 配置单元测试 | 新增 |
| `tests/diagnose/platform/test_java_jvm_profile.py` | profile 单元测试 | 新增 |
| `tests/diagnose/platform/test_java_jvm_guidance.py` | guidance 单元测试 | 新增 |
| `tests/diagnose/platform/test_java_jvm_placeholder.py` | 改造「planned/空假设」断言为 runtime/profile 断言 | 修改 |
| `tests/diagnose/platform/test_java_jvm_claim_guard.py` | claim 护栏测试（§11.5 表格） | 新增 |
| `tests/diagnose/test_agent_controls.py` | 补 Java reminder 注入断言 | 修改 |
| `tests/diagnose/platform/test_tda_integration.py` | TDA 真连接 `@pytest.mark.integration` | 新增 |
| `tmp-dir/dump_res/runtime-evidence-demo/` | 端到端验收输入（只读） | 不改 |

**依赖图：** Task 1（mcp）独立 ｜ Task 2（profile）独立 ｜ Task 3（guidance）← Task 2 ｜ Task 4（platform）← Task 2+3 ｜ Task 5（agent.py）← Task 4 ｜ Task 6（claim guard）← Task 4 ｜ Task 7（组合入口）← Task 1+5 ｜ Task 8（TDA 集成）← Task 1 ｜ Task 9（demo）← 全部。

> **与方案 §13 的顺序差异：** 本计划把 guidance（方案第 4 步）提到 platform 升级（方案第 3 步）之前，因为 `platform.build_agent_guidance` 内部要调用 guidance——否则 Task 4 无法实现。

---

## Task 1: Java MCP 工厂

**Files:**
- Create: `diagnose/platform_impl/java_jvm/mcp.py`
- Test: `tests/diagnose/platform/test_java_jvm_mcp.py`

**Interfaces:**
- Consumes: `from core.mcp import MCPManager, MCPServerConfig`（两者都在 `core.mcp.__all__`）；`MCPServerConfig(name, command, args, ...)` 是 frozen dataclass；`MCPManager(configs, *, ...)` 接位置参数 `configs`。
- Produces: `create_java_jvm_mcp_manager(*, project_root: str | Path) -> MCPManager`——不启动、不 `tools/list`、不映射工具名。

- [ ] **Step 1: 写失败测试**

```python
# tests/diagnose/platform/test_java_jvm_mcp.py
"""Java/JVM MCP 工厂测试 - TDD RED 阶段。

锁定 create_java_jvm_mcp_manager 的配置语义: TDA jar 绝对路径、tda/jprofiler
两个 server、不在 import/构造阶段启动子进程、jar 缺失时失败明确。
"""
from pathlib import Path

import pytest

from core.mcp import MCPManager, MCPServerConfig
from diagnose.platform_impl.java_jvm.mcp import create_java_jvm_mcp_manager


def _cfg_map(manager: MCPManager) -> dict[str, MCPServerConfig]:
    # MCPManager._configs 是 name -> config 的私有映射；此处只作纯配置断言，
    # 不启动 server。若未来提供公开 config view，应改用该公开接口。
    return dict(manager._configs)


@pytest.fixture
def project_root_with_tda(tmp_path) -> Path:
    jar = tmp_path / "mcp-assets/tda/tda-3.2.jar"
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"PK")  # 占位 jar；工厂不得读取或启动它
    return tmp_path


class TestTdaConfig:
    def test_returns_mcp_manager(self, project_root_with_tda):
        manager = create_java_jvm_mcp_manager(project_root=project_root_with_tda)
        assert isinstance(manager, MCPManager)

    def test_has_tda_and_jprofiler_servers(self, project_root_with_tda):
        manager = create_java_jvm_mcp_manager(project_root=project_root_with_tda)
        names = set(_cfg_map(manager).keys())
        assert {"tda", "jprofiler"} <= names

    def test_tda_uses_absolute_jar_path(self, project_root_with_tda):
        jar = project_root_with_tda / "mcp-assets/tda/tda-3.2.jar"
        manager = create_java_jvm_mcp_manager(project_root=project_root_with_tda)
        tda = _cfg_map(manager)["tda"]
        assert tda.command == "java"
        assert str(jar.resolve()) in tda.args
        assert Path(tda.args[tda.args.index("-jar") + 1]).is_absolute()

    def test_tda_args_exact_flags(self, project_root_with_tda):
        tda = _cfg_map(manager := create_java_jvm_mcp_manager(project_root=project_root_with_tda))["tda"]
        assert "-Djava.awt.headless=true" in tda.args
        assert "-jar" in tda.args
        assert "--mcp" in tda.args

    def test_jprofiler_uses_npx_latest(self, project_root_with_tda):
        jp = _cfg_map(manager := create_java_jvm_mcp_manager(project_root=project_root_with_tda))["jprofiler"]
        assert jp.command == "npx"
        assert "-y" in jp.args
        assert "@ej-technologies/jprofiler-mcp@latest" in jp.args


class TestMissingJar:
    def test_missing_jar_raises_filenotfound(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            create_java_jvm_mcp_manager(project_root=tmp_path)
```

> **关于 `_configs`：** `MCPManager.__init__` 把入参存到 `self._configs`（见 `core/mcp/manager.py:24-68`）。测试借此断言配置，不启动子进程。若 implementer 发现字段名不同，改用 `manager.health()`（同步，不连 server）的 server 名断言，但优先 `_configs`。

- [ ] **Step 2: 运行测试验证失败**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_mcp.py -v
```
Expected: FAIL with `ImportError: ... cannot import name 'create_java_jvm_mcp_manager'`。

- [ ] **Step 3: 写最小实现**

```python
# diagnose/platform_impl/java_jvm/mcp.py
"""Java/JVM runtime 的 MCP 工厂。

构造一个带 TDA 和 JProfiler 的 MCPManager, 交给 AgentConfig.mcp_manager。
本模块只负责配置, 不启动 manager, 不调用 tools/list, 不把任何 MCP 工具名
映射为 Java runtime action。Agent 通过 resolve_tools() 直接获得 MCP 工具。
"""
from __future__ import annotations

from pathlib import Path

from core.mcp import MCPManager, MCPServerConfig

_TDA_JAR_REL = "mcp-assets/tda/tda-3.2.jar"
_JPROFILER_PKG = "@ej-technologies/jprofiler-mcp@latest"


def create_java_jvm_mcp_manager(
    *,
    project_root: str | Path,
) -> MCPManager:
    """返回带 tda + jprofiler 的 MCPManager (未启动)。

    - TDA jar 路径基于 project_root 解析为绝对路径;
    - jar 不存在抛 FileNotFoundError, 避免到 start() 时才在子进程里失败;
    - 不启动 Java/npx 子进程, 不调用 tools/list。
    """
    tda_jar = (Path(project_root) / _TDA_JAR_REL).resolve()
    if not tda_jar.is_file():
        raise FileNotFoundError(
            f"TDA jar not found under project_root: expected {tda_jar}"
        )

    return MCPManager(
        [
            MCPServerConfig(
                name="tda",
                command="java",
                args=[
                    "-Djava.awt.headless=true",
                    "-jar",
                    str(tda_jar),
                    "--mcp",
                ],
            ),
            MCPServerConfig(
                name="jprofiler",
                command="npx",
                args=["-y", _JPROFILER_PKG],
            ),
        ]
    )
```

- [ ] **Step 4: 运行测试验证通过**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_mcp.py -v
```
Expected: PASS（6 tests）。

- [ ] **Step 5: pyright + commit**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pyright diagnose/platform_impl/java_jvm/mcp.py tests/diagnose/platform/test_java_jvm_mcp.py
git add diagnose/platform_impl/java_jvm/mcp.py tests/diagnose/platform/test_java_jvm_mcp.py
git commit -m "feat(diagnose): add Java/JVM MCP factory (TDA + JProfiler)"
```

---

## Task 2: Java 证据包画像

**Files:**
- Create: `diagnose/platform_impl/java_jvm/profile.py`
- Test: `tests/diagnose/platform/test_java_jvm_profile.py`

**Interfaces:**
- Consumes: `from diagnose.model import ArtifactKind, DiagnosisCase`；`case.artifacts: list[ArtifactRef]`，每个 `ArtifactRef(id, kind, path, sha256, size_bytes, metadata)`。
- Produces:
  - `@dataclass(frozen=True) JavaArtifactProfile(artifact_id, kind, relative_path, absolute_path, format, size_bytes)`
  - `@dataclass(frozen=True) JavaEvidenceProfile(artifacts, thread_dump_ids, heap_dump_ids, heap_histogram_ids, source_ids, log_ids, manifest_ids, crash_report_ids, limitations)`
  - `build_java_jvm_profile(case: DiagnosisCase) -> JavaEvidenceProfile`

- [ ] **Step 1: 写失败测试**

```python
# tests/diagnose/platform/test_java_jvm_profile.py
"""Java 证据包画像测试 - TDD RED 阶段。

Profile 不是解析器, 只回答: 有哪些证据、每类能回答什么、哪些不能、
Agent 调 MCP 该用哪些绝对路径。HPROF 不被读入内存。
"""
import pytest

from diagnose.errors import InvalidArtifactPathError
from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
from diagnose.platform_impl.java_jvm.profile import (
    JavaEvidenceProfile,
    build_java_jvm_profile,
)


def _art(aid: str, kind: ArtifactKind, path: str, **meta) -> ArtifactRef:
    return ArtifactRef(id=aid, kind=kind, path=path, metadata=meta)


def _case(root, artifacts) -> DiagnosisCase:
    return DiagnosisCase(
        id="c", platform_id="java-jvm", root_dir=str(root), artifacts=artifacts
    )


class TestArtifactClassification:
    def test_classifies_all_kinds(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(
                tmp_path,
                [
                    _art("td", ArtifactKind.THREAD_SNAPSHOT, "td.txt"),
                    _art("hd", ArtifactKind.HEAP_SNAPSHOT, "hd.hprof", format="hprof"),
                    _art("hh", ArtifactKind.MEMORY_SUMMARY, "hh.txt"),
                    _art("src", ArtifactKind.SOURCE, "App.java"),
                    _art("log", ArtifactKind.LOG, "app.log"),
                    _art("mf", ArtifactKind.BUILD_METADATA, "manifest.txt"),
                    _art("crash", ArtifactKind.RUNTIME_CRASH_REPORT, "hs_err.log"),
                ],
            )
        )
        assert profile.thread_dump_ids == ["td"]
        assert profile.heap_dump_ids == ["hd"]
        assert profile.heap_histogram_ids == ["hh"]
        assert profile.source_ids == ["src"]
        assert profile.log_ids == ["log"]
        assert profile.manifest_ids == ["mf"]
        assert profile.crash_report_ids == ["crash"]

    def test_absolute_path_resolved_from_root_dir(self, tmp_path):
        (tmp_path / "nested").mkdir()
        (tmp_path / "nested/td.txt").write_bytes(b"x")
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("td", ArtifactKind.THREAD_SNAPSHOT, "nested/td.txt")])
        )
        assert profile.artifacts[0].absolute_path == str((tmp_path / "nested/td.txt").resolve())
        assert profile.artifacts[0].relative_path == "nested/td.txt"

    def test_rejects_path_outside_root_when_called_directly(self, tmp_path):
        outside = tmp_path.parent / "outside-thread.txt"
        outside.write_text("x")
        case = _case(tmp_path, [_art("td", ArtifactKind.THREAD_SNAPSHOT, "../outside-thread.txt")])
        with pytest.raises(InvalidArtifactPathError, match="escapes root_dir"):
            build_java_jvm_profile(case)

    def test_format_taken_from_metadata(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("hd", ArtifactKind.HEAP_SNAPSHOT, "hd.hprof", format="hprof")])
        )
        assert profile.artifacts[0].format == "hprof"

    def test_size_bytes_copied_from_inspected_artifact(self, tmp_path):
        data = b"abcdef"
        art = ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt", size_bytes=len(data))
        profile = build_java_jvm_profile(_case(tmp_path, [art]))
        assert profile.artifacts[0].size_bytes == len(data)


class TestLimitations:
    def test_single_heap_snapshot_limitation(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("hd", ArtifactKind.HEAP_SNAPSHOT, "hd.hprof")])
        )
        assert any("growth" in m.lower() for m in profile.limitations)

    def test_no_heap_dump_limitation(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("hh", ArtifactKind.MEMORY_SUMMARY, "hh.txt")])
        )
        assert any("retention path" in m.lower() or "gc-root" in m.lower() for m in profile.limitations)

    def test_single_thread_dump_limitation(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("td", ArtifactKind.THREAD_SNAPSHOT, "td.txt")])
        )
        assert any("single thread" in m.lower() or "duration" in m.lower() for m in profile.limitations)

    def test_no_source_limitation(self, tmp_path):
        profile = build_java_jvm_profile(
            _case(tmp_path, [_art("td", ArtifactKind.THREAD_SNAPSHOT, "td.txt")])
        )
        assert any("source" in m.lower() or "ownership" in m.lower() for m in profile.limitations)

    def test_empty_case_has_no_artifacts_but_lists_limitations(self, tmp_path):
        profile = build_java_jvm_profile(_case(tmp_path, []))
        assert profile.artifacts == []
        assert len(profile.limitations) > 0


class TestHprofNotRead:
    def test_hprof_content_not_read_into_memory(self, tmp_path, monkeypatch):
        """对 HEAP_SNAPSHOT 只识别, 不打开文件读内容。"""
        hprof = tmp_path / "hd.hprof"
        hprof.write_bytes(b"not text")

        opened = []
        real_open = open

        def spy_open(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)

        import builtins

        monkeypatch.setattr(builtins, "open", spy_open)
        build_java_jvm_profile(_case(tmp_path, [_art("hd", ArtifactKind.HEAP_SNAPSHOT, "hd.hprof")]))
        assert not any("hd.hprof" in p for p in opened)
```

- [ ] **Step 2: 运行测试验证失败**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_profile.py -v
```
Expected: FAIL `ImportError: cannot import name 'build_java_jvm_profile'`。

- [ ] **Step 3: 写最小实现**

```python
# diagnose/platform_impl/java_jvm/profile.py
"""Java 证据包画像。

Profile 不是解析器, 也不是诊断器。它只回答: 本 case 里有哪些证据、
每类证据能回答什么问题、哪些问题不能回答、Agent 调用 MCP 时应使用哪些绝对路径。

绝对路径必须从 case.artifacts (已经过 platform.inspect_case 校验) 计算,
不从 Agent 输入或 manifest 内的采集机路径采信。HPROF 不被读入内存。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from diagnose.errors import InvalidArtifactPathError
from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase

_LIMIT_SINGLE_HEAP = "single heap snapshot does not establish growth over time"
_LIMIT_NO_HEAP_DUMP = "retention path / GC-root analysis is unavailable (no heap dump)"
_LIMIT_SINGLE_THREAD = "single thread snapshot cannot establish persistent CPU impact or duration"
_LIMIT_NO_SOURCE = "runtime observations cannot be fully traced back to application ownership (no source)"


@dataclass(frozen=True)
class JavaArtifactProfile:
    """单个 Java artifact 的画像条目。"""

    artifact_id: str
    kind: ArtifactKind
    relative_path: str
    absolute_path: str
    format: str | None
    size_bytes: int | None


@dataclass(frozen=True)
class JavaEvidenceProfile:
    """整个 Java case 的证据包画像。"""

    artifacts: list[JavaArtifactProfile]
    thread_dump_ids: list[str]
    heap_dump_ids: list[str]
    heap_histogram_ids: list[str]
    source_ids: list[str]
    log_ids: list[str]
    manifest_ids: list[str]
    crash_report_ids: list[str]
    limitations: list[str] = field(default_factory=list)


def _profile_artifact(root: Path, art: ArtifactRef) -> JavaArtifactProfile:
    declared = Path(art.path)
    if declared.is_absolute():
        raise InvalidArtifactPathError(
            f"artifact path must be relative to root_dir: {art.path!r}"
        )
    absolute = (root / declared).resolve()
    if not absolute.is_relative_to(root):
        raise InvalidArtifactPathError(f"artifact path escapes root_dir: {art.path!r}")
    return JavaArtifactProfile(
        artifact_id=art.id,
        kind=art.kind,
        relative_path=art.path,
        absolute_path=str(absolute),
        format=art.metadata.get("format") if isinstance(art.metadata, dict) else None,
        size_bytes=art.size_bytes,
    )


def _ids_by_kind(profiled: list[JavaArtifactProfile], kind: ArtifactKind) -> list[str]:
    return [p.artifact_id for p in profiled if p.kind == kind]


def _build_limitations(profile: "JavaEvidenceProfile") -> list[str]:
    limits: list[str] = []
    heap_like = profile.heap_dump_ids or profile.heap_histogram_ids
    if len(profile.heap_dump_ids) <= 1 and heap_like:
        limits.append(_LIMIT_SINGLE_HEAP)
    if not profile.heap_dump_ids:
        limits.append(_LIMIT_NO_HEAP_DUMP)
    if len(profile.thread_dump_ids) <= 1:
        limits.append(_LIMIT_SINGLE_THREAD)
    if not profile.source_ids:
        limits.append(_LIMIT_NO_SOURCE)
    return limits


def build_java_jvm_profile(case: DiagnosisCase) -> JavaEvidenceProfile:
    """基于 case.artifacts 建立 Java 证据包画像 (纯函数, 不读文件内容)。"""
    root = Path(case.root_dir).resolve()
    profiled = [_profile_artifact(root, art) for art in case.artifacts]
    partial = JavaEvidenceProfile(
        artifacts=profiled,
        thread_dump_ids=_ids_by_kind(profiled, ArtifactKind.THREAD_SNAPSHOT),
        heap_dump_ids=_ids_by_kind(profiled, ArtifactKind.HEAP_SNAPSHOT),
        heap_histogram_ids=_ids_by_kind(profiled, ArtifactKind.MEMORY_SUMMARY),
        source_ids=_ids_by_kind(profiled, ArtifactKind.SOURCE),
        log_ids=_ids_by_kind(profiled, ArtifactKind.LOG),
        manifest_ids=_ids_by_kind(profiled, ArtifactKind.BUILD_METADATA),
        crash_report_ids=_ids_by_kind(profiled, ArtifactKind.RUNTIME_CRASH_REPORT),
    )
    return JavaEvidenceProfile(
        **{f: getattr(partial, f) for f in [
            "artifacts", "thread_dump_ids", "heap_dump_ids", "heap_histogram_ids",
            "source_ids", "log_ids", "manifest_ids", "crash_report_ids",
        ]},
        limitations=_build_limitations(partial),
    )
```

- [ ] **Step 4: 运行测试验证通过**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_profile.py -v
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pyright diagnose/platform_impl/java_jvm/profile.py tests/diagnose/platform/test_java_jvm_profile.py
git add diagnose/platform_impl/java_jvm/profile.py tests/diagnose/platform/test_java_jvm_profile.py
git commit -m "feat(diagnose): add Java/JVM evidence profile"
```
Expected: PASS（11 tests）。

---

## Task 3: Java runtime guidance

**Files:**
- Create: `diagnose/platform_impl/java_jvm/guidance.py`
- Test: `tests/diagnose/platform/test_java_jvm_guidance.py`

**Interfaces:**
- Consumes: `from diagnose.platform_impl.java_jvm.profile import JavaEvidenceProfile`（Task 2 产物）。
- Produces: `build_java_jvm_reminder_text(profile: JavaEvidenceProfile) -> str`——仅返回正文，外层 `<system-reminder>` 由 `diagnose/agent.py` 统一负责。

- [ ] **Step 1: 写失败测试**

```python
# tests/diagnose/platform/test_java_jvm_guidance.py
"""Java runtime guidance 测试 - TDD RED 阶段。"""
from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
from diagnose.platform_impl.java_jvm.guidance import build_java_jvm_reminder_text
from diagnose.platform_impl.java_jvm.profile import build_java_jvm_profile


def _profile(tmp_path, arts):
    case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path), artifacts=arts)
    return build_java_jvm_profile(case)


class TestGuidanceContent:
    def test_tda_parse_first_when_thread_dump(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")
        ]))
        assert "parse_log" in txt or "parse first" in txt.lower()

    def test_tda_requires_absolute_path(self, tmp_path):
        (tmp_path / "td.txt").write_text("x")
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")
        ]))
        assert str((tmp_path / "td.txt").resolve()) in txt
        assert "absolute" in txt.lower()

    def test_blocked_is_not_deadlock(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")
        ]))
        assert "BLOCKED" in txt
        assert "deadlock" in txt.lower()

    def test_jprofiler_section_when_hprof(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="hd", kind=ArtifactKind.HEAP_SNAPSHOT, path="hd.hprof")
        ]))
        assert "JProfiler" in txt
        assert "HPROF" in txt or "hprof" in txt.lower()

    def test_no_jprofiler_section_when_no_hprof(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="hh", kind=ArtifactKind.MEMORY_SUMMARY, path="hh.txt")
        ]))
        assert "JProfiler" not in txt

    def test_histogram_cannot_prove_heap_leak(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="hh", kind=ArtifactKind.MEMORY_SUMMARY, path="hh.txt")
        ]))
        assert "histogram" in txt.lower()
        assert "heap leak" in txt.lower()

    def test_source_section_when_source(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="s", kind=ArtifactKind.SOURCE, path="App.java")
        ]))
        assert "source" in txt.lower()

    def test_returns_plain_text_without_system_reminder_tag(self, tmp_path):
        txt = build_java_jvm_reminder_text(_profile(tmp_path, [
            ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")
        ]))
        assert "<system-reminder>" not in txt
```

- [ ] **Step 2: 运行测试验证失败**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_guidance.py -v
```
Expected: FAIL `ImportError: cannot import name 'build_java_jvm_reminder_text'`。

- [ ] **Step 3: 写最小实现**

```python
# diagnose/platform_impl/java_jvm/guidance.py
"""Java runtime 提示正文生成。

根据 JavaEvidenceProfile 决定注入 Agent 的 Java 调查规则: TDA (thread dump)、
JProfiler (HPROF)、histogram、source 各自的边界与正确用法。

本函数只返回正文; 外层 <system-reminder> 由 diagnose/agent.py 统一负责。
"""
from __future__ import annotations

from diagnose.platform_impl.java_jvm.profile import JavaEvidenceProfile

_GENERAL = (
    "# Java/JVM Runtime Guidance\n"
    "Investigate the evidence bundle directly with the MCP tools already in your tool pool "
    "(TDA for thread dumps, JProfiler for heap dumps). Do not assume a fixed tool name; "
    "read the current tool list. Register every material finding via CaptureDiagnosisEvidence."
)


def _tda_section(profile: JavaEvidenceProfile) -> str:
    if not profile.thread_dump_ids:
        return ""
    paths = "\n".join(
        f"- thread dump {aid}: {next(a.absolute_path for a in profile.artifacts if a.artifact_id == aid)}"
        for aid in profile.thread_dump_ids
    )
    return (
        "\n\n## TDA (thread dump)\n"
        f"{paths}\n"
        "- TDA parse_log requires an absolute path; use the absolute paths above.\n"
        "- Call TDA parse_log first for a thread dump, then use get_summary / check_deadlocks / "
        "find_long_running on the parsed result.\n"
        "- BLOCKED alone is lock contention, not deadlock. A deadlock requires a TDA check_deadlocks "
        "result or an explicit lock-wait cycle.\n"
        "- A single thread snapshot cannot establish duration or continuous CPU impact."
    )


def _jprofiler_section(profile: JavaEvidenceProfile) -> str:
    if not profile.heap_dump_ids:
        return ""
    paths = "\n".join(
        f"- heap dump {aid}: {next(a.absolute_path for a in profile.artifacts if a.artifact_id == aid)}"
        for aid in profile.heap_dump_ids
    )
    return (
        "\n\n## JProfiler (heap dump / HPROF)\n"
        f"{paths}\n"
        "- Use the JProfiler MCP tools exposed in the current tool list.\n"
        "- Prefer retained-size, dominator, incoming-reference, or GC-root / retention-path findings.\n"
        "- Do NOT read the binary HPROF directly with generic text tools.\n"
        "- A single heap dump can show retention, but not heap growth over time."
    )


def _histogram_section(profile: JavaEvidenceProfile) -> str:
    if not profile.heap_histogram_ids:
        return ""
    return (
        "\n\n## Histogram\n"
        "- A histogram shows current instance counts and shallow sizes; it can support memory_retention.\n"
        "- It cannot independently confirm heap_leak (no growth over time)."
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
        _jprofiler_section(profile),
        _histogram_section(profile),
        _source_section(profile),
        _limitations_section(profile),
    ]
    return "".join(sections).rstrip() + "\n"
```

- [ ] **Step 4: 运行测试验证通过 + commit**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_guidance.py -v
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pyright diagnose/platform_impl/java_jvm/guidance.py tests/diagnose/platform/test_java_jvm_guidance.py
git add diagnose/platform_impl/java_jvm/guidance.py tests/diagnose/platform/test_java_jvm_guidance.py
git commit -m "feat(diagnose): add Java/JVM runtime guidance"
```
Expected: PASS（8 tests）。

---

## Task 4: Java 平台升级为 AVAILABLE profile

**Files:**
- Modify: `diagnose/platform_impl/java_jvm/platform.py`（descriptor、taxonomy、`seed_hypotheses`、新增 `build_agent_guidance`）
- Modify: `diagnose/model/platform.py`（`AVAILABLE` 注释）
- Modify: `diagnose/platform.py`（删「AVAILABLE 必须有 action」旧描述，若存在）
- Modify: `tests/diagnose/platform/test_java_jvm_placeholder.py`（planned/空假设断言 → runtime/profile 断言）
- Test: 见同上修改

**Interfaces:**
- Consumes: Task 2 `build_java_jvm_profile`、Task 3 `build_java_jvm_reminder_text`；`from diagnose.model import Hypothesis, HypothesisStatus, PlatformStatus, ...`。`Hypothesis(id, category, statement, status=PENDING, ...)`——**用 `statement`，无 `description`**。
- Produces:
  - `JavaJvmDiagnosticPlatform.descriptor.status == PlatformStatus.AVAILABLE`，`capabilities=[]`、`actions=[]` 仍空，taxonomy 含 8 类。
  - `seed_hypotheses(case) -> list[Hypothesis]`：依据 artifact kind 生成 PENDING 假设，可能非空。
  - `build_agent_guidance(case: DiagnosisCase) -> str`：调 profile + guidance 返回正文（不加 Protocol 成员）。

> **连锁影响（关键）：** `session.build_result()`（`session.py:130`）对 PLANNED 返回 `INSUFFICIENT_CAPABILITY`、对 AVAILABLE（无证据）返回 `INCONCLUSIVE`。改 AVAILABLE 后，`test_java_case_yields_insufficient_capability` 必然失败——Step 1 同步改它。

- [ ] **Step 1: 改现有测试的 planned/空假设断言**

把 `tests/diagnose/platform/test_java_jvm_placeholder.py` 中以下断言改成 runtime 语义（用 Edit，逐处）：

```python
# TestDescriptor.test_status_is_planned  ->  改名+断言:
class TestDescriptor:
    def test_status_is_available(self):
        assert JavaJvmDiagnosticPlatform().descriptor.status == PlatformStatus.AVAILABLE

    def test_taxonomy_contains_runtime_categories(self):
        cats = JavaJvmDiagnosticPlatform().descriptor.taxonomy.categories
        # 8 类 root-cause 候选
        for key in ("deadlock", "lock_contention", "memory_retention",
                    "heap_leak", "cpu_hotspot", "thread_starvation",
                    "runtime_crash", "inconclusive"):
            assert key in cats

    # 将旧 test_taxonomy_only_unknown 整个替换为本测试；不保留
    # `assert tax.categories == {}`，因为它与 AVAILABLE runtime taxonomy 冲突。

    # test_capabilities_empty / test_actions_empty 保持 (仍为 [])
    # test_artifact_kinds_exclude_unknown 保持
```

`TestSeedHypotheses.test_returns_empty_list` → 改为：

```python
class TestSeedHypotheses:
    def test_seeds_pending_hypotheses_for_thread_dump(self, tmp_path):
        (tmp_path / "td.txt").write_text("x")
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")])
        result = JavaJvmDiagnosticPlatform().seed_hypotheses(case)
        categories = {h.category for h in result}
        assert {"deadlock", "lock_contention", "cpu_hotspot"} <= categories
        assert all(h.status == HypothesisStatus.PENDING for h in result)

    def test_seeds_heap_hypotheses_for_heap_dump(self, tmp_path):
        (tmp_path / "hd.hprof").write_bytes(b"x")
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="hd", kind=ArtifactKind.HEAP_SNAPSHOT, path="hd.hprof")])
        categories = {h.category for h in JavaJvmDiagnosticPlatform().seed_hypotheses(case)}
        assert {"memory_retention", "heap_leak"} <= categories

    def test_no_hypotheses_for_empty_case(self, tmp_path):
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path), artifacts=[])
        assert JavaJvmDiagnosticPlatform().seed_hypotheses(case) == []
```

`TestJavaCaseSessionIntegration.test_java_case_yields_insufficient_capability` → 改为（AVAILABLE 语义）：

```python
    def test_java_case_yields_inconclusive_when_available(self, tmp_path):
        from diagnose.api import create_diagnosis_session
        (tmp_path / "app.log").write_bytes(b"x")
        case = DiagnosisCase(id="case-java-1", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="a-log", kind=ArtifactKind.LOG, path="app.log")])
        session = create_diagnosis_session(case, builtin_platform_registry())
        result = session.build_result()
        # AVAILABLE 平台不再返回 INSUFFICIENT_CAPABILITY; 无证据 -> INCONCLUSIVE
        assert result.status == DiagnosisStatus.INCONCLUSIVE
        assert result.root_cause is None
        assert result.missing_capabilities == []
```

并在文件顶部 import 增补 `HypothesisStatus`：

```python
from diagnose.model import (
    ArtifactKind, ArtifactRef, DiagnosisCase, DiagnosisStatus, HypothesisStatus, PlatformStatus,
)
```

`TestImportAndProtocol.test_platform_has_no_execute_method` **保持不变**（仍无 execute）。`TestBuiltinRegistry.test_factory_returns_registry_with_java_jvm` 中的 `d.status == PlatformStatus.PLANNED` → 改 `AVAILABLE`。

- [ ] **Step 2: 运行测试验证失败**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_placeholder.py -v
```
Expected: FAIL（status 仍 PLANNED、seed 仍空、build_result 仍 INSUFFICIENT_CAPABILITY）。

- [ ] **Step 3: 改 platform.py 实现**

在 `diagnose/platform_impl/java_jvm/platform.py` 中：

(a) `_build_descriptor()` 改 status + taxonomy：

```python
def _build_descriptor() -> DiagnosticPlatformDescriptor:
    return DiagnosticPlatformDescriptor(
        id="java-jvm",
        display_name="Java/JVM",
        status=PlatformStatus.AVAILABLE,
        description=(
            "Java/JVM service offline evidence-bundle diagnosis platform "
            "(Agent-driven: analysis via TDA/JProfiler MCP; runtime provides "
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
            # DiagnosisResult.root_cause_category 现有缺省值为 "unknown"；保持一致，
            # 不为了 Java runtime 改动语言无关结果模型。
            unknown_category="unknown",
        ),
        artifact_kinds=set(_JAVA_ARTIFACT_KINDS),
        capabilities=[],
        actions=[],
    )
```

(b) `seed_hypotheses` 改为依据 artifact kind 生成假设（顶部新增 import：`from diagnose.model import HypothesisStatus`，`from diagnose.platform_impl.java_jvm.guidance import build_java_jvm_reminder_text`，`from diagnose.platform_impl.java_jvm.profile import build_java_jvm_profile`）：

```python
    def seed_hypotheses(self, case: DiagnosisCase) -> list[Hypothesis]:
        """依据 case.artifacts 的 kind 生成 PENDING 假设 (不直接确认根因)。"""
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
```

(c) 新增 `build_agent_guidance`（**不加进 Protocol**，只是 platform 实例方法）：

```python
    def build_agent_guidance(self, case: DiagnosisCase) -> str:
        """生成 Java runtime 调查提示正文 (供 diagnose/agent.py 注入 reminder)。

        不是 DiagnosticPlatform Protocol 成员; agent.py 用 getattr 防御式获取。
        """
        profile = build_java_jvm_profile(case)
        return build_java_jvm_reminder_text(profile)
```

(d) 同步更新 `diagnose/model/platform.py` 中 `AVAILABLE` 的注释为「具备可运行的诊断 profile、工件规则、Agent 调查路径和外部分析工具接入」，并检查 `diagnose/platform.py` 模块 docstring 是否有「AVAILABLE 必须有 action」字样，若有则删除/改写。

- [ ] **Step 4: 运行测试验证通过**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_placeholder.py -v
```
Expected: PASS。

- [ ] **Step 5: 新增 build_agent_guidance 测试并 commit**

在 `test_java_jvm_placeholder.py` 末尾追加：

```python
class TestBuildAgentGuidance:
    def test_guidance_returns_text_with_tda_rules(self, tmp_path):
        (tmp_path / "td.txt").write_text("x")
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")])
        txt = JavaJvmDiagnosticPlatform().build_agent_guidance(case)
        assert "parse_log" in txt or "parse first" in txt.lower()
        assert "<system-reminder>" not in txt
```

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_placeholder.py -v
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pyright diagnose/platform_impl/java_jvm/platform.py diagnose/model/platform.py
git add -A
git commit -m "feat(diagnose): promote Java/JVM platform to AVAILABLE runtime profile"
```

---

## Task 5: 把 Java guidance 接入 reminder

**Files:**
- Modify: `diagnose/agent.py`（`_diagnosis_reminder`）
- Modify: `tests/diagnose/test_agent_controls.py`（补 Java reminder 注入断言）

**Interfaces:**
- Consumes: `session.platform`（`DiagnosticPlatform` 实例，Java 平台有 `build_agent_guidance(case)`）。
- Produces: `_diagnosis_reminder` 返回的 `UserMessage` 内容含 通用 workflow + `platform.build_agent_guidance(case)` 正文 + case 摘要，整体包在 `<system-reminder>` 里。`AgentConfig.system` 仍不变。

- [ ] **Step 1: 补失败测试**

在 `tests/diagnose/test_agent_controls.py` 中新增（需构造一个 Java session；用 `builtin_platform_registry` + `create_diagnosis_session`）：

```python
def test_reminder_includes_java_guidance_when_platform_provides_it(tmp_path):
    from diagnose.agent import configure_diagnosis_agent, _diagnosis_reminder
    from diagnose.api import create_diagnosis_session
    from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
    from diagnose.platform_impl import builtin_platform_registry

    (tmp_path / "td.txt").write_text("x")
    case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                         artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")])
    session = create_diagnosis_session(case, builtin_platform_registry())
    msg = _diagnosis_reminder(session)
    text = msg.content[0].text
    # 通用 workflow + Java guidance + case 摘要三者都在
    assert "Diagnosis Workflow" in text
    assert "parse_log" in text or "parse first" in text.lower()
    assert "case_id" in text
    assert text.startswith("<system-reminder>") and text.rstrip().endswith("</system-reminder>")


    def test_reminder_without_platform_guidance_still_wrapped(tmp_path, monkeypatch):
    # 平台无 build_agent_guidance 时, reminder 仍合法 (仅通用 workflow + 摘要)
    from diagnose.agent import _diagnosis_reminder
    from diagnose.api import create_diagnosis_session
    from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
    from diagnose.platform_impl import builtin_platform_registry

    (tmp_path / "td.txt").write_text("x")
    case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                         artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")])
    session = create_diagnosis_session(case, builtin_platform_registry())
    # build_agent_guidance 是类方法，不能对实例 delattr；替换 session.platform
    # 即可验证 getattr 的无 guidance 分支。get_context 只读取 session.descriptor。
    monkeypatch.setattr(session, "platform", object())
    msg = _diagnosis_reminder(session)
    text = msg.content[0].text
    assert "Diagnosis Workflow" in text
    assert text.startswith("<system-reminder>")
```

- [ ] **Step 2: 运行测试验证失败**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/test_agent_controls.py -v
```
Expected: FAIL（`parse_log` 不在 reminder）。

- [ ] **Step 3: 改 `_diagnosis_reminder`**

`diagnose/agent.py` 中把 `_diagnosis_reminder` 改为：

```python
def _diagnosis_reminder(session: DiagnosisSession) -> UserMessage:
    """构造持久化的诊断流程提醒: 通用 workflow + 平台 runtime guidance + case 摘要。

    平台 guidance 经 getattr 防御式获取 (build_agent_guidance 不是 Protocol 成员);
    无则只注入通用 workflow。整体包在一条 <system-reminder> UserMessage 里。
    """
    guidance = """<system-reminder>
# Diagnosis Workflow

You are conducting an evidence-bound diagnosis for the current case.

1. Begin with GetDiagnosisContext. Treat its artifacts and EVD-* records as the current case context.
2. Use directly available MCP and core tools to investigate. Do not assume a fixed tool name or a fixed MCP server.
3. Use CaptureDiagnosisEvidence to register useful tool findings as EVD-* evidence. Link each finding to case artifacts and include its tool/source identifier and key supporting output.
4. Keep confirmed facts, hypotheses, and refutations separate. Use UpdateDiagnosisHypothesis with CONTRADICTED for refuted candidates; use a validated claim only for a positive confirmed finding. Do not turn an observed symptom, a tool invocation, or an investigation lead into a confirmed root cause.
5. SubmitDiagnosisClaim validates evidence references. A claim without sufficient evidence is downgraded rather than treated as confirmed.
6. FinalizeDiagnosis is a gate, not a shortcut. Resolve its rejection reasons with more tool calls or return an explicitly inconclusive diagnosis.
"""
    guidance_builder = getattr(session.platform, "build_agent_guidance", None)
    if guidance_builder is not None:
        guidance += "\n" + guidance_builder(session.case)

    context = session.get_context()
    context_hint = (
        "\n# Diagnosis Case\n"
        f"- case_id: {context['case_id']}\n"
        f"- platform_id: {context['platform_id']}\n"
        f"- artifact_count: {len(context['artifacts'])}\n"
        "</system-reminder>"
    )
    return UserMessage(content=[TextBlock(text=guidance + context_hint)])
```

- [ ] **Step 4: 运行测试验证通过 + commit**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose -q
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pyright diagnose tests/diagnose
git add diagnose/agent.py tests/diagnose/test_agent_controls.py
git commit -m "feat(diagnose): inject platform guidance into diagnosis reminder"
```

---

## Task 6: 收紧 Java claim 护栏

**Files:**
- Modify: `diagnose/platform_impl/java_jvm/platform.py`（`validate_claim` 规则关键词）
- Test: `tests/diagnose/platform/test_java_jvm_claim_guard.py`

**Interfaces:**
- Consumes: `validate_claim(claim: Claim, evidence: list[EvidenceRecord]) -> list[ClaimValidationIssue]`（Java 额外方法，session 经 `getattr` 调用）。`Claim(status=ClaimStatus.VALIDATED, statement, evidence_ids)`。
- Produces: 高风险结论只接受**肯定性结果**。`check_deadlocks` 的调用或 "no deadlock"
  文本不能支持 deadlock；GC root、dominator 或 retention path 只能支持
  `memory_retention`，不能单独确认随时间增长的 `heap_leak`；仅 BLOCKED 或仅
  histogram 均返回 issue（session 据此降级为 UNVALIDATED）。

- [ ] **Step 1: 写失败测试**

```python
# tests/diagnose/platform/test_java_jvm_claim_guard.py
"""Java claim 护栏测试 - 对应方案 §11.5。"""
from diagnose.model import Claim, ClaimStatus, EvidenceRecord
from diagnose.platform_impl.java_jvm.platform import JavaJvmDiagnosticPlatform

_PLAT = JavaJvmDiagnosticPlatform()


def _ev(eid: str, summary: str, data=None) -> EvidenceRecord:
    return EvidenceRecord(
        id=eid, dedup_key=f"k-{eid}", platform_id="java-jvm",
        artifact_ids=["a"], analyzer_id="tda", summary=summary,
        data=data or {},
    )


def _claim(stmt: str, evs):
    return Claim(id="cl-1", statement=stmt, status=ClaimStatus.VALIDATED,
                 evidence_ids=[e.id for e in evs])


class TestDeadlockGuard:
    def test_three_blocked_no_cycle_downgrades(self):
        issues = _PLAT.validate_claim(
            _claim("deadlock between workers", [_ev("E1", "3 threads BLOCKED")]), [_ev("E1", "3 threads BLOCKED")]
        )
        assert issues  # 非空 -> session 降级

    def test_tda_deadlock_cycle_allowed(self):
        ev = _ev("E1", "TDA check_deadlocks: deadlock cycle found")
        assert _PLAT.validate_claim(_claim("deadlock found", [ev]), [ev]) == []

    def test_tda_no_deadlock_result_does_not_allow_deadlock(self):
        ev = _ev("E1", "TDA check_deadlocks: no deadlock found")
        assert _PLAT.validate_claim(_claim("deadlock found", [ev]), [ev])


class TestHeapLeakGuard:
    def test_single_histogram_heap_leak_downgrades(self):
        ev = _ev("E1", "histogram: byte[] occupies 84MB")
        assert _PLAT.validate_claim(_claim("heap leak", [ev]), [ev])

    def test_jprofiler_retention_path_does_not_confirm_heap_leak(self):
        ev = _ev("E1", "JProfiler path to GC root via static list")
        assert _PLAT.validate_claim(_claim("heap leak", [ev]), [ev])

    def test_multi_snapshot_growth_allowed(self):
        ev = _ev("E1", "two snapshots show a sustained growth trend in retained bytes")
        assert _PLAT.validate_claim(_claim("heap leak", [ev]), [ev]) == []


class TestMemoryRetention:
    def test_histogram_supports_retention(self):
        ev = _ev("E1", "histogram: 50000 DemoOrder, retained")
        # memory_retention 不在 heap-leak 关键词, 不触发 heap-leak 规则
        assert _PLAT.validate_claim(_claim("memory retention observed", [ev]), [ev]) == []


class TestUnvalidatedPasses:
    def test_unvalidated_claim_not_guarded(self):
        ev = _ev("E1", "3 threads BLOCKED")
        claim = Claim(id="cl-1", statement="deadlock", status=ClaimStatus.UNVALIDATED, evidence_ids=["E1"])
        assert _PLAT.validate_claim(claim, [ev]) == []
```

- [ ] **Step 2: 运行测试验证失败**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_claim_guard.py -v
```
Expected: 部分 FAIL（正向 deadlock cycle / multi-snapshot growth 关键词未覆盖）。

- [ ] **Step 3: 改 `validate_claim` 规则**

替换 `diagnose/platform_impl/java_jvm/platform.py` 中 `validate_claim` 的两条关键词集合：

```python
    def validate_claim(self, claim: Claim, evidence: list[EvidenceRecord]) -> list[ClaimValidationIssue]:
        """校验 Java/JVM 高风险结论的最低证据要求 (Agent 仍是主要判断者)。"""
        if claim.status.value != "validated":
            return []
        evidence_text = "\n".join(
            f"{record.summary}\n{record.data}".lower()
            for record in evidence
            if record.id in claim.evidence_ids
        )
        statement = claim.statement.lower()
        issues: list[ClaimValidationIssue] = []

        if "deadlock" in statement and not any(
            marker in evidence_text
            for marker in (
                "deadlock cycle found", "lock-wait cycle found", "jvm deadlock detected",
            )
        ):
            issues.append(ClaimValidationIssue(
                "java deadlock claim requires a positive deadlock-cycle result"
            ))

        if ("heap leak" in statement or "memory leak" in statement) and not any(
            marker in evidence_text
            for marker in (
                "growth trend", "increasing retained", "sustained growth",
                "multiple snapshots show", "two snapshots show",
            )
        ):
            issues.append(ClaimValidationIssue(
                "java heap-leak claim requires multi-snapshot growth evidence; a retention path alone shows retention, not a leak"
            ))
        return issues
```

- [ ] **Step 4: 运行测试验证通过 + commit**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_claim_guard.py -v
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pyright diagnose/platform_impl/java_jvm/platform.py
git add diagnose/platform_impl/java_jvm/platform.py tests/diagnose/platform/test_java_jvm_claim_guard.py
git commit -m "feat(diagnose): tighten Java/JVM claim guard keywords"
```
Expected: PASS（8 tests）。

---

## Task 7: Java Agent 组合入口

**Files:**
- Modify: `diagnose/platform_impl/java_jvm/__init__.py`
- Test: `tests/diagnose/platform/test_java_jvm_mcp.py`（追加组合入口测试）

**Interfaces:**
- Consumes: Task 1 `create_java_jvm_mcp_manager`；`diagnose.agent.configure_diagnosis_agent`；`dataclasses.replace`；`AgentConfig.mcp_manager: ToolProvider | None`。
- Produces: `configure_java_jvm_diagnosis_agent(config, session, *, project_root) -> AgentConfig`——注入专属 MCPManager，再委托 `configure_diagnosis_agent`；若 `config.mcp_manager` 已存在则拒绝。

- [ ] **Step 1: 写失败测试**（追加到 `test_java_jvm_mcp.py`）

```python
class TestComposeAgent:
    def test_attaches_dedicated_mcp_manager(self, tmp_path, monkeypatch):
        # 避免真实 start: 只验配置被注入, 不连子进程
        from diagnose.platform_impl.java_jvm import configure_java_jvm_diagnosis_agent
        from diagnose.agent import configure_diagnosis_agent  # noqa: F401
        from core.agent_loop import AgentConfig
        from diagnose.api import create_diagnosis_session
        from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
        from diagnose.platform_impl import builtin_platform_registry

        (tmp_path / "mcp-assets/tda").mkdir(parents=True)
        (tmp_path / "mcp-assets/tda/tda-3.2.jar").write_bytes(b"PK")
        (tmp_path / "td.txt").write_text("x")
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")])
        session = create_diagnosis_session(case, builtin_platform_registry())

        base = AgentConfig(provider=_FakeProvider(), system="diag", model="m", max_tokens=1)
        out = configure_java_jvm_diagnosis_agent(base, session, project_root=tmp_path)
        assert out.mcp_manager is not None
        # 控制工具被追加
        assert any(t.name == "GetDiagnosisContext" for t in out.tools)
        # system 不变
        assert out.system == "diag"

    def test_rejects_existing_mcp_manager(self, tmp_path):
        from diagnose.platform_impl.java_jvm import configure_java_jvm_diagnosis_agent
        from core.agent_loop import AgentConfig
        from core.mcp import MCPManager
        from diagnose.api import create_diagnosis_session
        from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
        from diagnose.platform_impl import builtin_platform_registry

        (tmp_path / "mcp-assets/tda").mkdir(parents=True)
        (tmp_path / "mcp-assets/tda/tda-3.2.jar").write_bytes(b"PK")
        (tmp_path / "td.txt").write_text("x")
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")])
        session = create_diagnosis_session(case, builtin_platform_registry())
        base = AgentConfig(provider=_FakeProvider(), system="diag", model="m", max_tokens=1,
                           mcp_manager=MCPManager([]))
        import pytest
        with pytest.raises(ValueError):
            configure_java_jvm_diagnosis_agent(base, session, project_root=tmp_path)
```

> **测试辅助：** 在该测试文件顶部增加满足 `Provider` Protocol 的最小 `_FakeProvider`，
> 不使用 `object()`，以保持 `pyright diagnose tests/diagnose` 为 0 errors：
>
> ```python
> from core.types import Message, StreamEvent
>
>
> class _FakeProvider:
>     def stream(self, **_: object):
>         async def events():
>             if False:
>                 yield StreamEvent(type="message_stop")
>
>         return events()
>
>     def count_tokens(self, messages: list[Message]) -> int:
>         return 0
> ```

- [ ] **Step 2: 运行测试验证失败**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_mcp.py::TestComposeAgent -v
```
Expected: FAIL `ImportError: cannot import name 'configure_java_jvm_diagnosis_agent'`。

- [ ] **Step 3: 写实现**

`diagnose/platform_impl/java_jvm/__init__.py`：

```python
"""Java/JVM runtime 平台: profile + guidance + claim 护栏 + MCP 组合入口。

分析能力由 Agent 直接调用 TDA/JProfiler MCP 提供; 本包不解析 dump。
"""
from pathlib import Path

from core.agent_loop import AgentConfig

from diagnose.agent import configure_diagnosis_agent
from diagnose.platform_impl.java_jvm.mcp import create_java_jvm_mcp_manager
from diagnose.platform_impl.java_jvm.platform import JavaJvmDiagnosticPlatform
from diagnose.session import DiagnosisSession

__all__ = [
    "JavaJvmDiagnosticPlatform",
    "configure_java_jvm_diagnosis_agent",
    "create_java_jvm_mcp_manager",
]


def configure_java_jvm_diagnosis_agent(
    config: AgentConfig,
    session: DiagnosisSession,
    *,
    project_root: str | Path,
) -> AgentConfig:
    """注入专属 Java MCPManager (tda + jprofiler), 再委托通用诊断组合入口。

    - 不在此启动 manager (非 async); 生命周期由调用方管理 (start/close)。
    - 不改 config.system, 不替换 config.tools (由 configure_diagnosis_agent 追加)。
    - 调用方已有 mcp_manager 时拒绝, 避免静默覆盖。
    """
    if config.mcp_manager is not None:
        raise ValueError("Java/JVM diagnosis agent requires a dedicated MCP manager")

    manager = create_java_jvm_mcp_manager(project_root=project_root)
    from dataclasses import replace
    return configure_diagnosis_agent(replace(config, mcp_manager=manager), session)
```

- [ ] **Step 4: 运行测试验证通过 + commit**

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose/platform/test_java_jvm_mcp.py -v
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pyright diagnose/platform_impl/java_jvm/__init__.py
git add diagnose/platform_impl/java_jvm/__init__.py tests/diagnose/platform/test_java_jvm_mcp.py
git commit -m "feat(diagnose): add Java/JVM diagnosis agent compose entry"
```

---

## Task 8: TDA 真连接集成测试

**Files:**
- Create: `tests/diagnose/platform/test_tda_integration.py`

**Interfaces:**
- Consumes: Task 1 `create_java_jvm_mcp_manager`；`MCPManager.start/list_tools/close`。
- Produces: `@pytest.mark.integration` 标注的 TDA 真连接测试。默认 pytest 配置排除
  integration；显式运行时只启动 TDA，绝不因 `manager.start()` 顺带启动 JProfiler。

- [ ] **Step 1: 写测试**

```python
# tests/diagnose/platform/test_tda_integration.py
"""TDA MCP 真连接集成测试。

默认由 pytest 配置排除；显式 `-m integration` 时需要本机 java + jar。
本测试只启动 TDA，不启动 JProfiler，也不需要网络。
"""
import shutil
from pathlib import Path

import pytest

from core.mcp import MCPManager
from diagnose.platform_impl.java_jvm.mcp import create_java_jvm_mcp_manager

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _skip_without_java():
    if not shutil.which("java"):
        pytest.skip("java not available")


@pytest.mark.asyncio
async def test_tda_exposes_parse_log_and_check_deadlocks():
    # 工厂产物同时包含 JProfiler；TDA 专项测试必须裁掉其 config，避免 start()
    # 启动 npx 或触发网络下载。
    full_manager = create_java_jvm_mcp_manager(project_root=_PROJECT_ROOT)
    manager = MCPManager([full_manager._configs["tda"]])
    await manager.start()
    try:
        specs = await manager.list_tools()
        names = {s.name for s in specs}
        assert "parse_log" in names
        assert "check_deadlocks" in names
    finally:
        await manager.close()
```

- [ ] **Step 2: 注册 marker + 验证默认 skip**

在 `pyproject.toml` 的现有 `[tool.pytest.ini_options]` 中注册 marker，并用 `addopts`
默认排除 integration。仅注册 marker 不会阻止 pytest 收集和执行它：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["."]
testpaths = ["tests"]
addopts = "-m 'not integration'"
markers = [
  "integration: requires external processes, excluded from the default test run",
]
```

```bash
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose -q
UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest -o addopts="" -m integration tests/diagnose/platform/test_tda_integration.py -v
git add tests/diagnose/platform/test_tda_integration.py pyproject.toml
git commit -m "test(diagnose): add TDA MCP integration test (opt-in)"
```
Expected: 默认全套会将该集成测试 deselect，因而不会执行；显式运行在本机真连 TDA 时 PASS（暴露
`parse_log`/`check_deadlocks`），且不会启动 JProfiler。

---

## Task 9: demo 端到端验收（手工/集成）

**Files:**
- Create: `scripts/diagnose_runtime_demo.py`（demo runner，不进 pytest 默认集）

**Interfaces:**
- Consumes: 全部前序 Task；`core.agent_loop.submit`、`build_agent_state`；真实 Provider（用户配置）；`configure_java_jvm_diagnosis_agent`；`builtin_platform_registry`；`create_diagnosis_session`。

- [ ] **Step 1: 写 demo runner**

```python
# scripts/diagnose_runtime_demo.py
"""runtime-evidence-demo 端到端诊断 runner (手工/集成, 非 pytest)。

用法: UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run python scripts/diagnose_runtime_demo.py
预期: Agent 在 Java runtime 规则引导下, 用 TDA/JProfiler 得出不越界的结论。
"""
import asyncio
from pathlib import Path

from core.agent_loop import AgentConfig, build_agent_state, submit
from core.prompts import build_diagnose_system_prompt
from core.providers.anthropic import AnthropicAdapter
from core.session_memory import await_pending_extractions
from telemetry.tracer import NoopTracer
from config import get_settings

from diagnose.api import create_diagnosis_session
from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
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
    agent_state = build_agent_state(config)

    manager = config.mcp_manager
    assert manager is not None
    await manager.start()
    try:
        async for event in submit(
            "Diagnose this Java service evidence bundle. Confirm/refute deadlock, lock contention, "
            "and heap retention; do not over-claim heap leak from a single snapshot.",
            agent_state, config, tracer=NoopTracer(),
        ):
            print(event)
    finally:
        await manager.close()
        await await_pending_extractions()
        from core.agent_loop import shutdown_agent_state
        await shutdown_agent_state(agent_state)
        await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
```

> **运行前提：** runner 使用仓库现有 `config.get_settings()`、`AnthropicAdapter` 和
> `telemetry.tracer.NoopTracer`，不包含待替换占位符。运行者须设置
> `LOOP_ENGINEER_API_KEY`；模型、token 上限和 base URL 复用现有 `LOOP_ENGINEER_*`
> 配置。本 Task 不纳入默认 pytest 集合，验收以手工运行结果为准。

- [ ] **Step 2: 手工验收标准（参照方案 §12）**

运行后，Agent 输出应满足：

- **Confirmed:** lock contention（3 个 worker 在同一 monitor 等待）；heap 存在大量 `byte[]` + 50000 `DemoOrder`，源码显示被静态集合 `RETAINED_PAYLOADS`/`RETAINED_ORDERS` 持有 → `memory_retention`。
- **Refuted:** deadlock（TDA `check_deadlocks` 无 lock-wait cycle）；将 `seed-deadlock`
  hypothesis 更新为 `CONTRADICTED`，不要提交 “no deadlock” 的 validated Claim。
- **Unconfirmed:** heap leak（单快照即使提供 retention path 也只能说明可达/保留，不能证明
  增长性泄漏）；CPU impact（单 thread dump）。
- **Final status:** 允许 `inconclusive`；不得编造 root cause；不得把 BLOCKED 判为 deadlock；不得把单 histogram 判为 heap leak。

- [ ] **Step 3: commit demo runner**

```bash
git add scripts/diagnose_runtime_demo.py
git commit -m "docs(diagnose): add runtime-evidence-demo end-to-end runner"
```

---

## 完整验收清单

完成全部 Task 后，必须同时满足：

- [ ] `UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pytest tests/diagnose -q` 全绿（默认集，不含 integration）。
- [ ] `UV_CACHE_DIR=/private/tmp/loop-engineer-uv-cache uv run pyright diagnose tests/diagnose` 0 errors。
- [ ] `git diff --check` 无空白错误。
- [ ] `core/` 未被修改（三文件 `Terminal`/`TextBlock` 是上两个 commit 的既有改动，本计划不动 core）。
- [ ] `rg "AnalyzeThreadDump|AnalyzeHeapDump|ToolExecutionObserver|OBS-" diagnose` 无输出。
- [ ] `rg "NotImplementedError" diagnose/platform_impl/java_jvm` 无输出。
- [ ] `rg "<system-reminder>" diagnose/agent.py` 确认 reminder 仍单层包裹、不改 `config.system`。
- [ ] （集成，可选）`pytest -o addopts="" -m integration tests/diagnose/platform/test_tda_integration.py` 在本机真连 TDA PASS。
- [ ] （手工）`scripts/diagnose_runtime_demo.py` 跑通且结论不越界（见 §12）。

**完成标准不是「Java runtime 自己能解析 dump」，而是：Agent 在 Java runtime 规则引导下，能够使用直接注入的 TDA/JProfiler MCP，对 demo 证据包作出不越界的诊断结论。**

---

## Self-Review 笔记

**Spec 覆盖：** 方案 §1-13 逐节落到 Task——MCP(1) / profile(2) / guidance(3,8) / platform AVAILABLE+taxonomy+seed(4) / agent reminder(5,§8) / claim guard(6,§9,§11.5) / 组合入口(7,§10) / 集成测试(8,§11.3) / demo(9,§12)。§4「profile/guidance 可先不拆分」的选项被采纳为拆分（利于后续 Python runtime 复用）。

**方案事实修正（已纠正）：**
1. `Hypothesis` 无 `description`/`evidence_ids` → 用 `statement` + `supporting_evidence_ids`/`contradicting_evidence_ids`（Task 4）。
2. `MCPManager` 无 `__aenter__` → demo runner 用 `try/finally` 显式 `start/close`（Task 9）。
3. `session.build_result()` 对 AVAILABLE 且无证据返回 `INCONCLUSIVE` → 改测试为精确断言 `INCONCLUSIVE`（Task 4 Step 1）。
4. `build_agent_guidance` 不进 Protocol，agent.py 用 `getattr` 防御式获取（Task 4/5）。
5. demo manifest 内绝对路径是采集机路径（`/Users/wangzheng.440/...`）→ profile 一律用 `case.root_dir` 重算，并在公开 profile 函数中重复验证 containment（Task 2）。
6. integration marker 仅注册不会默认跳过测试，且 `MCPManager.start()` 会启动全部配置
   server → 默认 `addopts` 排除 integration，TDA 专项测试只构造 TDA manager（Task 8）。
7. `check_deadlocks`、GC root、dominator 等只能说明调用或保留路径，不能自动确认
   deadlock/heap leak → claim guard 仅接受正向 deadlock cycle 和多快照增长证据（Task 6）。

**类型一致性：** `create_java_jvm_mcp_manager`、`build_java_jvm_profile`、`build_java_jvm_reminder_text`、`build_agent_guidance`、`configure_java_jvm_diagnosis_agent`、`JavaArtifactProfile`/`JavaEvidenceProfile`/`ClaimValidationIssue` 在各 Task 间签名一致；`Hypothesis(..., statement=..., status=HypothesisStatus.PENDING)` 全程统一。

**已知遗留（不在本期）：** JProfiler 真连接测试（其工具名/参数由 `tools/list` 注入，不预设）；`ExecutableDiagnosticPlatform.execute`（计划 §9，本期明确不做）；预存失败（`test_agent_loop_skill` 等，与本期无关）。
