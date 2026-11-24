# 只读选购检查点与恢复

新增shopping_checkpoints表及0006迁移。演示库启动create_all可创建新表；Alembic管理环境需升级。
选购开始时保存条件、会话归属与新的checkpoint_id；每次工具返回后，在检查节点保存
工具结果、候选、已查SKU、核验结果、下一动作和步数，并提交事务，避免后续失败全部回滚。
仅选购读路径使用中间提交，不改变退款等写路径的事务边界。
正常结束记completed，捕获的执行异常记interrupted。进程强杀/断连可能保留running。
不保存进程本地monotonic时钟作为恢复时间依据。

## 使用方式

- 聊天JSON结果新增checkpoint_id；SSE started事件包含session_id和checkpoint_id。
- GET `/api/v1/sessions/{session_id}/shopping-checkpoint?user_id=...` 获取最近一次选购快照。
- POST `/api/v1/shopping-checkpoints/{checkpoint_id}/resume`，请求体`{"user_id":"u_demo"}`。
- 中断可恢复；running需距上次保存至少60秒，避免立刻重启可能仍在运行的请求；completed拒绝恢复。
- 校验会话归属、接待仍有效、原任务条件未被后续消息改变。结束接待、异用户、旧条件或完成记录不能恢复。
- 恢复创建新的执行检查点并记录parent_id；按保存条件重新搜索、重新核验价格与库存。
  原snapshot保留作诊断，不直接把旧verified当作当前事实。

这里实现的是应用层的持久化快照和安全重放式恢复，不是LangGraph原生checkpointer，
也不是从任意节点精确续跑。重新核验会重做只读工具调用，本轮次数和时间预算重新计算。
只有显式恢复API，没有后台调度器或前端恢复按钮；尚无跨实例恢复租约、严格单执行保证、
恢复次数总配额或检查点保留清理策略。同一只读任务可能被重复恢复，不支持写操作恢复。
JSON首次失败若尚未得到session_id，客户端目前难以定位该首次请求；SSE开始事件可提前拿到标识。

故障测试使用真实文件SQLite：工具核验后抛异常，确认快照已提交，重建服务容器，改变商品价格，
显式恢复并验证旧商品不再推荐。另测运行中拒绝、过期running可恢复、完成拒绝和归属隔离。
未执行真实操作系统进程强杀或PostgreSQL多实例故障测试。
