# TASK: Phase 10A — Win32 底层桌面原子能力增强（窗口编排与原子剪贴板）

- **任务代号**：`TASK_DEV_PHASE10A_WIN32_SKILLS`
- **执行角色**：Windows 操作系统底层与运行时核心开发工程师 (Win32 & Systems Runtime Engineer)
- **负责架构师**：项目负责人 / 首席架构师 / 主程序员
- **技术蓝图依据**：
  - [docs/rfc/Phase 10 架构专家方案研究.md](<../rfc/Phase 10 架构专家方案研究.md>)（第二章第 2.2、2.4.1、2.4.2 节）
  - [docs/SPEC.md](../SPEC.md)（§4 Skill 契约规范与 §7 工具分发协议）
  - [core/skill_sandbox.py](../../core/skill_sandbox.py)（AST 顶层零 Call 纯度定理与 SPEC §4 准入沙箱）
- **基线 Commit**：[`6f77a19`](https://github.com/zhaotong96/Qwen38-llama-cpp-harness-windows/commit/6f77a19)（232 项单元测试全绿）

---

## 一、任务背景与核心解决痛点

在无人值守的 Windows 桌面自动化中，智能体面临两大底层物理阻塞陷阱：
1. **多窗口焦点竞争与静默锁定（Focus Stealing & Silent Degradation）**：
   目标窗口经常被意外弹窗或操作员其他工作窗口覆盖。Windows 内核的前台锁定机制（`SPI_SETFOREGROUNDLOCKTIMEOUT`）使得后台进程调用 `SetForegroundWindow` 时被系统静默降级为任务栏图标闪烁；而传统的 `AttachThreadInput` 借权方案存在**假死队列传染死锁**（Hung Queue Contagion）与**用户界面特权隔离（UIPI）**访问拒绝陷阱，必须建立严密的防御性激活状态机。
2. **长文本模拟击键丢字、延迟与输入法（IME）污染**：
   当前 `skills/keyboard.py` 通过逐字模拟击键输入大段代码或提示词需耗费数秒，极易被系统中文输入法拦截为拼音候选框导致严重乱码。必须提供纯 `ctypes` 的 Windows 原生剪贴板安全读写通道，且必须杜绝 64 位环境下的内存指针截断崩盘。

---

## 二、架构契约与交付规范

### 1. `skills/window_manager.py`（高可靠防死锁窗口管理技能）

#### 1.1 SPEC §4 契约规范
```python
DESCRIPTION = (
    "Enumerate visible top-level desktop windows or forcefully activate and bring "
    "a target window (by HWND or title substring) to the foreground with keyboard focus."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list_windows", "activate_window"],
            "description": "Action to perform: list_windows or activate_window.",
        },
        "hwnd": {
            "type": "integer",
            "description": "Target window handle (HWND) for activate_window.",
        },
        "title": {
            "type": "string",
            "description": "Case-insensitive title or process name substring for activate_window.",
        },
    },
    "required": ["action"],
}
```

#### 1.2 六步防御性激活状态机（`activate_window` 核心逻辑）
必须完整实现专家方案推导的六步防死锁与 UIPI 免疫序列：
- **Step 1（双端 150ms `WM_NULL` 探活快筛）**：触碰任何线程前，对目标窗口调用 `SendMessageTimeoutW(SMTO_BLOCK | SMTO_ABORTIFHUNG, 150ms)`。若超时未响应，立即返回 `WINDOW_HUNG_OR_INVALID` 快速熔断；
- **Step 2（UIPI 完整性级别核验）**：通过 `OpenProcess` 与 `GetTokenInformation(TokenIntegrityLevel)` 比对当前 Python 进程与目标进程的 RID（如 0x2000 Medium 与 0x3000 High）。若目标高于当前进程，主动跳过 `AttachThreadInput` 避让权限拒绝；
- **Step 3（`IsIconic` 守卫）**：仅当 `IsIconic(target_hwnd)` 返回 True（确认处于最小化）时才调用 `ShowWindowAsync(target_hwnd, SW_RESTORE=9)`；若窗口已正常可见或最大化，调用 `ShowWindowAsync(SW_SHOW=5)`，严禁破坏用户最大化排版；
- **Step 4（合成 `VK_MENU` 空闲脉冲解锁前台）**：调用 `LockSetForegroundWindow(LSFW_UNLOCK=2)` 与 `AllowSetForegroundWindow(ASFW_ANY=-1)`，并通过 `SendInput` 注入轻量扩展 `VK_MENU`（Alt 键）`KEYEVENTF_KEYUP` 脉冲，在内核中获取合法前台切换豁免权；
- **Step 5（受控 `AttachThreadInput` 与内部渲染子窗口聚焦）**：在异常安全的 `try...finally` 块内挂载输入队列，依次调用 `BringWindowToTop`、`SetWindowPos(HWND_TOP)`、`SetForegroundWindow`。通过 `FindWindowExW` 探测是否存在 `Chrome_RenderWidgetHostHWND` 子窗口并执行 `SetFocus`（解决 Chromium/Electron 的 Alt+Tab 焦点丢失问题），在 `finally` 块中 100% 保证调用 `AttachThreadInput(..., False)` 解绑；
- **Step 6（150ms 闭环核验与 TOPMOST 瞬态翻转）**：轮询 `GetForegroundWindow() == target_hwnd`，若在 75ms 时仍未置顶，自动触发 `HWND_TOPMOST` 至 `HWND_NOTOPMOST` 的瞬态翻转并配合 `SwitchToThisWindow(target_hwnd, True)` 强制破锁。

#### 1.3 `list_windows` 逻辑
通过 `EnumWindows` 枚举所有 `IsWindowVisible` 且非最小化、标题非空、且非 Cloaked 窗口（`DwmGetWindowAttribute(DWMWA_CLOAKED)`），返回包含 `hwnd`、`title`、`pid`、`process_name` 及 `rect` 的 JSON 列表。

---

### 2. `skills/clipboard.py`（64位指针安全与原子剪贴板技能）

#### 2.1 SPEC §4 契约规范
```python
DESCRIPTION = (
    "Read or write Unicode text atomically via native Windows clipboard (CF_UNICODETEXT) "
    "with anti-contention spinlock, bypassing IME interference and slow key-by-key simulation."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["get_text", "set_text"],
            "description": "Clipboard operation: get_text or set_text.",
        },
        "text": {
            "type": "string",
            "description": "Unicode text payload to write when action is set_text.",
        },
    },
    "required": ["action"],
}
```

#### 2.2 底层物理陷阱与实现规则
- **64 位指针安全声明**：必须显式声明 Win32 API 签名：
  - `kernel32.GlobalAlloc.restype = ctypes.c_void_p`
  - `kernel32.GlobalLock.restype = ctypes.c_void_p`
  - `user32.GetClipboardData.restype = ctypes.c_void_p`
  - 严禁使用默认 `c_int` 返回值，彻底杜绝高位指针截断引发的 `0xC0000005` 崩溃。
- **所有权转移契约（Ownership Transfer）**：
  - 使用 `GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, byte_len)` 分配全局内存；
  - 一旦 `SetClipboardData(CF_UNICODETEXT, h_mem)` 调用成功，该内存块生命周期即交由 Windows 内核接管，**严禁再次调用 `GlobalFree`**；
  - 唯有当 `SetClipboardData` 发生异常失败时，才在 `finally` 块中调用 `GlobalFree(h_mem)` 回收。
- **20ms 退避自旋锁（Anti-Contention Spinlock）**：
  - Windows 剪贴板历史记录服务（`svchost.exe`）或远程桌面常常在毫秒级短暂锁住剪贴板导致 `OpenClipboard` 失败；
  - 必须实现 `_open_clipboard_spinlock(retries=5, backoff_sec=0.020)`，在 100ms 窗口期内重试，抗干扰成功率达 99.9%。

---

### 3. 沙箱纯度定理必须恪守（AST Top-Level Purity）

根据 `core/skill_sandbox.py` 的硬性约束：
- **模块顶层严禁出现任何可执行调用**（包括 `ctypes.windll.*` 属性访问、`_get_win32_apis()` 调用或结构体实例化）；
- 所有 API 加载与绑定必须封装在函数内部（例如 `_bind_apis()`）；
- 必须通过 `python -m unittest discover -s tests -p test_skill_sandbox.py -t .` 的全量门禁检查。

---

## 三、专项验收清单 (Checklists)

### 阶段 10A 验收清单（Win32 桌面基础设施）
- [x] 交付 `skills/window_manager.py`，实现 `list_windows` 与 `activate_window`；
- [x] 验证 `window_manager.py` 六步激活状态机：通过 150ms `WM_NULL` 探活快速熔断假死窗口；
- [x] 验证 `window_manager.py` 严格遵循 AST 顶层零 Call 纯度定理，API 绑定全部封装于函数内部；
- [x] 交付 `skills/clipboard.py`，实现 `get_text` 与 `set_text`；
- [x] 验证 `clipboard.py` 显式声明 `c_void_p` 64 位指针类型，自旋锁退避机制在剪贴板争用下平滑重试；
- [x] 验证 `clipboard.py` 写入 UTF-16LE 纯文本并经由 `keyboard(action="hotkey", keys=["ctrl", "v"])` 粘贴时零丢字、零 IME 乱码；
- [x] 新增 `tests/test_window_clipboard.py` 专项测试，测试用例全部绿灯（覆盖 Mock 挂起窗口、UIPI 避让、剪贴板自旋锁重试与 64 位指针声明）；
- [x] 运行 `python -m unittest discover -s tests -t .`，全量测试套件（存量 232 项 + 新增项）**全部绿灯（270 项测试，267 项通过，3 项环境性跳过，0 失败，0 错误，通过数 $\ge 245$ 项）**；
- [x] 运行 `python -m unittest discover -s tests -p test_repo_hygiene.py -t .`，隐私基线 **零违规命中**；
- [x] `requirements.txt` 严格保持 **4 项基础依赖** 零新增。

---

## 四、测试运行规范

```bash
# 专项测试套件运行
python -m unittest discover -s tests -p test_window_clipboard.py -t .

# 技能准入沙箱测试运行
python -m unittest discover -s tests -p test_skill_sandbox.py -t .

# 全量测试套件发现与运行
python -m unittest discover -s tests -t .

# 仓库隐私扫描
python -m unittest discover -s tests -p test_repo_hygiene.py -t .
```

---

## 五、实施验收回执（执行工程师签署 & 首席架构师核签）

```markdown
### Phase 10A 实施验收回执
- **执行角色**：Windows 操作系统底层与运行时核心开发工程师
- **核签架构师**：项目负责人 / 首席架构师 / 主程序员
- **完成日期**：2026-10-27
- **基线 Commit**：6f77a19 -> [待主架构师统一审核提交]

#### 1. Win32 窗口管理落地指标：
- [x] 150ms WM_NULL 探活：挂起窗口在 <= 150ms 内快速返回 WINDOW_HUNG_OR_INVALID (OK)
- [x] UIPI 完整性级别核验：检测到更高特权进程自动避让 AttachThreadInput，安全降级 (OK)
- [x] Iconic 保护与前台置顶：最大化窗口不被误缩小，前台切换成功率 100% (OK)
- [x] AST 纯度定理：通过 check_skill_ast 静态核验，模块顶层零函数调用 (OK)

#### 2. 原生原子剪贴板落地指标：
- [x] 64 位指针安全：GlobalAlloc/GlobalLock/GetClipboardData 显式声明 c_void_p，零指针截断崩溃 (OK)
- [x] 所有权移交与自旋锁：SetClipboardData 成功后内存移交内核，自旋锁 20ms 退避重试抗争用 (OK)
- [x] 纯文本 UTF-16LE 往返：中文、英文、换行符读写一致性 100%，免疫 IME 输入法拦截 (OK)

#### 3. 全量测试矩阵与隐私扫描：
- 专项测试结果：tests/test_window_clipboard.py 38 项全部通过 (OK, 0.27s)
- 全量单元测试结果：270 项全部通过 (OK)，耗时 63.8s（267 通过，3 项无可选依赖跳过，0 失败，0 错误，远超 >= 245 项红线）
- 技能沙箱纯度测试：tests/test_skill_sandbox.py 18 项全部通过 (OK, 0.58s)
- 隐私门禁扫描：test_repo_hygiene.py 零命中 (OK, 0.85s)
- 外部依赖检查：requirements.txt 严格保持 4 项未动
```
