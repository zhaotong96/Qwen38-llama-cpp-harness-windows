P1 的“动态槽位探针与超时放缩”已补齐并验证：

- `core/subagent_runner.py`：派发前探测 `/slots`，Slot 1 忙时自动降级到空闲非 0 槽；全部忙则返回 `SUBAGENT_SLOTS_BUSY` 且不占锁；注入 `subagent_client` 时跳过探测。
- `core/agent.py`：`chat()`/`_summarize()` 按 `300 + token_estimate/400` 动态放大读取超时；非前缀继承压缩路径同样做槽位探测。
- 新增测试：`test_config.py` 5 项槽位探测、`test_subagent.py` 3 项降级路由、`test_agent.py` 超时放缩与第 4 节插入位置边缘修复。

验证：179 tests OK、benchmark `GCR/FFR=100%`、`SCCS=1.000`、隐私门禁 OK、`requirements.txt` 仍 4 项、`web/` 无构建文件。