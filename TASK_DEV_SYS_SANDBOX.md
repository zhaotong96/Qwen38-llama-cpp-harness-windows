# TASK_DEV_SYS_SANDBOX.md: Phase 6A 系统底层与执行安全加固

- **阶段目标**：Phase 6A 消除管道死锁、进程树孤儿脱壳、工作区越权写入与实现毫秒级三道门禁熔断
- **执行角色**：系统底层与执行安全工程师 (Systems & Runtime Security Engineer)
- **前置依赖**：Commit `2a9e4b1`（Phase 5B 已闭环，107 项单元测试全绿灯）
- **权威契约**：[SPEC.md](SPEC.md) §7（工具分发契约）、`report.md` §4（专家审计第四章：Windows 本地执行体与 3-Gate 熔断的进程树安全沙箱）
- **状态**：待领用 (Pending Assignment)

---

## 一、任务背景与工程公理

在当前架构中，Agent 通过 `core/tools.py::tool_shell` 及 `skills/` 调用 Windows 本地能力。专家审计报告第 4 章揭示了当前实现潜藏的两个操作系统级严重隐患：
1. **64 KB 匿名管道死锁（Pipe Buffer Deadlock）**：在 `while proc.poll() is None:` 等待期间未实时排空管道。当命令输出超过 Windows 内核缓冲区（4~64 KB）时，子进程 `pwsh.exe` 挂起等待读管道，父进程死等退出，导致 120 秒假死超时；
2. **孤儿孙进程脱壳（Orphan Processes）**：`taskkill /F /T` 无法追踪父进程已退出的深层后台孙进程（如 `cmake`, `git`, 后台编译器），打断后遗留孤儿进程继续锁定工作区句柄与端口。

**工程红线约束**：
- **KISS 与奥卡姆剃刀**：严禁引入 Docker、WSL 或任何第三方 Python 外部依赖（如 `psutil`、`pywin32`）；
- **纯标准库实现**：必须且仅能使用 Python 内置的 `ctypes.windll.kernel32` 与 `threading` 标准库完成 Win32 Job Object（作业对象）的封装。

---

## 二、工作清单与详细实现规范

### 1. `core/tools.py`：Win32 作业对象（Job Object）内核沙箱
- 仅在 `sys.platform == "win32"` 时激活；
- 显式声明 64 位 `HANDLE` 类型签名（`argtypes` 与 `restype`），杜绝 64 位指针在默认 32 位 `c_int` 下的高位截断；
- 构造 `_JOBOBJECT_EXTENDED_LIMIT_INFORMATION` 结构体：
  - 置位 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x00002000)`：保证句柄关闭时内核强制杀死所有子孙进程；
  - 置位 `JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION (0x00000400)`：消除子进程崩溃弹出 WerFault 对话框挂起流程；
  - 限制内存上限 `JobMemoryLimit = 4 * 1024 * 1024 * 1024`（4 GiB）；
  - 限制最大活跃并发进程数 `ActiveProcessLimit = 64`（阻断 Fork 炸弹）；
  - **严禁** 置位 `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK (0x00001000)`，防止子孙进程静默脱壳。

### 2. `core/tools.py::tool_shell`：异步双管道排空与生命周期闭环
- 创建匿名 Job Object：`h_job = _create_kill_on_close_job()`；
- `subprocess.Popen` 创建子进程后，立即调用 `AssignProcessToJobObject(h_job, proc._handle)`；
- **消除管道死锁**：启动两个轻量级守护线程（`threading.Thread`），实时读取 `proc.stdout` 与 `proc.stderr` 并存入 `stdout_chunks` / `stderr_chunks` 列表；
- 在循环等待退出期间，每隔 50ms 轮询 `_is_active_execution_aborted()`：
  - 若检测到 `abort.flag` 或达到超时时间，立即调用 `TerminateJobObject(h_job, 1)`；
- 在 `finally` 块中始终调用 `CloseHandle(h_job)`，确保任何异常（包括 Python 自身被杀）均由 Windows NT 内核自动连根拔除整个子进程树。

### 3. 全局执行体打断对齐与超时加固
- [skills/git.py](skills/git.py)：将无超时的 `subprocess.run` 增加 `timeout=60`，并在执行前后增加 `_is_active_execution_aborted()` 检查；
- `core/tools.py::tool_search`：对 `rg` 调用增加超时与打断感知。

### 4. 工作区写入边界加固（`core/tools.py::writable_path`）
- 审计 `writable_path(path)`：确保所有写入类操作（`write_file`, `patch` 等）严格限制在 `ROOT`（当前工作区）以及 `tempfile.gettempdir()` 系统临时目录内，阻断任意绝对路径覆盖系统关键目录的风险。

### 5. 编写单元测试 `tests/test_sandbox.py`
- 测试 1：测试超长输出（如 200 KB 连续打印），验证异步双管道排空不发生 64 KB 死锁，瞬间返回；
- 测试 2：测试多层孙进程派生并触发 Gate 3 打断，验证进程树被连根清理，无孤儿进程残留；
- 测试 3：测试工作区越权写入拒绝。

---

## 三、验收清单

- [ ] `core/tools.py` 使用标准库 `ctypes` 完整实现 Win32 Job Object 与管道双线程排空；
- [ ] 执行输出 >64 KB 的长文本命令能在 1 秒内正常返回，无管道缓冲区死锁；
- [ ] 打断正在运行的复杂批处理/多级子进程，验证子进程树全部退出，无孤儿进程脱壳；
- [ ] `skills/git.py` 与 `tool_search` 接入打断与超时防护；
- [ ] `writable_path` 严格拦截工作区外的敏感路径写入；
- [ ] 新增 `tests/test_sandbox.py` 且通过，全量单元测试（原有 107 项 + 新增测试）全部绿灯；
- [ ] 零新增第三方依赖，`requirements.txt` 保持 4 项；
- [ ] 回填文末“四、验收回执”。

---

## 四、验收回执（执行人填写）

*(待执行工程师完成后回填)*
