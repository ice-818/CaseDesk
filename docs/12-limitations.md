# 12 · 限制与路线图

> 一句话：文档必须和代码一致。下面是明确没做完或故意没做的；已补上的不要再写成缺口。

## 本章目录

1. [限制（对照代码）](#限制对照代码)
2. [已补上不要再写成缺口](#已补上不要再写成缺口)
3. [不计划堆料](#不计划用简历堆料补的)
4. [下一阶段](#下一阶段)

## 限制（对照代码）

| 项 | 现状 | 看哪里 |
|---|---|---|
| 数据性质 | 业务与交易数据均为仿真：联调库 24 商品（两店各 12，每店 10 可售/2 零库存）、5 订单、3 账号；非真实交易 | `data/local/app_merchant.db`、`scripts/seed_catalog.py` |
| 交易范围 | 导购为只读核验、售后为申请/审批类操作；不做真实下单、支付、扣库存、购物车（仍未实现） | `app/agents/nodes/shopping.py`、`app/tools/aftersale_tools.py` |
| 排序方式 | 手工可解释规则权重，不做学习排序 LTR、协同过滤与个性化；小数据下这些数据依赖不成立，硬上会被问穿。`ProductRanker` 已预留 LTR 替换接口 | `app/ranking/product.py` |
| 分页一致性 | offset 分页不提供目录并发变更下的快照一致性；SQL 内 `order_by(sku)` 仅保证分页确定性 | `app/repositories/order_repository.py` |
| 外部对接 | 无真实承运商，未连接商家外部规格或检测资料；`feature:` 是目录标注，不是检测认证或性能保证 | `app/tools/`、`docs/22-shopping-loop.md` |
| 多后端 | PostgreSQL 与真实 Qdrant 尚未实测（本机 Docker 暂停）；`DateTime(timezone=True)` 在 SQLite/PG 行为有差异，恢复后需补迁移与全量测试 | `app/repositories/db.py` |
| Embedding | 本地 `BAAI/bge-small-zh-v1.5`；加载失败才词面降级 | `app/providers/embeddings.py` |
| Rerank | 本地 Cross-Encoder `BAAI/bge-reranker-base`；未装权重时词面降级 | `app/providers/reranker.py` |
| Tokenizer | 128k 启发式，非官方 tokenizer | `app/memory/budget.py` |
| 知识库更新 | 启动全量 ingest，无删除 API | `app/retrieval/ingest.py` |
| 文档解析 | 生产 Unstructured（PDF/Office/HTML/图）；缺依赖或 testing 降级 Markdown 切分。OCR/复杂表格不保证 | `app/retrieval/unstructured_parser.py` |
| 遗留数据 | 旧 `app.db` 无迁移版本（已备份 `app.db.bak-20261119`），何时切换默认库或清理由维护者决定；历史 `SKU-TEE` 无结构化 color/size，作为已知例外豁免 | `data/local/` |

## 已补上不要再写成缺口

| 能力 | 代码 |
|---|---|
| 可信身份认证 | 不透明令牌仅存 SHA-256、bcrypt 成本 12、认证失败 fail-closed；见 `docs/25-trusted-auth.md` |
| 商家数据隔离 | DataScope `platform/shop` 两模式、过滤在 limit/offset 前、未绑定客服拒绝；见 `app/core/data_scope.py` |
| 启动迁移校验 | 启动只认 Alembic 0010，缺表/错版本/缺列直接拒绝，不在 live 库 `create_all` 或 stamp；见 `app/repositories/db.py:init_schema` |
| 分批召回完整性 | 未达目标持续补页直到覆盖全目录/确认无货，找够即停是成本控制；见 `app/agents/nodes/shopping.py` |
| 可解释规则精排 | 四特征归一化、未舍入加权和、纯函数确定性、可插拔抽象；见 `app/ranking/product.py` |
| 多轮状态机与导航 | 候选/已看/选中、稳定序号、换一个/上一个/对比/选择，跨轮恢复校验版本/会话/身份/商家范围；见 `docs/22-shopping-loop.md` |
| 售后幂等与状态机 | 每订单至多一个活跃单据、跨表互斥、累计退款不超额、非法流转拒绝、退货签收自动退款；见 `docs/26-aftersale-idempotency.md` |
| 导购前端 | 商品卡片、当前条件条、导航交互，普通消息不破坏；见 `frontend/src/components/` |
| SSE 校验后分段 | `ChatService.stream` 在答案完成校验后分段发送 |
| MCP 可执行 | `app/mcp/runtime.py`、`MCPToolProxy.execute` |
| 忠实度 LLM 自纠错 | `self_correct_answer` |

## 不计划用「简历堆料」补的

- Kafka / Celery：没有出站异步作业
- Elasticsearch：政策规模内存 BM25 足够
- LTR / 协同过滤 / 个性化：当前 5 商品 1 买家条件下训不出来，不伪造行为数据冒充学习效果
- 用词面 overlap 冒充 Cross-Encoder，或用 hash 向量冒充真实嵌入
- Unstructured Cloud / Transform MCP
- embedding 聚类丢掉近重复政策条款

## 下一阶段

1. 评测闭环：人工红队、LLM 合成用户、属性测试、故障注入、bug 回归库，形成发现→评测→归因→修复→回归→沉淀的受控闭环
2. 真实交易幂等（可选）：下单/支付/扣库存的幂等与防超卖，复用现有确认与 fail-closed 机制
3. Docker 恢复后补 PostgreSQL/Qdrant 迁移与全量测试
4. 定位并加固 e2e 偶发 flaky（SSE/事件循环进程级状态），避免 CI 偶发红

改行为时同步改本章、[10](10-evaluation.md) 与 [14 · 测评报告](14-evaluation-report.md)。README 只保留入口和实测表。

---

上一章 [11](11-deployment.md) · [返回目录](README.md)
