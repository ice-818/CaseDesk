# BUG-004：sse_starlette 进程级退出事件跨 TestClient 事件循环泄漏，造成偶发 flaky

| 字段 | 内容 |
| --- | --- |
| bug_id | BUG-004 |
| 标题 | `sse_starlette` 把优雅退出事件 `AppStatus.should_exit_event` 保存在类级（进程级），首次 SSE 响应时绑定到该 TestClient 的事件循环；后续 TestClient 各自拥有新循环，复用这个已绑定旧循环的事件即抛 `RuntimeError: ... bound to a different event loop`，把本应稳定的 401/200 变成异常 |
| 严重级别 | P2（测试稳定性/CI 可靠性问题，不影响业务逻辑、无越权或资金风险；但会让全量测试偶发误红，故单独修复并加确定性回归） |
| 归因层 | 第三方库进程级状态 + 测试事件循环隔离 |
| 依赖版本 | sse-starlette 2.4.1，anyio 4.16.0，starlette 0.46.2 |
| 回归用例 | `tests/e2e/test_sse_event_loop_isolation.py::test_sse_appstatus_event_does_not_leak_across_event_loops` |
| 发现方式 | 带覆盖率全量运行时偶发 `test_all_business_endpoints_require_bearer[post-/sessions/fake/feedback-body4]` 失败，独立复跑即过；通过确定性复现脚本定位到事件循环绑定 |

## 根因

`sse_starlette/sse.py` 在类上维护进程级状态：

```python
class AppStatus:
    should_exit = False
    should_exit_event: anyio.Event | None = None
```

`EventSourceResponse.__call__` 启动任务组，其中 `_listen_for_exit_signal` 在首次需要时执行
`AppStatus.should_exit_event = anyio.Event()`，该事件在被 `wait()` 时绑定到**当前运行的事件循环**。

而测试中每个 `TestClient(create_app())` 都通过独立 portal 在**新的事件循环**里运行：

1. 第一个 TestClient 的真实 SSE 流创建事件并绑定到循环 A；
2. 该 client 退出，循环 A 关闭，但类属性仍指向绑定循环 A 的事件；
3. 第二个 TestClient 在循环 B 里复用同一事件并 `wait()`，asyncio 检测到事件绑定的循环与运行循环不一致，
   抛 `RuntimeError: <asyncio.locks.Event ...> is bound to a different event loop`；
4. 异常经兜底处理器转成 500，断言 401 的用例因此偶发失败。其低概率性来自：仅当请求真正走到 SSE 任务组时才会 `wait()` 该事件，纯认证拦截的无 token 请求在多数情况下不触发它。

该问题为上游已知问题（sse-starlette issue #59），官方 PyPI 文档给出的解法即是在测试边界重置该事件。

## 修复

- 在 `tests/conftest.py` 新增对**所有测试**生效的 autouse fixture `_reset_sse_appstatus`，
  在每个测试的 setup 与 teardown 都强制 `AppStatus.should_exit_event = None`，
  使绑定到已关闭循环的事件不会跨 app 实例或测试边界泄漏。
- 移除原先仅在 e2e、依赖 `monkeypatch` 恢复（可能恢复成脏事件）的重置逻辑，改为不依赖恢复语义的显式重置。
- 不修改生产代码、不升级大版本、不新增运行时依赖。

## 修复后证据（通过）

- 新增确定性回归用例 1 项，包含：真实 SSE 产生循环绑定事件、在新循环上 `wait()` 必抛
  `bound to a different event loop` 的对照断言、重置后新 client SSE 正常返回 answer。
- 全量后端 **549 passed**（548 + 新增回归 1 项，约 152 秒）。
- 发布门禁 **4/4 PASS**，发布基线 `expected_total_tests` 同步更新为 549。

## 副作用预期（回归断言）

- 任一测试开始与结束时，进程级 `AppStatus.should_exit_event` 都处于干净的 `None`。
- 真实 SSE 流的 answer/token/done 事件序列与内容不受重置影响。
- 无业务写入、无认证口径变化，纯测试隔离修复。
