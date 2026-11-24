# 接待生命周期：后端第一阶段

一轮模型执行结束，不代表整次客服接待结束。新增 receptions 表独立记录接待，
session_id 关联原有 sessions；结束不删除消息、不撤销订单或已提交的售后申请。

## 状态和接口

- active：仍可继续咨询，模型等待消息时不持续运行。
- closed / resolved：用户明确标记已解决。
- closed / user_closed：用户主动结束，但未声明已解决。
- closed / timeout：达到无消息超时，不可计为已解决。

`GET /api/v1/sessions/{session_id}/reception?user_id=...` 返回接待状态。
`POST /api/v1/sessions/{session_id}/close` 请求体为
`{"user_id":"u_demo","reason":"resolved"}` 或 `user_closed`。
客户端不能声明 timeout；由服务端检测。重复关闭保留第一次结束原因和时间。

发送聊天消息时检查接待是否仍有效，关闭后返回409，需要省略session_id创建新咨询。
默认 RECEPTION_IDLE_SECONDS=1800，最小60秒，可通过环境变量配置。
只有消息保存刷新活动时间，读取状态不会延长接待。
本阶段使用访问时惰性过期：GET状态或尝试聊天时发现超时，写入关闭状态；
closed_at记为实际到期时刻，不是检查时刻。没有后台定时扫描和主动推送。

## 数据与边界

新环境启动的create_all会创建新表；已有演示库不改原有表和历史消息。
受Alembic管理的环境可运行 `python -m alembic upgrade head`，新增迁移0003。
旧会话首次使用新功能时按原updated_at初始化，可能因已过期直接关闭。

沿用现有演示user_id归属模型并核对会话所有者，这不等于生产级认证；不能把
客户端自报user_id视为已认证身份。生产部署需要从可信认证上下文取用户身份。
当前没有实现关闭请求与正在执行工具的跨请求锁/中断机制，也没有并发关闭线性化保证。
已经执行的业务动作不会因关闭会话自动撤销。
## 第二阶段：结束按钮与评价

前端接入“已解决”“结束咨询”，生成中禁用结束操作，关闭后隐藏输入框并引导新建咨询。
每次载入接待和空闲时每30秒查询状态，以发现超时；服务端仍是惰性过期。
接待关闭后展示可选评价表单：满意度1—5分、是否解决（独立必选）、原因（可选，最多1000字）。
新表reception_feedback按session_id唯一记录，关联最后一条助手消息ID，便于回查整段接待。
这不是逐条回答评分，尚未记录每次邀评展示事件，也未生成bad case或自动优化。

`POST /api/v1/sessions/{session_id}/feedback`接收user_id、score、resolved、reason。
接待未关闭返回409；归属不匹配返回404；非法评分返回422。
相同评价重复提交幂等，不同评价不覆盖第一次记录，返回409。
GET接待状态包含已保存feedback，页面刷新后可恢复已评价状态。
评价的resolved不修改end_reason，超时关闭即使用户随后反馈已解决，也仍是timeout。
Alembic新增0004迁移，沿用启动create_all的演示库可自动创建新表。
