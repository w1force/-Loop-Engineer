# 诊断 Case 格式 (一期)

本文档说明 `diagnose` 内核一期对调用方的输入约束。一期只做"调用方显式构造 Case ->
内核轻量校验 -> 安全明确结果"这一条链路, 不做自动发现。

## 必填字段

调用方构造一个 `DiagnosisCase` 必须显式提供:

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `str` | Case 的稳定标识, 由调用方分配, 用于关联结果与上游任务。 |
| `platform_id` | `str` | 目标平台 ID, 必须已在 `PlatformRegistry` 中注册 (如 `java-jvm`)。 |
| `root_dir` | `str` | 所有 artifact 的根目录 (绝对路径)。内核只读该目录内的文件。 |
| `artifacts` | `list[ArtifactRef]` | 诊断所需工件列表, 至少应给出 `id` / `kind` / `path`。 |

> 一期**不支持自动 dump 识别**: 内核不会扫描 `root_dir`、不会按文件后缀猜测工件
> 类型, 也不会把一个压缩包自动拆成多个 artifact。调用方必须把每个工件作为
> `ArtifactRef` 显式声明。

## Artifact 引用

`ArtifactRef` 指向 `root_dir` 内的一个具体文件:

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 工件稳定 ID, 用于在证据/假设中引用。 |
| `kind` | 是 | `ArtifactKind` 枚举值 (跨平台粗粒度分类, 见下)。 |
| `path` | 是 | **相对 `root_dir` 的路径**, 不可为绝对路径, 不可含 `..` 越界。 |
| `sha256` / `size_bytes` | 否 | 调用方可预填; 缺省时 `inspect_case` 会流式计算并补全。 |
| `metadata` | 否 | 平台特定信息 (如原始格式 `{"format": "hprof"}`)。 |

### path 边界约束 (fail closed)

- `path` 必须是**相对路径**; 绝对路径会抛 `InvalidArtifactPathError`。
- `path` 规范化后必须仍在 `root_dir` 之内; 含 `..` 越界或符号链接逃逸会抛
  `InvalidArtifactPathError`。
- `path` 指向的文件不存在会抛 `ArtifactNotFoundError`。

内核**不静默忽略**任何非法或缺失的工件: 调用方会立刻拿到明确异常, 而不是在结果
阶段才发现证据缺失。

### ArtifactKind 枚举

跨平台粗粒度分类, **不含**平台专有格式:

| 枚举值 | 含义 |
|---|---|
| `LOG` | 应用/系统日志 |
| `SOURCE` | 源码 |
| `BUILD_METADATA` | 构建元数据 |
| `THREAD_SNAPSHOT` | 线程快照 (如 thread dump) |
| `HEAP_SNAPSHOT` | 堆快照 (如 hprof) |
| `MEMORY_SUMMARY` | 内存摘要 |
| `RUNTIME_CRASH_REPORT` | 运行时崩溃报告 |
| `UNKNOWN` | 兜底, 不应在平台 `artifact_kinds` 中声明 |

> 平台特定格式 (如 `hprof`) 不要新增枚举, 放进 `ArtifactRef.metadata["format"]`。

## 平台能力

一期内置仅 `java-jvm` 一个已知平台, 且其状态为 **`PLANNED`**:

- **可以**: 注册、构造 Case、创建 session、`inspect_case` 做路径安全校验与流式 hash。
- **不能**: 分析生产证据、执行任何 action、产出根因。

`PLANNED` 平台经 `create_diagnosis_session(...)` 后 `build_result()` 返回
`DiagnosisStatus.INSUFFICIENT_CAPABILITY`, `root_cause` 为 `None`,
`missing_capabilities` 给出明确缺口说明。内核**绝不假装能诊断**。

未知 `platform_id` 一律 fail closed: `create_diagnosis_session` 抛
`UnknownPlatformError`, 不返回 `None`, 不静默通过。

## Budget (action 预算)

`create_diagnosis_session` 组装 `DiagnosisPlan` 时会按 budget 对静态 gating 通过的
候选 action 二次过滤 (按 `descriptor.actions` 声明顺序扣减 `estimated_cost`, 超预算的
action 进 `plan.rejected_actions`)。budget 的**单位是 action 调用次数** (与
`AnalysisActionSpec.estimated_cost` 同一量纲), 不是 token 或 wall-clock 时间。

budget 按以下优先级解析, 前一级合法即采用:

1. 显式参数 `create_diagnosis_session(case, registry, budget=N)`;
2. `case.metadata["budget"]`;
3. 内置缺省值 `10`。

```python
# 推荐: 显式传 budget, 契约清晰、不依赖 metadata 约定。
session = create_diagnosis_session(case, registry, budget=5)
```

```python
# 也可在 case.metadata 里设 (向后兼容旧调用方)。
case = DiagnosisCase(
    id="case-java-1",
    platform_id="java-jvm",
    root_dir="/path/to/case-root",
    artifacts=[...],
    metadata={"budget": 5},
)
session = create_diagnosis_session(case, registry)
```

合法性校验对两级来源一致: `bool` 一律排除 (`bool` 是 `int` 子类, 避免 `True` 被当作
`1`), 非正整数一律回退到缺省值, 避免零或负预算进入 plan。显式传入 `budget=None` (缺省)
或不传时, 行为与只读 metadata 的旧版本完全一致。

## 最小示例

```python
from diagnose import (
    ArtifactKind,
    ArtifactRef,
    DiagnosisCase,
    DiagnosisStatus,
    builtin_platform_registry,
    create_diagnosis_session,
)

case = DiagnosisCase(
    id="case-java-1",
    platform_id="java-jvm",
    root_dir="/path/to/case-root",  # artifact 都在该目录内
    artifacts=[
        ArtifactRef(id="a-log", kind=ArtifactKind.LOG, path="app.log"),
        ArtifactRef(
            id="a-heap",
            kind=ArtifactKind.HEAP_SNAPSHOT,
            path="dumps/heap.hprof",
            metadata={"format": "hprof"},
        ),
    ],
)

session = create_diagnosis_session(case, builtin_platform_registry())
result = session.build_result()

assert result.status == DiagnosisStatus.INSUFFICIENT_CAPABILITY
assert result.root_cause is None
```

## 调用方清单 (一期)

- [ ] 显式给出 `platform_id`、`id`、`root_dir`、`artifacts`。
- [ ] 每个 artifact 的 `path` 相对 `root_dir`, 不越界、文件存在。
- [ ] 不要期望内核自动识别 dump 或按后缀分类。
- [ ] Java 平台只能拿到 `INSUFFICIENT_CAPABILITY`, 不要在生产上期望根因。
- [ ] 未知 `platform_id` 用 `try/except` 捕获 `UnknownPlatformError`。
