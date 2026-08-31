# Verification 测试生成 Skill 集成与剩余缺口

## 当前落地状态

领域测试生成 SOP 与最终执行门禁已经分层：

```text
测试生成 Skill
  SKILL.md + selection.yaml + provenance.yaml
  发现阶段只暴露 name、description 和场景选择 prompt
                         ↓ 选中后加载并冻结完整内容
VerificationPlanProposal
  generation_skill_names + generation_skill_choices + generation_skill_digests
                         ↓ Coordinator 校验并冻结
VerificationPlan
  生成 Skill digest + 既有 verification.yaml 执行 Skill digest
                         ↓
Docker replay + VerificationEngine 七类硬门禁
```

`core.verification.VerificationGenerationSkillCatalog` 对生成 Skill 执行严格发现：

- 目录、frontmatter 和 `selection.yaml.name` 必须使用相同的
  `verification-<type>` 名称；
- 选择阶段不读取 `SKILL.md` body，只读取 `name`、`description` 和
  `selection.yaml`；
- 场景选择必须引用已发布的 scenario id，并至少匹配 `matched_rule`、改动路径或
  `risk_tags` 之一；
- 路径路由支持零层或多层 `**/` 与 `{a,b}` 扩展；k6/Property 还必须命中规则或
  风险标签，不能只因通用源码路径被选中；
- 选中后才读取完整 SOP 和本地加固 reference，并对整目录、来源 commit 和
  vendored 文件计算或校验 SHA-256；
- Planner 必须原样返回选中的名称、场景、选择理由和完整内容摘要，Freezer 再从
  受信根目录重载，复核场景路由并比较摘要；选择或内容漂移都会 fail-closed；
- `verification-property-oracle` 只能作为横切增强，不能单独替代领域 Skill。

项目默认通用 Skill 根目录包含 `skills/verification-generators/`。普通 Agent 侧仍
采用 description-only 发现，只有调用 `Load_Skill` 后才读取完整 `SKILL.md`。
Coordinator 侧应显式配置：

```python
catalog = VerificationGenerationSkillCatalog(
    ["skills/verification-generators"]
)
freezer = VerificationPlanFreezer(
    policy=policy,
    skill_loader=execution_case_pack_loader,
    allowed_skill_names=allowed_execution_case_packs,
    generation_skill_catalog=catalog,
)
```

## Skill 清单与仍未完善的部分

### `verification-api-contract`

SOP 已明确：所有显式响应以及 3xx、5xx、wildcard、default obligation；独立 oracle；
边界、状态、副作用和幂等矩阵；固定 seed/clock/dependency；禁止 retry-until-pass；
Prism header 仅限 mock；隔离与强制 cleanup；artifact manifest schema 和本地 validator。

仍缺：

- 尚无把 `artifact-manifest.json` 自动转换为项目 `verification.yaml` case pack 并
  只读挂载到回放容器的受信 adapter；
- 数据库、消息队列、webhook 等副作用仍需每个目标项目提供受信观察 adapter；
- OpenAPI callback/webhook、异步协议、GraphQL 和复杂远程 `$ref` 不在当前范围；
- 真实 Drift/Prism 工具链和镜像 digest 仍需部署配置与端到端验证。

### `verification-mcp-agent`

SOP 已明确：scripted mock LLM + mock MCP server；initialize、tools/list、tools/call、
非法参数、错误返回、timeout、cancel、server exit、reconnect、retry、幂等、stdio
污染和结构化 transcript oracle；control 只可作为受信策略声明的未改变行为 oracle。

仍缺：

- 当前重点是 Agent 作为 MCP client 的 stdio 链路，不是完整 MCP server
  conformance suite；
- Streamable HTTP、SSE、OAuth、sampling、roots、resources、prompts 等能力需按目标
  MCP 版本继续增加模板；
- 不同 Agent 的消息协议和工具结果归一化需要项目级 adapter；
- timeout/cancel/reconnect 的确定性时钟及进程控制仍需受信 launcher 实现。
- `artifact-manifest.json`/结果目前只有字段约定，没有本地 JSON Schema 与 validator。

### `verification-ui-playwright`

SOP 已明确：oracle 只能来自 Incident、产品契约或受信 control；禁止 candidate oracle、
heal、`skip`/`fixme` 和弱化断言；覆盖功能、校验边界、持久化、网络副作用、console
错误与受限浏览器矩阵；冻结 spec、fixture、mock、lockfile 和浏览器身份；要求可执行
cleanup argv、postcondition 与 receipt。

仍缺：

- 尚无把 candidate-external Playwright bundle 只读挂载进 control/candidate 的
  launcher adapter；
- 视觉回归需要单独审核的基线、字体/渲染归一化和像素容差策略；
- 第三方登录、验证码、支付等流程默认 BLOCKED，需专用 fake 或 sandbox；
- browser image、trace 脱敏和结构化 reporter 需要在实际 CI/Docker 环境做 E2E。
- `artifact-manifest.json`/结果目前只有字段约定，没有本地 JSON Schema 与 validator。

### `verification-performance-k6`

SOP 已明确：HAR 必须脱敏；无 workflow、缺脚本、零样本和 cleanup 失败均 fail-closed；
LG 饱和返回 BLOCKED；SLO 在 candidate 结果前冻结；固定 seed、预热、至少三次有效
重复和统计规则；固定 k6/Node/Playwright/remote JS；仅允许本地隔离目标，必需操作
无法可逆清理时 BLOCKED；仅命中通用源码路径不足以选择该 Skill。

仍缺：

- k6 summary、浏览器指标和 LG 健康状态尚未接成 VerificationEngine 的专用结构化
  evidence provider；
- control/candidate 的交错运行、噪声预算和显著性判定仍需受信调度器；
- 测试账号、流量配额、隔离 namespace 和清理回执依赖目标系统 adapter；
- HAR 脱敏器能处理常见 header/cookie/query/body，但业务自定义敏感字段仍需配置。
- `generation-manifest.yaml`/summary 目前没有本地 schema 与 validator；保留的上游
  `SKILL.md` 还引用 7 个未 vendored 的非权威扩展文档。

### `verification-dbt-model`

SOP 已明确：强制临时 profile/schema；`--empty` 只允许新建的可销毁 namespace；禁止
`enabled:false`；从 SQL diff 生成 branch/null/join/window/date/incremental 边界矩阵；
独立 expected-row oracle；补 schema/type/data contract；固定 dbt、adapter、timezone；
companion data test 禁止 warning-only 降级。

仍缺：

- 各 warehouse 的临时 namespace 创建、权限、成本限制和删除确认需部署 adapter；
- Python model、snapshot、recursive SQL、跨项目/package model 当前明确不适用；
- adapter-specific 类型、宏、UDF 和增量语义需要对应仓库的额外模板；
- `manifest.json`/`run_results.json` 尚未接成 VerificationEngine 专用 evidence provider。
- `generation-manifest.yaml`/结果目前没有本地 schema 与 validator。

### `verification-property-oracle`

SOP 已明确：定位为横切增强；独立 property/oracle 选择；Python、TypeScript、Rust、Go、
Java、Solidity runner 约束；固定 seed、case/shrink budget；保存最小反例、corpus 和
replay 命令；区分冻结回归样本与本轮 counterexample evidence；验证阶段禁止修改生产
代码；有状态测试要求 cleanup receipt；禁止记录 secret/PII；新增依赖必须由策略预批准。

仍缺：

- 这是跨语言设计 SOP，不会自动推断每个仓库的 property library 和构建布局；
- 没有预批准依赖时只能返回 BLOCKED，不能自动安装框架；
- 有状态或外部 I/O 属性仍需领域 fake/model 与资源清理 adapter；
- mutation check 只能在调用方提供的 disposable copy 中执行，目前 Coordinator 尚未
  提供该专用执行阶段。
- `generation-manifest.yaml`/结果目前没有本地 schema 与 validator。

## 未作为独立 Skill 接入

- Postman：只保留“API/Collection 发现”的参考价值。上游不生成 `pm.test`，远程
  Collection/Environment 未冻结，`runCollection` 也不能提供足够逐请求证据。
- Anthropic `webapp-testing`：只吸收 DOM 侦察与 server 生命周期思路。上游缺测试
  计划、fixture、断言和边界矩阵，不能作为独立放行用测试生成 SOP。

## 系统级剩余 TODO

1. **P0：GeneratedCasePack adapter。** 校验每个 Skill 的 generation manifest，转换
   为受信 `verification.yaml`/runner contract，并把生成目录只读挂载到 control 与
   candidate；当前实现已完成选择、完整加载和 plan digest 绑定，但尚未自动执行生成
   文件。
2. **P0：专用 TestGenerationAgent。** 在 Coordinator-owned、candidate-external
   workspace 中受限写入，只允许输出 manifest 声明的文件；不得修改应用代码。
3. **P0：补齐五类生成物硬校验。** API 已有 schema/validator，可拒绝重复 JSON key、
   NaN/Infinity、symlink、路径逃逸、未列出文件、可变依赖和缺失 oracle；MCP、UI、
   k6、dbt、Property 仍需等价的本地 schema/validator。
4. **P1：领域 evidence adapter。** 接入 Playwright reporter、k6 summary/dbt
   artifacts、MCP transcript、PBT counterexample，并绑定现有 replay window。
5. **P1：真实环境 E2E。** 在固定 Docker 镜像上验证六套 generator → case pack →
   control/candidate replay → 七门禁的完整链路；当前静态校验和单元测试不能替代它。
6. **P1：结构化排除条件与覆盖策略。** 当前正向路由条件、k6/Property 的必需信号和
   已选 scenario 均由 Freezer 复核，但 `exclusions` 仍是给 selector 的文本约束，且系统
   尚未判断是否遗漏了另一个同样适用的场景；需扩展 Incident/selection schema 后才能
   机器判定目标环境、oracle 可用性和“所有必需场景”。
7. **P1：生产 bootstrap。** 通用 Agent 默认能发现这六个 Skill，但 Coordinator 仍需
   调用方显式把 `VerificationGenerationSkillCatalog` 注入 PlanFreezer；项目尚无从配置文件
   自动装配整套 Coordinator 的入口。
