# CaseDesk 标准 MCP stdio 演示

本次接通一个完整而有限的工具链：CaseDesk → 官方 MCP Python SDK 客户端
→ 独立 stdio 服务进程 → 营业时间工具。业务数据为固定演示数据，不代表真实商家系统。

## 安装与运行

在项目根目录，使用项目 Python 3.12 虚拟环境：

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8002
```

`MCP_ENABLED=true`（默认）会在应用构建时启动服务、初始化并发现工具；
不需要手动启动服务，也不占用 HTTP 端口。关闭该设置会同时关闭新旧 MCP 演示工具。
启动发现失败会明确报错，不会悄悄退回旧版营业时间模拟。

在工作台问“周末客服几点上班？”可观察工具调用。模型是否选择工具受现有路由、
规划和模型输出影响；下面的离线测试直接验证协议链路，无需 DeepSeek 调用。

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_mcp_stdio.py -q
```

## 业务与源码顺序

1. `app/mcp/server.py`：FastMCP 提供 `mcp_store_hours`。
   参数 `day` 可为 `all`（默认）、`weekday`、`weekend`。
2. `app/mcp/client.py`：使用当前 `sys.executable` 启动服务脚本的绝对路径，
   经 `initialize`、`tools/list` 获取服务端 Schema；经 `tools/call` 获取结果。
   不依赖当前工作目录或全局 Python，也不把应用的 DeepSeek 密钥传入子进程。
3. `app/mcp/stdio_adapter.py`：将发现的名称、描述、原始 inputSchema 提供给
   Planner；按该 Schema 校验参数，将 ToolResult 交回现有 Executor。
4. `app/services/container.py`：发现并注册工具，拒绝同名覆盖。

默认查询返回 `weekday=09:00-21:00`、`weekend=10:00-18:00`、
`night_refund_review=false`、`source=mcp_stdio_demo`。
指定 day 时额外返回所选时段的 `hours`。

## 生命周期与失败处理

每次发现/调用分别启动一个短生命周期子进程，在同一 async 任务内完成初始化、
请求和关闭，避免跨 FastAPI lifespan/请求任务共享 SDK 上下文。
操作默认超时 15 秒，含握手；SDK 上下文负责关闭管道和清理子进程。
连接失败、超时、协议失败、`isError=true` 转为 `ToolExecutionError`；
模型参数不符合发现的 Schema 时转为 `InvalidRequestError`。
现有 Executor 对营业时间读工具的知识库回退仍适用。
stdout 专用于协议消息，服务日志写 stderr。

## 实现边界

- 本次使用固定的官方 SDK v1.26.0 和其支持的协议协商，不宣称覆盖所有 MCP 版本和能力。
- 仅营业时间走标准 MCP；旧 `runtime.py` 中的本地营业时间函数仅为兼容旧测试保留，
  应用注册不再使用它。
- 仓库截单仍为本地模拟；`MCP_REMOTE_URL` 仍是旧自定义 HTTP 接口，不能填标准 MCP URL。
- 未实现远程 Streamable HTTP、SSE MCP 传输、OAuth、常驻连接池或多服务配置。
  聊天 SSE 和本次 MCP stdio 是两个独立的通道。
- README 原版结构、截图、许可证和上游归属不作改动。

SDK 参考：https://py.sdk.modelcontextprotocol.io/v1/
