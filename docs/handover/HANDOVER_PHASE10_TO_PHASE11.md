# Qwen38-llama-cpp-harness-windows 跨会话工程交接档案 (Hand-off Dossier)

> **前序会话 ID**: `24045d5a-a8ca-43f9-a4e3-0b46a2d97052`  
> **前序会话链接**: [Qwen38-Harness Architecture & Phase 10 Review](conversation://24045d5a-a8ca-43f9-a4e3-0b46a2d97052)  
> **当前物理工作区**: `G:\My Drive\work\Qwen38-llama-cpp-harness-windows`  
> **交接时间戳**: 2026-10-30T18:05:00-04:00  
> **交接状态**: Phase 10 完美收官（337 测试全绿 + 全仓 Commit 历史脱敏至 noreply） $\rightarrow$ 开启 Phase 11 认知自治  

---

## 1. 核心身份契约与系统红线（必须无缝继承）

1. **角色定义**：你必须严格扮演 **`主程序员 / 项目架构负责人 / 项目主管`**。优先编写 Markdown 规范或任务卡片委派任务，而非擅自修改业务边界；
2. **全局规则 (`@[user_global]`)**：
   - 每次回复必须以 `[意图]: 1. xxx; 2. xxx...` 开头；
   - 任何破坏性或状态变更操作前必须显式标注 `[动作]: <意图或命令>`；
   - **绝对严禁未经用户显式批准私自执行 `git commit`**；
   - 严禁执行未请求、未验证或越界的脚本；
   - 穷尽自主深度推理建立因果真理，严禁把排错与推导的认知负担转嫁给用户；
3. **架构铁律与极简审美**：
   - **UNIX-like 万物皆文件**：状态透明投影在磁盘（`.agent/`、`checkpoints/`），拒绝黑盒数据库；
   - **零构建原生 Web 控制台**（SPEC §13.2）：`web/` 目录 100% 原生 ES Modules，严禁引入 node/npm/vite/webpack 构建链；
   - **4 项依赖不变性**：`requirements.txt` 严格锁定 `openai`, `mss`, `pillow`, `websocket-client` 4 项，严禁膨胀；
   - **单例守护网关**：`py -3 -m core.api`（控制台直连 `http://127.0.0.1:8765/web/index.html`）；
   - **推理宿主参数（Tesla M40）**：锁定 `--spec-draft-n-max 4` 与 `--cache-reuse 0`。

---

## 2. Phase 10 全量交付与终检状态（已 100% 闭环）

- **Task 10A（Win32 窗口编排与原子剪贴板）**：
  - `skills/window_manager.py`：六步防死锁状态机（150ms `WM_NULL` 探活、Token RID 权限避让、`IsIconic` 保护、`VK_MENU` 脉冲、成对解绑、75ms `HWND_TOPMOST` 瞬态破锁、Chromium 子句柄聚焦）。
  - `skills/clipboard.py`：64 位 `c_void_p` 原生指针安全、零 `GlobalFree`、5x20ms 自旋锁、`paste=True` 单批次 `SendInput` Ctrl+V 原子注入。
  - 专项验证：`tests/test_window_clipboard.py`（38 项测试，100% 通过）。
- **Task 10B（视觉-语义双模自适应对准与高清切片）**：
  - `skills/grounding.py`：Chromium 15ms DOM 动态唤醒（`WM_GETOBJECT` 0x003D + `OBJID_CLIENT`）、三段式语义检索（`auto_id` -> `exact_name` -> `substring`）、Stage 1.5 父容器窄化（70%~90% 降噪）、四维贫血自适应降级。
  - `skills/pic_zoom.py`：整数倍 Lanczos 高清切片、32px 物理十字标尺、天花板双射逆映射。
  - 专项验证：`tests/test_grounding_dpi.py`（39 项测试，100% 通过）。
- **Task 10C（零构建 Web 控制台透视画布与时间轴）**：
  - `web/store.js`（119 行原生微任务单向流状态机）、`web/canvas_overlay.js`（视网膜 `+0.5px` 物理半像素防模糊）、`web/dag_timeline.js`（SVG 因果虚线弧与五段式账本第 4 节联动）。
  - `core/api.py`：只读投影路由 `/tasks/<id>/ui_tree`、`/tasks/<id>/rewind_ledger`、`/screens[/<name>]`（严格防御路径穿越）。
  - 专项验证：`tests/test_api_ui_tree.py`（28 项测试，100% 通过）。
- **领域专家建议的 3 项边界加固补丁（已全部实装）**：
  1. `skills/grounding.py` `_persist_ui_snapshot` 补齐 `origin`, `dpi_scale`, `window_title`；
  2. `skills/mouse.py` 归一化与逆映射升级为与 `pic_zoom.py` 严格同构的天花板双射模型；
  3. `core/skill_sandbox.py` 的 `FROZEN_CORE_SKILLS` 写保护清单扩容纳入 `"clipboard"`, `"grounding"`, `"window_manager"`。
- **全量测试与安全门禁**：
  - **337 / 337 项测试通过**（72.7s，0 失败，0 错误）；
  - AST 零 Call 纯度门禁 100% 通过（`test_skill_sandbox.py`）；
  - 隐私与敏感信息门禁 100% 通过（`test_repo_hygiene.py`，0 违规）。

---

## 3. Git 版本库与脱敏状态（当前真实现场）

1. **当前分支与 HEAD**：
   - 当前分支：`main`；
   - 本地最新提交：`125d2b6` (`feat(phase10): Web runtime, dual-modal grounding, and Win32 desktop primitives (Phase 10 complete, 337 tests OK)`)；
2. **全仓历史作者与邮箱脱敏（B1 审计同构）**：
   - 全部 10 个 Commit 的作者与提交者已通过 `redact_history.py`（基于底层 `git commit-tree`）统一重写为：  
     **`zhaotong96 <40412861+zhaotong96@users.noreply.github.com>`**；
   - 彻底清除个人真实 Outlook 邮箱；
   - 本地自动保留了安全备份分支：`backup-pre-redact`（指向旧的 `30f57d1`）；
3. **本地配置加固**：
   - `.git/config` 已硬化配置：
     ```ini
     [user]
     	name = zhaotong96
     	email = 40412861+zhaotong96@users.noreply.github.com
     [credential]
     	helper = 
     	helper = manager
     ```
4. **远程源与推送就绪度**：
   - `remote.origin.url` = `https://github.com/zhaotong96/Qwen38-llama-cpp-harness-windows.git`；
   - 待执行推送命令（由于重写了历史元数据，采用安全租约强制更新）：
     ```powershell
     git push --force-with-lease origin main
     ```

---

## 4. 关键物理路径与环境恢复背景

- **为什么开启新会话？**
  - 系统重启后，Google Drive for Desktop 挂载盘符在物理系统表现为 `G:\My Drive`；
  - 旧会话 IDE 终端进程工作目录绑定在原路径 `G:\我的云端硬盘` 上；
  - 由于 Google Drive 是虚拟云盘驱动（非物理 NTFS 卷），执行 `mklink /J "G:\我的云端硬盘" "G:\My Drive"` 会报错 `Local NTFS volumes are required to complete the operation`；
  - **新会话解决方案**：新对话直接在 IDE 中以 `G:\My Drive\work\Qwen38-llama-cpp-harness-windows` 作为工作区打开，终端与全部命令工具将天然无缝恢复 100% 正常执行。

---

## 5. 新会话即刻接手行动指南（Immediate Action Items）

新会话启动后，按以下顺序推进工作：

1. **步骤一：验证终端与执行远程推送**：
   ```powershell
   # 移除一次性脱敏临时脚本
   Remove-Item redact_history.py -ErrorAction SilentlyContinue

   # 租约安全推送 Phase 10 全量脱敏主干至 GitHub
   git push --force-with-lease origin main
   ```
2. **步骤二：正式开启 Phase 11 认知自治研发周期**：
   - 查阅核心规约：[`docs/rfc/Phase 10 验收与 Phase 11 架构前瞻.md`](../rfc/Phase%2010%20验收与%20Phase%2011%20架构前瞻.md) 与 [`docs/rfc/ARCHITECTURE_RFC_PHASE11_VISION_CONSULTATION.md`](../rfc/ARCHITECTURE_RFC_PHASE11_VISION_CONSULTATION.md)；
   - 启动 **Phase 11A 任务卡片** 编写：`docs/tasks/TASK_DEV_PHASE11A_CONTEXT_GC.md`（聚焦上下文尾端暂态报错剪枝 `prune_resolved_trial_tail`，消除多轮 ReAct 试错时的上下文垃圾与 Token 浪费，保持因果账本第 4 节 Lessons 不变）。
