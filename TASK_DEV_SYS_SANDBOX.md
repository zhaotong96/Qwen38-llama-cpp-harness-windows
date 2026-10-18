# TASK_DEV_SYS_SANDBOX.md: Phase 6A 系统底层与执行安全加固

- **阶段目标**：Phase 6A 消除管道死锁、进程树孤儿脱壳、工作区越权写入与实现毫秒级三道门禁熔断
- **执行角色**：系统底层与执行安全工程师 (Systems & Runtime Security Engineer)
- **前置依赖**：Commit `2a9e4b1`（Phase 5B 已闭环，107 项单元测试全绿灯）
- **权威契约**：[SPEC.md](SPEC.md) §7（工具分发契约）、`report.md` §4（专家审计第四章：Windows 本地执行体与 3-Gate 熔断的进程树安全沙箱）
- **状态**：已完成验收（2026-10-18；执行：系统底层与执行安全工程师；回执见文末“四、验收回执”）

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

## 四、验收回执（执行人：系统底层与执行安全工程师；日期：2026-10-18）

**环境**：Windows 本机；`python` 3.14.2 嵌入式发行包（`python314._pth` 隔离模式，cwd 不入 `sys.path`）。任务书字面命令 `python -m unittest discover tests` 在本机必然 `ModuleNotFoundError: No module named 'core'`（嵌入包环境缺陷，与 5B 回执一致），实际采用 SPEC §11 规范形式 `python -m unittest discover -s tests -t .`。零新增第三方依赖：`git status` 证实改动仅 `core/tools.py`、`skills/git.py` 与新增 `tests/test_sandbox.py`，`requirements.txt` 保持 4 项未动。

### 1. Win32 Job Object 内核沙箱（`core/tools.py`）
- 纯 `ctypes.windll.kernel32` + `threading` 标准库实现，仅 `sys.platform == "win32"` 激活；
- 显式声明 64 位签名：`CreateJobObjectW` / `SetInformationJobObject` / `AssignProcessToJobObject` / `TerminateJobObject` / `CloseHandle` 全部设置 `argtypes`/`restype`（`wintypes.HANDLE`），杜绝句柄高位截断；
- `_JOBOBJECT_EXTENDED_LIMIT_INFORMATION` 置位：`KILL_ON_JOB_CLOSE (0x2000)` + `DIE_ON_UNHANDLED_EXCEPTION (0x400)` + `JOB_MEMORY` + `ACTIVE_PROCESS`；`JobMemoryLimit = 4 GiB`、`ActiveProcessLimit = 64`；**未**置位 `SILENT_BREAKAWAY_OK`；
- `subprocess.Popen` 后立即 `AssignProcessToJobObject(h_job, int(proc._handle))`；`finally` 恒 `CloseHandle(h_job)` —— 父进程任何死法都由内核连根拔除整棵子进程树。

### 2. 双管道排空与生命周期闭环（`core/tools.py`）
- 新增 `_run_process(argv, timeout)`：两个 daemon 排空线程实时消费 `proc.stdout`/`proc.stderr`（`readline` 循环），主循环每 50ms 轮询退出/`abort.flag`/超时；返回 `(status, stdout, stderr, returncode)`，`status ∈ ok|timeout|interrupted|spawn_error`；
- `tool_shell` 全量重构为经 `_run_process` 执行 `pwsh -NoProfile -Command`：打断 → `TerminateJobObject` 后返回既有 `[EXECUTION_INTERRUPTED_AND_IGNORED]` 文本；超时 → `error: timed out after 120s`；事前拒绝 `[EXECUTION_REJECTED]` 与危险命令 `SECURITY_BLOCK` 语义原样保留。
- **死锁消除实测**（旧实现 64 KB 管道写满即 120s 假死）：

| 场景 | 实测耗时 | 结果 |
| --- | --- | --- |
| 2000 行 × 100 字符（~200 KB stdout） | 0.376 s | exit=0，截断标注正常 |
| 单行 300 000 字符 | 0.219 s | exit=0 |
| stderr 50 KB | 0.269 s | exit=0 |

  三条全部 <1 s 返回，满足“1 秒内正常返回”验收线（时间探针脚本见 `C:\Users\zt\AppData\Local\Temp\opencode\timing_probe.py` 输出实录）。
- **孤儿孙进程实测**：真实 pwsh 派生 Hidden 孙进程链（写自身与孙进程 PID 落盘），Gate 3 打断后 `tool_shell` 在 ~0.1 s 内返回中断文本；直接子进程与孙进程 PID 经 Win32 `OpenProcess`/`GetExitCodeProcess` 复核全部退出，无孤儿残留。

### 3. 全局打断与超时对齐
- [skills/git.py](skills/git.py)：`subprocess.run` 增加 `timeout=60`（超时 → `error: git action timed out after 60s`），执行前后各一次 `execution_aborted()` 检查（命中 → 中断文本）；
- `core/tools.py` 新增公开探针 `execution_aborted()`（读取线程本地执行上下文，供 skills 复用）；内部 `_is_active_execution_aborted()` 语义不变；
- `tool_search`：`rg` 改经 `_run_process`（30 s 硬超时 + 打断感知）：打断 → 中断文本；超时 → `error: search timed out`；`rg` 缺失（spawn_error）自动回退原 pwsh `Select-String` 链路，原 `FileNotFoundError` 兜底语义等价保留。

### 4. 工作区写入边界（`writable_path`）
- 白名单两棵树：`ROOT`（工作区）与 `tempfile.gettempdir()`（系统临时目录），`Path.relative_to` 判定；越界 `raise PermissionError("write denied outside workspace and temp dir: ...")`；
- 实测：工作区路径、临时目录路径写入成功；`C:\Windows\System32\drivers\etc\sandbox_probe.txt` 拒绝且不落盘；临时目录父级逃逸路径拒绝且不落盘；
- 兼容性：`tool_write`（catch Exception 前缀 `error:`）与 `skills/patch.py`（已 catch PermissionError）无需改动；`tests/test_tools.py::PathTests` 原断言（`writable_path(tmp)` 等于 `Path(tmp).resolve()`）不破坏。

### 5. 测试与回归
- 新增 `tests/test_sandbox.py`（8 项）：
  - `PipeDrainTests` ×3：大 stdout / 巨型单行 / stderr 排空，断言 `exit=0`、截断标注、<10 s 返回（防死锁回归闸门）；
  - `AbortReapingTests` ×1：`IsolatedCase` 隔离任务根 + 真实 pwsh 孙进程链，Gate 3 打断后经 Win32 `GetExitCodeProcess` 断言父子 PID 全灭；
  - `WriteBoundaryTests` ×4：工作区/临时目录放行 + 系统目录/逃逸路径拒绝且不落盘。
- 命令与结果（原样实录）：
  - `python -m unittest discover -s tests -t .` → `Ran 115 tests in 23.589s — OK`（原有 107 项断言零改动 + 新增 8 项全绿）；
  - `python -m unittest discover -s tests -t . -p "test_sandbox.py"` → `Ran 8 tests in 1.405s — OK`。

### 6. 验收清单对照
- [x] `core/tools.py` 以标准库 `ctypes` 完整实现 Win32 Job Object 与双线程管道排空；
- [x] >64 KB 长输出 1 秒内正常返回（实测 0.219–0.376 s），无管道缓冲区死锁；
- [x] 打断多级子进程批处理：进程树全部退出，无孤儿脱壳；
- [x] `skills/git.py` 与 `tool_search` 接入打断与超时防护；
- [x] `writable_path` 严格拦截工作区外敏感路径写入；
- [x] 新增 `tests/test_sandbox.py` 且通过，全量 115 项全绿灯；
- [x] 零新增第三方依赖，`requirements.txt` 保持 4 项；
- [x] 回填本回执。

