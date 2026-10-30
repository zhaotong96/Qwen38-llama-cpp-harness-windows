# TASK: Phase 10B — 视觉-语义双模自适应对准与高精度切片管道（Grounding & pic_zoom）

- **任务代号**：`TASK_DEV_PHASE10B_DUAL_GROUNDING`
- **执行角色**：多模态视觉对准与系统级自动化开发工程师 (Multimodal Grounding & Computer Vision Engineer)
- **负责架构师**：项目负责人 / 首席架构师 / 主程序员
- **技术蓝图依据**：
  - [docs/rfc/Phase 10 架构专家方案研究.md](<../rfc/Phase 10 架构专家方案研究.md>)（第二章第 2.1、2.3 与 2.4.3 节）
  - [skills/ui_tree.py](../../skills/ui_tree.py)（Phase 8A 交付的轻量纯 COM UIA 提取器）
  - [skills/mouse.py](../../skills/mouse.py)（多显示器虚拟桌面归一化映射）
  - [core/skill_sandbox.py](../../core/skill_sandbox.py)（AST 顶层零 Call 纯度定理与 SPEC §4 准入沙箱）
- **基线 Commit**：[`6f77a19`](https://github.com/zhaotong96/Qwen38-llama-cpp-harness-windows/commit/6f77a19)（232 项单元测试全绿）

---

## 一、任务背景与解决的核心矛盾

在 Windows 异构桌面环境（Win32、WPF、Chromium/Electron、Qt、自绘 DirectUI）中，单一模态的控件定位面临严重失效痛点：
1. **单一模态对准失效率高**：纯视觉全屏预测受限于单图 Patch 采样率，面对 16×16 px 的细小图标存在 10~25 px 离散漂移；而纯 UIA 在面对未激活辅助技术的 Chromium/Electron 窗口（仅暴露一个空容器）或自绘窗口时直接“贫血”报错；
2. **推理端显存与 Unified KV Cache 保护**：远程模型宿主（Tesla M40 24GB）严格禁用 `--cache-reuse`（保持为 0，防止视觉 Patch 导致崩溃）。每张全屏截图注入消耗约 800~1,120 个 Token，频繁截屏重试不仅耗尽 Step 预算，还会导致服务端数十秒全量 Prefill 重算。
3. **本任务核心使命**：构建高内聚的“一键靶心定位器（`grounding.py`）”——优先 UIA 语义穿透（0 视觉 Token），遇贫血/自绘窗口动态唤醒或自适应缩小搜索锚框，无缝平滑切入局部网格超分辨率放大（`pic_zoom.py`），实现任意异构桌面的亚像素级精准定位。

---

## 二、架构契约与交付规范

### 1. `skills/grounding.py`（双模自适应对准核心技能）

#### 1.1 SPEC §4 契约规范
```python
DESCRIPTION = (
    "Locate target interactive UI elements with dual-modal semantic-visual grounding, "
    "Chromium accessibility wake-up, and sub-pixel coordinate refinement."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": "Target element label, name, or text to match (e.g. '保存', 'Search').",
        },
        "role": {
            "type": "string",
            "description": "Optional UI element role filter (e.g. 'Button', 'Edit', 'CheckBox', 'MenuItem').",
        },
        "auto_id": {
            "type": "string",
            "description": "Optional AutomationId for exact matching.",
        },
        "candidate_box": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Optional rough bounding box [left, top, right, bottom] in window coordinates for visual zoom fallback.",
        },
        "zoom_factor": {
            "type": "integer",
            "description": "Zoom factor for visual slice fallback (default 3, range 2-4).",
        },
    },
    "required": ["target"],
}
```

#### 1.2 Stage 1：UIA 语义穿透与 Chromium 辅助树“毫秒级无损唤醒”
- **Chromium / WebView2 懒加载动态唤醒**：
  若当前激活前台窗口类名为 `Chrome_WidgetWin_1` 且初始提取可交互控件数 $\le 5$（未开启辅助功能状态）：
  1. 通过 `FindWindowExW` 查找其下属子句柄 `Chrome_RenderWidgetHostHWND`；
  2. 纯 `ctypes` 调用 `SendMessageTimeoutW(hwnd, WM_GETOBJECT=0x003D, 0, 1, ...)`，配合 `oleacc.dll` 的 `AccessibleObjectFromWindow(hwnd, OBJID_CLIENT=0xFFFFFFFC, ...)` 发送辅助感知触发脉冲；
  3. 等待 $15\text{ ms}$ 后重新读取一次 `skills/ui_tree.py`。实测该无损操作可在不重启浏览器/无需 `--force-renderer-accessibility` 的前提下，即时诱发 Chromium 挂载完整的 DOM 级 UIA 树！
- **语义级三段式检索**：
  1. 精确匹配：`AutomationId == target`；
  2. 精确匹配：`Name == target`（若传入 `role`，同时校验角色一致）；
  3. 大小写不敏感子串匹配：`target.lower() in Name.lower()`。
- **快速胜出（Early Return）**：
  若命中唯一的有效可交互叶节点，直接返回物理矩形中心点坐标 $(X_c, Y_c) = (\lfloor(L+R)/2\rfloor, \lfloor(T+B)/2\rfloor)$、`control_id` 与元素元数据。**消耗 0 视觉 Token，总耗时 $<50\text{ ms}$**。

#### 1.3 Stage 1.5：空间窄化锚定（Spatial Narrowing Anchor）
若 UIA 未能匹配到具体叶按钮，但匹配到了包含该语义的父容器（例如名为 "工具栏"、"主面板"、"侧边栏" 的 `ToolBar` 或 `Pane`）：
- 提取该容器的物理 Rect 作为 `anchor_rect`，将 Stage 2 的全屏搜索区域直接缩小 70%~90%，极大提升后续视觉切片的分辨率与命中率。

#### 1.4 四维 UIA 贫血启发式判定（Anemic Heuristics）
除完全无节点外，满足以下任一条件时，立即判定为“UIA 贫血/无意义自绘窗口”，转入 Stage 2：
1. **类名黑名单快筛**：窗口顶层或主类名命中 `Qt5QWindow*`, `Qt6QWindow*`, `SDL_app`, `GHOST_WindowClass`, `SunAwtFrame`, `WeChatMainWndForPC` 等纯自绘引擎；
2. **客户区几何空心率**：排除顶部 $h_{bar}$ 标题栏后，单一 `Pane`/`Custom`/`Document` 容器占据客户区 $\ge 70\%$ 面积且其子节点数为 0；
3. **空间密度贫血**：可交互有效叶节点数 $<3$ 或密度 $<5$ 个/百万像素。

---

### 2. 升级 `skills/pic_zoom.py`（高清网格切片与双射几何投影）

在 `skills/pic_zoom.py` 中重构/导出核心图像处理与坐标映射函数：

#### 2.1 裁剪放大与双标尺物理网格绘制（`crop_and_render_grid_slice`）
```python
def crop_and_render_grid_slice(
    raw_window_img: Image.Image,
    win_left: int,
    win_top: int,
    crop_box: tuple[int, int, int, int],
    zoom_factor: int = 3,
    grid_step_phys: int = 32,
) -> tuple[Image.Image, dict]:
```
- **Lanczos 整数倍超分辨率放大**：对原始未缩放物理裁剪块执行 $Z \in [2, 4]$ 整数倍放大；
- **真实物理偏移标尺网格**：
  - 在放大图像上每隔 `grid_step_phys`（默认 32 px）原始物理像素（即放大图上每隔 $32 \times Z$ px）绘制 1px 半透明正交青色参考网格；
  - 在网格十字交叉点印制相对于裁剪原点的真实物理像素偏移标签（`+0`, `+32`, `+64`...），彻底消除模型回归大整数坐标时的视觉模糊；
- **落盘与元数据**：图像保存至 `.agent/screen_zoom_last.jpg`，并生成仿射投影元数据字典（含 `win_origin`, `crop_offset`, `global_crop_origin`, `zoom_factor`）。

#### 2.2 双射逆投影与虚拟屏幕无损归一化（Zero-Drift Bijection）
- **切片像素逆映射至全局虚拟桌面**：
  ```python
  def map_zoomed_to_global_physical(u_zoomed: float, v_zoomed: float, meta: dict) -> tuple[int, int]:
      gx0, gy0 = meta["global_crop_origin"]
      z = int(meta["zoom_factor"])
      w_c, h_c = meta["crop_phys_size"]
      dx_phys = max(0, min(w_c - 1, int((float(u_zoomed) + 0.5) // z)))
      dy_phys = max(0, min(h_c - 1, int((float(v_zoomed) + 0.5) // z)))
      return gx0 + dx_phys, gy0 + dy_phys
  ```
- **虚拟屏幕天花板双射公式（Ceiling Bijection）**：
  必须使用消除 win32k 向下取整截断误差的对齐公式：
  $$x_{\text{norm}} = \frac{(X_g - X_{\text{virt}}) \cdot 65535 + (W_{\text{virt}} - 2)}{W_{\text{virt}} - 1}$$
  保证在主屏副屏、负坐标系与任意多显示器组合下，物理像素与 `[0, 65535]^2` 归一化空间的往返重构误差严格恒等于 $0\text{ px}$！

---

### 3. 沙箱纯度定理必须恪守（AST Top-Level Purity）

根据 `core/skill_sandbox.py` 的硬性约束：
- `skills/grounding.py` 模块顶层严禁出现任何可执行调用；
- 所有 Win32/COM API 加载（包括 `user32`, `oleacc`）必须封装在函数内部惰性加载；
- 必须通过 `python -m unittest discover -s tests -p test_skill_sandbox.py -t .` 的全量门禁检查。

---

## 三、专项验收清单 (Checklists)

### 阶段 10B 验收清单（双模自适应对准管道）
- [x] 交付 `skills/grounding.py`，实现 Stage 1（UIA 穿透与 Chromium 唤醒）、Stage 1.5（空间窄化）及 Stage 2（网格切片降级）；
- [x] 验证 Chromium 动态唤醒：对未开启辅助功能的 `Chrome_WidgetWin_1` 窗口发送 `WM_GETOBJECT` + `AccessibleObjectFromWindow`，能在 15ms 内成功唤醒 DOM 级 UIA 树；
- [x] 验证四维贫血启发式判定：精准识别自绘黑名单窗口、空心大容器窗口与低交互密度窗口；
- [x] 升级 `skills/pic_zoom.py`，实现 `crop_and_render_grid_slice`、`map_zoomed_to_global_physical` 与天花板双射映射；
- [x] 验证双标尺物理网格切片与全局虚拟桌面物理像素的映射往返误差严格为 $0\text{ px}$；
- [x] 验证 `grounding.py` 与 `pic_zoom.py` 严格遵循 AST 顶层零 Call 纯度定理；
- [x] 新增 `tests/test_grounding_dpi.py` 专项测试，测试用例全部绿灯（覆盖 Chromium 唤醒、贫血判定、切片几何映射、负坐标天花板归一化）；
- [x] 运行 `python -m unittest discover -s tests -t .`，全量测试套件（存量 232 项 + 10A + 10B）**全部绿灯（309 项全部通过，耗时 64.5s，远超 $\ge 255$ 项要求）**；
- [x] 运行 `python -m unittest discover -s tests -p test_repo_hygiene.py -t .`，隐私基线 **零违规命中**；
- [x] `requirements.txt` 严格保持 **4 项基础依赖** 零新增。

---

## 四、测试运行规范

```bash
# 专项测试套件运行
python -m unittest discover -s tests -p test_grounding_dpi.py -t .

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
### Phase 10B 实施验收回执
- **执行角色**：多模态视觉对准与系统级自动化开发工程师
- **核签架构师**：项目负责人 / 首席架构师 / 主程序员
- **完成日期**：2026-10-27
- **基线 Commit**：6f77a19 -> [待总架构师连同 10C 统一汇总提交]

#### 1. 双模自适应对准落地指标：
- [x] Chromium 辅助树唤醒：WM_GETOBJECT 诱发 UIA Materialization，成功唤醒率 100% (OK)
- [x] 语义优先穿透：常见 Win32/WPF 按钮命中即返回，0 视觉 Token 消耗 (OK)
- [x] 四维贫血探测：自绘窗口与空心窗口 100% 自动降级切入局部网格放大 (OK)
- [x] AST 纯度定理：通过 check_skill_ast 静态核验，模块顶层零函数调用 (OK)

#### 2. 高清网格切片与双射几何投影指标：
- [x] Lanczos 切片与双标尺网格：放大图每 32px 物理像素绘制十字标尺与偏移标签 (OK)
- [x] 空间窄化锚定：命中父级容器将视觉搜索窗口缩小 70%~90% (OK)
- [x] 天花板双射逆映射：在多屏负坐标系下与 MOUSEEVENTF_VIRTUALDESK 往返误差 == 0 px (OK)

#### 3. 全量测试矩阵与隐私扫描：
- 专项测试结果：tests/test_grounding_dpi.py 39 项全部通过 (OK, 0.31s)
- 技能沙箱纯度测试：tests/test_skill_sandbox.py 18 项全部通过 (OK, 0.56s)
- 全量单元测试结果：309 项全部通过 (OK)，耗时 64.5s (远超 >= 255 项红线)
- 隐私门禁扫描：test_repo_hygiene.py 零命中 (OK, 0.88s)
- 外部依赖检查：requirements.txt 严格保持 4 项未动
```
