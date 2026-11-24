# 登录前端接入验收

本批只改 frontend。没有新增运行时或开发依赖，不修改后端，不调用模型，不创建 Git 提交。

## 行为

- 登录使用 `/api/v1/auth/login`，只在内存持有令牌；刷新重新登录。
- JSON 与 fetch SSE 共用请求层，使用 Bearer 请求头；业务不再发送 user_id。
- HTTP 401、流内终止性 401 共用失效逻辑。403 显示权限不足；网络错误和 503 保留身份。
- 退出先清空本机身份并取消业务请求，再等待独立的服务端撤销请求；撤销未确认会提示。
- 登录版本防止旧账号的迟到 401、登录结果和退出失败影响新账号。
- 工作区按登录版本重新挂载；operator 才显示管理入口，最终权限仍由后端判断。
- 管理页不再填写 x-admin-token 或 reviewer。
- 缓存使用 `casedesk-web:v2:<encoded-user-id>`，主题使用其 `:theme` 子键。
- 旧全局缓存保持原样，不迁移、不删除、不认领；待确认动作不从缓存恢复。

## 验证

基线：`npm.cmd test`，2 文件、3 项通过；`npm.cmd run build` 通过。

最终测试覆盖以下文件，使用离线 fetch 模拟，没有真实服务或模型调用：

|测试文件|覆盖|
|---|---|
|src/lib/auth.test.ts|登录状态、订阅取消、版本竞争、内存凭证|
|src/lib/api.test.ts|Bearer、无凭证登录、HTTP 401/403/503、网络错误、迟到响应、写请求不重放、退出、流内错误、取消流|
|src/lib/storage.test.ts|账号分区、编码、旧缓存不认领、过期写入隔离、临时确认动作过滤、存储异常|
|src/lib/sse.test.ts|UTF-8 分片、CRLF、无尾换行、终止性 401|
|src/components/LoginPanel.test.tsx|空密码与遮蔽、禁用自动补全、登录中状态、失效提示|
|src/auth/AuthProvider.test.tsx|真实 Provider 动作逻辑、统一登录失败提示、立即清理身份、登录/退出竞争|
|src/App.test.tsx|登录入口、角色入口、跨账号会话不展示|
|src/components/MessageView.test.tsx|原有答复及引用展示回归|

页面测试使用 React 服务端渲染检查结构及 Provider 动作，不代替浏览器交互或真实后端联调。
测试与生产构建结果在本批交付回复中记录。前后端联调留给下一步验收。

## 改动文件

新增：src/lib/auth.ts、src/auth/AuthProvider.tsx、src/components/LoginPanel.tsx；
src/lib/authTestSupport.ts、src/lib/auth.test.ts、src/lib/api.test.ts、src/lib/storage.test.ts、
src/auth/AuthProvider.test.tsx、src/components/LoginPanel.test.tsx、src/App.test.tsx；本文件。

修改：src/main.tsx、src/App.tsx、src/lib/api.ts、src/lib/sse.ts、src/lib/sse.test.ts、
src/lib/storage.ts、src/hooks/useChat.ts、src/components/ReceptionPanel.tsx、
src/components/PlatformPanel.tsx、src/types.ts、src/styles.css。

## 边界

- 本批不迁移数据库、不初始化账号、不启动后端。
- 刷新页面只清除本机内存令牌，不等于服务器已撤销该会话；服务端 TTL 仍有效。
- 退出时若网络失败，本机已退出，但不能声称服务端撤销成功。
- 浏览器密码管理器是否遵从 autocomplete=off 由浏览器决定；应用不持久化密码。
- UI 隐藏管理入口不构成授权，后端认证与资源归属检查保持有效。
