# 可信登录身份：后端实现与验收边界

本批只改后端、CLI、评测直调和测试。前端文件保留原样，因此旧工作台尚不能登录，
不带 Bearer 的业务请求会返回 401。不得为了兼容旧前端恢复匿名访问。

## 数据与初始化

新增 users 和 auth_session 两张表，迁移 0008，前置 0007。旧订单、会话、检查点的
user_id 不修改、不回填，不新增业务表到 users 的外键。普通 create_all 排除认证表。

**已有库注意：** 如果业务表由 create_all 建立而未登记 Alembic 版本，先备份并核对
真实结构，再单独确认基线登记；不能盲目 stamp 或直接重跑初始迁移。本次实现不自动
升级真实业务库，也不自动创建实际账号。

对于已经具有正确迁移基线的库，使用项目解释器运行 `-m alembic upgrade head`。
Alembic 使用运行时 DATABASE_URL；测试可以显式提供连接，避免误迁默认本地库。
认证表缺失或迁移版本不符时启动失败，不提供匿名服务。

随后执行 `python scripts/seed_users.py`。默认用 getpass 隐藏输入；可以从环境变量
CASEDESK_SEED_BUYER_PASSWORD、CASEDESK_SEED_AGENT_PASSWORD、CASEDESK_SEED_OPERATOR_PASSWORD
读取。预置 u_demo/buyer、u_service_agent/service_agent、u_operator/operator。
现有账号不重置密码、不改角色或状态，冲突只报告。`--with-demo-data` 显式调用旧演示
数据加载函数；重复执行不覆盖旧商品或订单。密码不放入命令参数、代码或日志。

## 令牌与接口

登录：POST /api/v1/auth/login，字段 user_id、password。用户名沿用现有 user_id。
密码使用 bcrypt 自动加盐，成本 12，通过线程池执行；UTF-8 长度超过72字节明确拒绝。
不存在的账号执行虚拟哈希检查。登录尝试限制与4个哈希并发名额均为进程内保护，
不声称已经实现分布式严格登录限流。

原令牌为 cds_ 加 secrets.token_urlsafe(32)，仅在登录成功响应返回一次。
数据库仅存 SHA-256 摘要；这不是密码散列方案。会话默认8小时绝对TTL，不自动续期。
退出 POST /api/v1/auth/logout 撤销当前会话；GET /api/v1/auth/me 返回安全用户资料。
operator 使用 POST /api/v1/admin/users/{user_id}/disable 停用账号，事务内撤销全部会话。
禁止停用自己或最后一个 active operator。

普通7个业务入口只接受 Authorization: Bearer；客户端 user_id 仍可传入但完全忽略，
OpenAPI 标为 deprecated。内部用户身份由认证生成；角色和凭证不进入模型消息、
Agent State、检查点或工具参数。当前角色不赋予跨买家资源访问权，merchant_id 尚未启用。

## 管理兼容顺序

1. 提供 Bearer 必须验证；无效返回401，角色不是operator返回403，不回退其他方式。
2. 无Bearer且正确x-admin-token，映射system:admin-token。
3. 无任何凭证、未配置管理员令牌、且development/testing，保留原管理免令牌行为，
   身份为system:dev-admin；仅限管理路由。无效的已提交凭证不能触发此例外。
4. production无凭证拒绝。审核人永远来自实际operator或兼容系统身份。

## 运行中失效

数据库读取异常返回拒绝/503，不使用认证成功缓存。每次请求查数据库，工具前和输出前复查，
长请求/长流每1秒检查。SSE开始前401使用HTTP状态；HTTP200发出后使用终止性error事件，
包含status_code=401、error_code=UNAUTHORIZED和trace_id，随后取消任务，不再发送成功答案。
认证异常不转换为工具失败，不进入工具回退。服务初始会话/购物条件在长模型调用前提交，
避免SQLite写事务阻塞另一连接退出。

撤销提交后的后续校验立即失败。已经完成的动作不撤回；已通过校验的在途单次操作可能完成。
这不构成撤销与业务写入的严格事务串行化，也不保证取消外部系统已经受理的动作。

## CLI与评测

`casedesk-cli login --base-url http://127.0.0.1:8002 --user-id u_demo` 登录并进入交互。
`--local` 是显式本地模式，也必须使用相同认证服务登录。API不可达不自动绕过到本地。
令牌只在进程内存保存；/login、/logout 管理身份，/new只重开业务会话。
不支持明文密码参数，认证失败后不能继续调用业务。

eval.runner 的E2E分支要求 CASEDESK_EVAL_TOKEN，经过同一认证服务；题目user_id不能声明身份。
其可能调用真实模型，运行前必须独立确认预算。本批不运行该付费评测。独立组件评测不强制登录。

## 测试与凭证保护

新增auth service/repository/migration/seed/API/user_binding/stream/CLI/redaction测试。
E2E用例通过0008迁移建立临时库，实际登录签发令牌，模型及外部依赖保持离线。
第三方SSE库的进程全局退出事件在各TestClient测试之间隔离，防止跨事件循环污染。
业务组件单元测试可显式注入测试身份检查函数，不存在生产skip_auth开关。

日志在JSON序列化前递归脱敏，覆盖参数、额外字段、异常和访问日志；访问日志不记录Query，
登录校验错误不返回原input。数据库引擎隐藏SQL参数。登录成功之外的响应不返回原凭证或摘要。

本机Docker处于手动暂停状态，PostgreSQL尚未实测；SQLite结果不替代PostgreSQL并发/迁移结论。
保留原工作区备份和逐文件哈希。回退只能撤销本批增量，不能重置仓库或丢弃旧未提交文件；
真实环境不能以恢复匿名访问作为安全回退。


## 后端验收记录（2026-11-19）
基线：225 passed，1条既有LangGraph依赖警告。最终：259 passed，0 failures，0 errors，0 skipped；仍只有1条既有警告。
app+eval语句覆盖率：74.58%，高于CI要求的70%。Ruff check .通过；mypy app通过（136个源码文件）；uv lock --check通过。
新依赖仅bcrypt 5.0.0，原锁文件已有包版本未变化。26个备份中的前端源码/配置文件哈希一致，真实app.db哈希一致。
全部实现留在工作区，没有git commit。未运行付费评测，也未迁移真实业务库或初始化实际密码。
原始证据：`C:\Users\7000\AppData\Local\Temp\CaseDesk-auth-validation-20261119\pytest.xml`、`C:\Users\7000\AppData\Local\Temp\CaseDesk-auth-validation-20261119\coverage.json`、`C:\Users\7000\AppData\Local\Temp\CaseDesk-auth-validation-20261119\change_manifest.json`。
备份及原文件哈希清单：`C:\Users\7000\AppData\Local\Temp\CaseDesk-auth-before-20261119-162647`。
双实例验证使用进程内两个独立FastAPI应用与独立数据库引擎，SQLite文件共享；不等于跨机器/独立服务器验证。PostgreSQL尚未实测：Docker Desktop手动暂停，未擅自恢复。
### 十类要求的测试证据
|要求|证据|
|---|---|
|正确/错误登录|[tests/e2e/test_auth_api.py](D:/code/CaseDesk/tests/e2e/test_auth_api.py)：test_login_logout_wrong_credentials_and_safe_responses|
|七入口未登录401|[tests/e2e/test_auth_api.py](D:/code/CaseDesk/tests/e2e/test_auth_api.py)：test_all_business_endpoints_require_bearer|
|篡改user_id无效|[tests/e2e/test_auth_user_binding.py](D:/code/CaseDesk/tests/e2e/test_auth_user_binding.py)：test_client_identity_is_ignored_and_foreign_resources_stay_private|
|买家管理接口403|[tests/e2e/test_auth_api.py](D:/code/CaseDesk/tests/e2e/test_auth_api.py)：test_management_auth_priority_and_disable_protections|
|退出旧令牌401|[tests/e2e/test_auth_api.py](D:/code/CaseDesk/tests/e2e/test_auth_api.py)：test_login_logout_wrong_credentials_and_safe_responses|
|过期令牌401|[tests/e2e/test_auth_api.py](D:/code/CaseDesk/tests/e2e/test_auth_api.py)：test_expired_token_and_query_credentials_are_rejected_without_echo|
|伪造令牌401|[tests/e2e/test_auth_cli.py](D:/code/CaseDesk/tests/e2e/test_auth_cli.py)：test_cli_http_and_local_require_real_valid_credentials|
|跨用户检查点恢复拒绝|[tests/e2e/test_auth_user_binding.py](D:/code/CaseDesk/tests/e2e/test_auth_user_binding.py)：test_client_identity_is_ignored_and_foreign_resources_stay_private|
|登录后原聊天/反馈/检查点回归|[tests/e2e/test_shopping_recovery.py](D:/code/CaseDesk/tests/e2e/test_shopping_recovery.py)：以及test_chat_api.py、test_reception_api.py、test_shopping_multiturn.py|
|日志/响应不泄露凭证|[tests/unit/test_auth_redaction.py](D:/code/CaseDesk/tests/unit/test_auth_redaction.py)：以及test_auth_api.py中的登录与校验错误响应检查|

对抗证据还包括：test_auth_repository.py的双引擎撤销、全量停用、最后operator并发保护及非SQLAlchemy异常拒绝；test_auth_api.py的双HTTP应用撤销；test_auth_stream.py的长流/长JSON请求撤销、数据库故障拒绝；test_executor_fallback.py的认证失败不回退；test_auth_migration.py的0007升级/约束/降级。

### 本批逐文件清单（相对编码前快照）
下表只包含本批实际变化，未将原有修改但本批未触碰的文件计入。

|状态|文件|
|---|---|
|增量修改|[.env.example](D:/code/CaseDesk/.env.example)|
|增量修改|[README.md](D:/code/CaseDesk/README.md)|
|增量修改|[alembic/env.py](D:/code/CaseDesk/alembic/env.py)|
|新增|[alembic/versions/0008_trusted_auth.py](D:/code/CaseDesk/alembic/versions/0008_trusted_auth.py)|
|增量修改|[app/agents/nodes/executor.py](D:/code/CaseDesk/app/agents/nodes/executor.py)|
|新增|[app/api/auth_route.py](D:/code/CaseDesk/app/api/auth_route.py)|
|增量修改|[app/api/dependencies.py](D:/code/CaseDesk/app/api/dependencies.py)|
|增量修改|[app/api/routes/admin.py](D:/code/CaseDesk/app/api/routes/admin.py)|
|新增|[app/api/routes/auth.py](D:/code/CaseDesk/app/api/routes/auth.py)|
|增量修改|[app/api/routes/chat.py](D:/code/CaseDesk/app/api/routes/chat.py)|
|增量修改|[app/api/routes/platform.py](D:/code/CaseDesk/app/api/routes/platform.py)|
|增量修改|[app/cli/client.py](D:/code/CaseDesk/app/cli/client.py)|
|增量修改|[app/cli/commands.py](D:/code/CaseDesk/app/cli/commands.py)|
|增量修改|[app/cli/render.py](D:/code/CaseDesk/app/cli/render.py)|
|增量修改|[app/config/settings.py](D:/code/CaseDesk/app/config/settings.py)|
|新增|[app/core/auth_context.py](D:/code/CaseDesk/app/core/auth_context.py)|
|增量修改|[app/core/error_codes.py](D:/code/CaseDesk/app/core/error_codes.py)|
|增量修改|[app/core/exceptions.py](D:/code/CaseDesk/app/core/exceptions.py)|
|增量修改|[app/core/security.py](D:/code/CaseDesk/app/core/security.py)|
|增量修改|[app/main.py](D:/code/CaseDesk/app/main.py)|
|增量修改|[app/observability/logging.py](D:/code/CaseDesk/app/observability/logging.py)|
|新增|[app/repositories/auth_repository.py](D:/code/CaseDesk/app/repositories/auth_repository.py)|
|增量修改|[app/repositories/db.py](D:/code/CaseDesk/app/repositories/db.py)|
|增量修改|[app/repositories/models.py](D:/code/CaseDesk/app/repositories/models.py)|
|增量修改|[app/repositories/session_repository.py](D:/code/CaseDesk/app/repositories/session_repository.py)|
|新增|[app/schemas/auth.py](D:/code/CaseDesk/app/schemas/auth.py)|
|增量修改|[app/schemas/chat.py](D:/code/CaseDesk/app/schemas/chat.py)|
|增量修改|[app/schemas/platform.py](D:/code/CaseDesk/app/schemas/platform.py)|
|增量修改|[app/schemas/reception.py](D:/code/CaseDesk/app/schemas/reception.py)|
|新增|[app/services/auth_service.py](D:/code/CaseDesk/app/services/auth_service.py)|
|增量修改|[app/services/chat_service.py](D:/code/CaseDesk/app/services/chat_service.py)|
|增量修改|[app/services/container.py](D:/code/CaseDesk/app/services/container.py)|
|新增|[docs/25-trusted-auth.md](D:/code/CaseDesk/docs/25-trusted-auth.md)|
|增量修改|[eval/runner.py](D:/code/CaseDesk/eval/runner.py)|
|增量修改|[pyproject.toml](D:/code/CaseDesk/pyproject.toml)|
|新增|[scripts/seed_users.py](D:/code/CaseDesk/scripts/seed_users.py)|
|新增|[tests/__init__.py](D:/code/CaseDesk/tests/__init__.py)|
|新增|[tests/auth_support.py](D:/code/CaseDesk/tests/auth_support.py)|
|增量修改|[tests/conftest.py](D:/code/CaseDesk/tests/conftest.py)|
|新增|[tests/e2e/test_auth_api.py](D:/code/CaseDesk/tests/e2e/test_auth_api.py)|
|新增|[tests/e2e/test_auth_cli.py](D:/code/CaseDesk/tests/e2e/test_auth_cli.py)|
|新增|[tests/e2e/test_auth_stream.py](D:/code/CaseDesk/tests/e2e/test_auth_stream.py)|
|新增|[tests/e2e/test_auth_user_binding.py](D:/code/CaseDesk/tests/e2e/test_auth_user_binding.py)|
|增量修改|[tests/e2e/test_chat_api.py](D:/code/CaseDesk/tests/e2e/test_chat_api.py)|
|增量修改|[tests/e2e/test_data_evaluation_platform.py](D:/code/CaseDesk/tests/e2e/test_data_evaluation_platform.py)|
|增量修改|[tests/e2e/test_reception_api.py](D:/code/CaseDesk/tests/e2e/test_reception_api.py)|
|增量修改|[tests/e2e/test_shopping_multiturn.py](D:/code/CaseDesk/tests/e2e/test_shopping_multiturn.py)|
|增量修改|[tests/e2e/test_shopping_recovery.py](D:/code/CaseDesk/tests/e2e/test_shopping_recovery.py)|
|新增|[tests/integration/test_auth_migration.py](D:/code/CaseDesk/tests/integration/test_auth_migration.py)|
|新增|[tests/integration/test_auth_repository.py](D:/code/CaseDesk/tests/integration/test_auth_repository.py)|
|新增|[tests/integration/test_auth_seed.py](D:/code/CaseDesk/tests/integration/test_auth_seed.py)|
|增量修改|[tests/integration/test_reception.py](D:/code/CaseDesk/tests/integration/test_reception.py)|
|增量修改|[tests/unit/test_action_confirmation.py](D:/code/CaseDesk/tests/unit/test_action_confirmation.py)|
|新增|[tests/unit/test_auth_redaction.py](D:/code/CaseDesk/tests/unit/test_auth_redaction.py)|
|新增|[tests/unit/test_auth_service.py](D:/code/CaseDesk/tests/unit/test_auth_service.py)|
|增量修改|[tests/unit/test_executor_fallback.py](D:/code/CaseDesk/tests/unit/test_executor_fallback.py)|
|增量修改|[tests/unit/test_product_execution_guard.py](D:/code/CaseDesk/tests/unit/test_product_execution_guard.py)|
|增量修改|[uv.lock](D:/code/CaseDesk/uv.lock)|
