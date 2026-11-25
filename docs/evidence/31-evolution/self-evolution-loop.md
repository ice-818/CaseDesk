# 半自动自进化闭环 实现与验证证据

- 日期：2026-11-25
- 范围：在不引入任何外部付费调用、不做全自动 Prompt 优化/自动上线的前提下，实现"机器做确定性环节、人做最终采纳决策"的错例自进化闭环。

## 1. 目标与边界

平台需要一条可审计的反馈改进链路，但必须坚持两条安全原则：

1. **半自动而非全自动**：机器只负责确定性的错例收集、词法归因、冻结集回归、门禁判定；最终是否采纳/上线由 operator 人审，人审不可绕过。
2. **fail-closed**：任何门禁硬条件不满足，提案进入终态 `blocked`，不允许"先上线再说"。

明确**不做**的事：

- 不做 LLM 自动生成 Prompt、自动改写并自动上线——仿真/离线环境没有客观手段验证新 Prompt 的正确性，且违背 HITL 原则。
- 候选变更被严格限定为**确定性、可审查**的类型，当前仅支持"意图关键词补充"（`intent_keyword`），不涉及模型权重训练。
- 全程离线，外部 API 调用数为 0，零付费。

## 2. 状态机

提案生命周期为六阶段加两个失败终态：

```
collect -> attribute -> regress -> gate -> review -> promoted
                        |          |
                        |          +-- 硬条件失败 -> blocked（终态）
                        +-- 人审驳回 -> rejected（终态）
```

| 阶段 | 执行方 | 职责 |
| --- | --- | --- |
| collect | 机器 | 扫描低分未解决反馈，重建完整对话，落为错例集 |
| attribute | 机器 | 词法规则确定性归因，输出类别与命中信号 |
| regress | 机器 | 在已批准冻结集上分别跑基线规则与候选规则，产生两个评测 run |
| gate | 机器 | 对比两个 run，硬条件 fail-closed 判定 |
| review | 人 | operator 批准/驳回，记录 reviewer 与 note |
| promote | 人确认 | 仅 approved 提案可晋升，状态转 `promoted` |

每个阶段都校验当前状态，非法转换（如跳过 attribute 直接 gate）返回 `409`；`blocked`/`rejected`/`promoted` 为终态，无法继续推进。

## 3. 数据模型与迁移

- 新增表 `improvement_proposals`（模型 `ImprovementProposalRow`），含状态机字段、三个外键（错例集、基线 run、候选 run）与两个 CHECK 约束（状态枚举、人审决策枚举）。
- 新增迁移 `alembic/versions/0011_improvement_proposals.py`，`down_revision="0010"`，`downgrade` 直接 drop_table。
- 已在临时 SQLite 库验证 `alembic upgrade head`（0010→0011）与 `downgrade base`（0011→0010）均可正常执行。
- 同步将 `app/repositories/db.py` 的 `EXPECTED_REVISION` 由 `0010` 更新为 `0011`，启动时的迁移版本守卫随之生效。

错例集与冻结集均复用现有 `dataset_versions` 表，以 `task` 字段区分（`reception_feedback` / `intent_classification`），评测 run 复用 `evaluation_runs` 表，对现有商品搜索/购物需求评测链路零侵入。

## 4. 确定性归因

归因引擎（`AttributionEngine`）仅基于反馈原因与用户消息文本，按特异性顺序匹配关键词，无 LLM 调用：

| 归因类别 | 触发信号（示例） |
| --- | --- |
| tool_permission_error | 权限、无权、操作失败、报错、403 |
| faithfulness_gap | 胡说、编造、瞎编、与事实不符、幻觉 |
| retrieval_miss | 找不到、搜不到、没查到、没有相关 |
| intent_error | 答非所问、理解错、没听懂、转人工 |
| answer_quality | 以上均不命中时的默认类别 |

输出每个错例的类别与命中信号，并汇总各类别计数。

## 5. 门禁判定（fail-closed）

`run_gate` 逐用例对比基线与候选结果，计算 `improved`/`regressed` 与 `pass_rate_delta`，硬条件为：

1. 两个 run 均为 `completed`；
2. 候选通过率不低于基线（`candidate_pass_rate >= baseline_pass_rate`）；
3. 不存在原本通过、候选后失败的用例（`regressed` 为空）。

任一不满足即 `passed=False`，记录具体原因，提案转 `blocked`；全部满足才进入 `gate` 阶段等待人审。测试中实测：

- 候选关键词修复了基线误判用例（`催发货` 由默认 `faq_policy` 正确归入 `logistics`），通过率 0.5 → 1.0，`improved=["c2"]`，门禁通过。
- 候选关键词过宽（给 `human_handoff` 加 `订单`）会抢占原本正确的 `order_inquiry` 用例，`regressed=["o1"]`，门禁阻断且无法进入人审/晋升。

## 6. 测试与覆盖率

- `tests/e2e/test_self_evolution.py` 共 8 个用例：完整成功闭环、门禁阻断、非法转换与角色守卫、回归集审查守卫、回归前置校验、人审驳回终态，以及错例沉淀成功路径与沉淀守卫（去重/非 promoted 拒绝）。
- 全量后端测试 **557 passed**，无失败。
- 全量行覆盖率 **TOTAL 85%**，高于 CI 门槛 70%，也高于上一基线 78.08%。

### 关于 coverage 数字的诚实说明

`evolution_repository.py` 的行覆盖率仅显示 44%，但这是 **coverage 工具的测量局限，并非真实未覆盖**，证据有二：

1. 同样完全通过 TestClient/HTTP 端到端测试的成熟模块 `platform_repository.py` 也只显示 48%；而通过直接单元测试的仓库（order 87%、session 95%、idempotency 97%、auth 100%）均正常。
2. 独立探针（在脚本内用 coverage 直接测量一次真实 `create_proposal` 调用）显示该方法被报告"未覆盖"的 135–168 行在真实执行时**全部命中**。

根因是 starlette TestClient 在独立线程（anyio portal）中运行异步 ASGI 应用，且在 `await`（经 aiosqlite 线程做数据库 IO）挂起/恢复后，coverage 的行追踪在该线程上未持续附着，导致方法在 await 点之后的行被系统性漏记。功能本身已被断言完整验证（状态码、状态转换、门禁结果、人审终态均通过），未为美化数字而扭曲代码或堆砌无意义测试。

## 7. 可量化素材（面试口径）

- 一条 6 阶段、2 个失败终态的可审计自进化状态机，人审为强制节点、不可绕过。
- 确定性词法归因覆盖 5 类失败，外部 API 调用 0、零付费、全程离线可复现。
- 门禁实测可拦截"过宽关键词"导致的回归（regressed 用例），fail-closed 成立。
- 晋升错例可确定性沉淀为回归集新版本（draft→人审冻结），按 query 去重、无新增则 409，让回归库随真实反馈持续增厚，且不新增表字段、不绕过 HITL。
- 全量 557 测试通过、整体覆盖率 85%、release gate 4/4，并能解释 coverage 在异步线程下的测量局限。

## 8. 边界与后续

- 当前候选变更仅支持意图关键词；知识条目、检索参数等其他确定性变更类型可按同一状态机扩展。
- 不做真实支付/资金移动、不做 LTR/协同过滤（数据稀疏，不伪造）。
- 候选变更的真实"代码落地"仍由开发者在评审通过后执行，系统负责验证与门禁，不自动写生产配置。

## 9. 错例沉淀：让回归库随真实反馈持续增厚

仅有"提案晋升"还不算完整闭环——已确认修复的错例若不进入冻结集，下次变更可能再次踩坑。因此新增沉淀环节，把晋升提案对应的错例确定性地转为回归用例。

### 9.1 设计取舍

1. **不新增表字段**：候选意图（`CandidateChange.intent`）原本只在请求中传递、未持久化。改为在候选评测 run 的 `manifest` 中新增 `candidate_intent` 字段（关键词已在 `candidate_label`）。manifest 本就是描述"该 run 在测什么"的元数据，加字段语义合理，且对表结构零改动、无需新迁移。
2. **不直接改已批准集**：平台规则是"已审不可改、只能发新版本"。沉淀产出的是回归集的一条新 `DatasetVersionRow`（`status="draft"`），沿用原集全部用例并追加沉淀用例，operator 人审 `approved` 后才成为新冻结集——既自动又不绕过 HITL。
3. **确定性提取**：代表性 query 取错例对话中"包含候选关键词"的那条 user 消息；找不到则回退到第一条 user 消息。gold intent 取候选变更的 intent。全程无 LLM、无外部调用。
4. **按 query 去重**：回归集已存在相同 query 的用例则跳过；若没有任何新增，返回 `409`，不产生空版本。

### 9.2 执行流程（`consolidate_to_regression`）

1. 校验提案状态为 `promoted`，否则 `409`；
2. 读取错例集（`FeedbackCaseBundle`）与候选 run，从其 manifest 重建 `CandidateChange`（`candidate_intent` + `candidate_label`）；
3. 从 manifest 的 `dataset_id` 找到原回归集（`IntentRegressionBundle`）；
4. 对每个错例调用 `build_regression_case_from_feedback` 生成回归用例（新 case_id 以 `sed_` 前缀 + 唯一片段），按 query 去重后追加；
5. 以新版本号构建新 bundle，计算 canonical content_hash，插入一条 `intent_classification`/`regression` 的 `draft` 版本并提交。

### 9.3 测试实测

- 成功路径：用户消息含候选关键词 `催发货`，晋升后沉淀出 `v2` 草稿，原用例保留、新增用例 `query="催发货"`、`expected_intent="logistics"`，再经人审批准成为新冻结集。
- 守卫：非 `promoted` 状态沉淀返回 `409`；回归集已含相同 query、无新增时返回 `409`，不产生冗余版本。

至此闭环为：真实反馈 → 错例收集 → 归因 → 回归 → 门禁 → 人审 → 晋升 → **沉淀为新回归版本 → 人审冻结**，回归库随真实反馈持续增厚，且每一步都可审计、人审不可绕过。
