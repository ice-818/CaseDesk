# Qdrant 向量检索实测证据

- 日期：2026-11-25
- 范围：在独立 Qdrant 容器上验证 ingest 真实写入、集合配置、三层 scope 过滤、HTTP 语义检索命中与故障回退，不修改任何已提交代码。

## 1. 目标

项目默认在无向量数据库时使用内存向量存储（`InMemoryVectorStore`）。本次实测用于证明配置 `QDRANT_URL` 后，知识入库真正走 Qdrant（而非内存），语义检索与数据隔离在专业向量数据库上成立，并验证 Qdrant 故障时的容错回退。

## 2. 环境

| 组件 | 版本 / 配置 |
| --- | --- |
| Docker Engine | 29.4.0 |
| 镜像 | `qdrant/qdrant:v1.12.5` |
| 容器名 | `casedesk-qdrant-verify` |
| 端口映射 | 本地 `16333` → 6333（HTTP），本地 `16334` → 6334（gRPC） |
| 集合名 | `cs_knowledge_scope_v1`（`qdrant_collection` + `_scope_v1`） |
| 应用 | 本地 `.venv` 启动 uvicorn，端口 `8013`，`DEEPSEEK_API_KEY=""`，DB 用 SQLite 演示库 |

环境变量：`QDRANT_URL=http://127.0.0.1:16333`

### 网络问题与处理（如实记录）

免费代理节点不稳定，拉取过程中出现两次 `EOF` / SSL 错误。经 Clash 控制接口重新测量节点延迟并切换到可用节点后，镜像成功拉取。离线嵌入维度经核实为 `settings.embedding_dim = 512`，与集合尺寸一致，未出现维度不匹配。

## 3. 应用接入确认

`/api/v1/ready` 返回向量存储依赖为 `QdrantVectorStore`，证明启动时 ingest 成功写入 Qdrant，未回退内存。

## 4. 直查与黑盒验证（11/11 通过）

使用 qdrant-client 直连核对，再通过 httpx 发起真实检索。

| # | 验证点 | 结果 |
| --- | --- | --- |
| 1 | 集合 `cs_knowledge_scope_v1` 已创建 | PASS |
| 2 | 向量维度为 512 | PASS |
| 3 | 距离度量为 Cosine | PASS |
| 4 | 已写入知识点（136 个点） | PASS |
| 5 | payload 含 `policy_layer`，覆盖 platform / category / merchant 三层 | PASS |
| 6 | payload 含 scope 与内容字段（chunk_id、merchant_id、applicable_category、content、corpus_fingerprint） | PASS |
| 7 | 平台层点存在（81 个）且 merchant_id 为空 | PASS |
| 8 | 按 `policy_layer=platform` 过滤仅返回平台层点 | PASS |
| 9 | 买家登录返回 200 | PASS |
| 10 | 知识库检索（"保修多久"）返回 200 且有回答 | PASS |
| 11 | 检索带来引用来源（citations） | PASS |

### 三层 scope 机制

点 payload 同时包含 `policy_layer`、`merchant_id`、`applicable_category`，对应平台层、类目层、商户层。`QdrantVectorStore._filter` 按当前身份构造 `platform / category / merchant` 三个分支的 OR 过滤，并强制 `policy_scope_version=1` 与 `corpus_fingerprint` 匹配，实现跨租户/跨店隔离。

## 5. 故障回退验证

停止 Qdrant 容器后再次发起检索：

- 应用仍返回完整回答（长度 464）与引用（1 条），未返回 5xx。
- 机制：`QdrantVectorStore.search` 捕获连接异常后，回退到 ingest 时同步维护的 `InMemoryVectorStore`（`_fallback`），保证向量数据库故障时服务不中断。

## 6. 结论

- 知识入库在真实 Qdrant 上完成集合创建、点写入（136 点）与三层 scope 过滤，语义检索经 HTTP 端到端命中并带来引用。
- Qdrant 故障时确定性回退内存，验证了降级容错。
- 本次为可移植性验证，不做性能压测，不报告 QPS / TPS。

## 7. 回滚

- 停止并删除容器：`docker rm -f casedesk-qdrant-verify`（数据随容器删除）。
- 停止 uvicorn，删除临时验证脚本 `tmp_qdrant_verify.py`。
- 已提交代码与 SQLite 数据库在整个过程中未被修改。
