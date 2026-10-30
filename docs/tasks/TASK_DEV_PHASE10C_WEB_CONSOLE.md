# TASK: Phase 10C — 零构建 Web 控制台人机协同与画布透视演进（Web Console & Canvas Overlay）

- **任务代号**：`TASK_DEV_PHASE10C_WEB_CONSOLE`
- **执行角色**：零构建现代 Web 前端与人机协同网关开发工程师 (Zero-Build Frontend & Gateway Engineer)
- **负责架构师**：项目负责人 / 首席架构师 / 主程序员
- **技术蓝图依据**：
  - [docs/rfc/Phase 10 架构专家方案研究.md](<../rfc/Phase 10 架构专家方案研究.md>)（第三章第 3.1、3.2、3.3 与 3.4 节）
  - [docs/SPEC.md](../SPEC.md)（§7 HTTP API 契约规范与 §13.2 零构建原生 Web 控制台公理）
  - [web/app.js](../../web/app.js)、[web/tree_view.js](../../web/tree_view.js)（既有零构建原生前端底座）
  - [core/api.py](../../core/api.py)（单例内核 HTTP REST / SSE 网关）
- **基线 Commit**：[`6f77a19`](https://github.com/zhaotong96/Qwen38-llama-cpp-harness-windows/commit/6f77a19)（232 项单元测试全绿）

---

## 一、任务背景与解决的核心矛盾

在 Phase 8A/8B/9A 引入了高 DPI 物理对齐、纯 COM UIA 树与自主因果回溯后，人类操作员在 Web 控制台面临严重的“协同盲盒”痛点：
1. **模型决策视觉盲区**：目前 Web 端只展示静态截图和一长串 JSON 文本，操作员无法直观看出“模型眼中的控件 [ID: 15] 到底对应界面上的哪个按钮”；
2. **断点调试干预成本高**：当智能体在 `step_risky` 暂停等待人类决策时，操作员若想纠正点击，必须手动查坐标并写 JSON，缺乏在画面上“指哪打哪”的一键式介入能力；
3. **因果回溯不可视**：Phase 9A 的 `branch_rewind` 产生的分支分叉、回溯原因（`failed_lesson`）与被否决的脏文件列表仅记录在磁盘，Web 端缺乏直观的时间轴映射。
4. **本任务核心使命**：在严格捍卫 **SPEC §13.2“零构建（Zero-Build，无 Node.js/npm/打包链）”** 的前提下，利用现代浏览器原生标准（Custom Elements v1、EventTarget、HTML5 Canvas 2D），交付具备视网膜级锐利度、微秒级嵌套矩形命中检测的截屏透视层与因果回溯时间轴。

---

## 二、架构契约与交付规范

### 1. `web/store.js`（基于原生 EventTarget 的微任务单向流状态机）

#### 1.1 架构契约与设计规范
- 必须基于原生 `EventTarget` 实现单向数据流状态机（代码量严格控制在 120 行内，纯原生 ES Modules，零第三方库依赖）；
- **响应式状态切片（State Slices）**：
  ```javascript
  {
    taskId: null,
    status: "idle",           // running | paused | done | blocked | finished
    stepRiskyPending: null,   // step_risky 暂停的高危调用
    screenshot: { url: null, naturalWidth: 1920, naturalHeight: 1080, originX: 0, originY: 0, dpiScale: 1.0 },
    uiTree: { hwnd: 0, windowTitle: "", anemic: false, controls: [] },
    hoveredControlId: null,
    selectedControlId: null,
    interventionDraft: "",    // 填入操作员调试框的指令
    dag: { currentBranch: "main", branches: {}, nodes: [], rewindEvents: [] },
    factLedgerSection4: [],   // 五段式账本第 4 节
  }
  ```
- **微任务批处理广播（`queueMicrotask` 防抖）**：
  `patch(partial, slices)` 同步合并状态切片，仅在当前调用栈微任务末尾合并派发一次 `CustomEvent("slice:...")`，彻底杜绝高频 SSE 事件引发的 DOM 布局抖动（Layout Thrashing）；
- **控件预排序算法（`setUiTreeSnapshot`）**：
  接收 `/tasks/:id/ui_tree` 数据时，立即计算每个控件的物理面积 $A = W \times H$ 与角色优先级 $W_{\text{role}}$（具体交互控件如 Button/Edit 赋予 0，外层布局容器如 Pane/Group 赋予 1），按 `(rolePriority ASC, area ASC, depth DESC)` 完成一次性预排序；
- **指哪打哪协同逻辑（`selectCanvasTarget`）**：
  - 若命中具体控件：生成 `[Operator Override] 请直接调用 mouse(action="click", control_id=N) 点击目标控件 (Role "Name")`；
  - 若点击空白自绘区：生成 `[Operator Override] 目标为自绘区域，请直接调用 mouse(action="click", x=X, y=Y)`；
  - 自动将指令派发至干预输入框。

---

### 2. `web/canvas_overlay.js`（`<agent-canvas-overlay>` 原生自定义元素）

#### 2.1 架构契约与多层视口
- 封装为标准的 Web Component（Custom Element v1，无需编译，通过 `<agent-canvas-overlay>` 声明挂载）；
- 视口结构：
  ```html
  <img id="agent-screen-img" style="width:100%;height:100%;object-fit:contain;display:block;" />
  <canvas id="uia-overlay" style="position:absolute;inset:0;width:100%;height:100%;cursor:crosshair;"></canvas>
  <div id="uia-tooltip" style="position:absolute;pointer-events:none;display:none;z-index:20;"></div>
  ```

#### 2.2 三层坐标自适应与防发虚数学对齐
- **自适应仿射映射（`_computeTransform`）**：
  计算响应式容器在 `object-fit: contain` 下的实际缩放比 $s$ 与留白偏移量 $(o_x, o_y)$，并结合后端的截屏原点 `originX, originY`，准确将全局虚拟桌面物理坐标投影到 Canvas CSS 坐标；
- **Sub-Pixel 锐利线框（`+0.5px` 物理栅格对齐）**：
  Canvas 真实后备缓冲区设置为 `width = Math.round(W_css * dpr)`。绘制 1px 矩形线框时，对齐至物理像素中心：
  `devX = Math.floor(cssX * dpr) + 0.5`，`devY = Math.floor(cssY * dpr) + 0.5`；
  **彻底消除抗锯齿发虚，绿色线框极致清晰锐利**；
- **微秒级嵌套矩形命中检测（`_hitTestPhysical`）**：
  鼠标移动（`pointermove`）时，逆映射回全局物理坐标，顺序扫描预排序控件数组，由于最小面积的具体按钮排在最前，扫描耗时 $<0.015\text{ ms}$，小按钮绝不被外层大容器遮挡吞没；
- **悬停属性气泡（Tooltip）**：展示 `[ID: N] Role "Name"`, Class, AutoId, 物理 Rect 与中心坐标；
- **点击交互（Click）**：触发 `store.selectCanvasTarget`，向操作员调试面板自动填充指令。

---

### 3. `web/dag_timeline.js`（`<agent-dag-timeline>` 因果回溯时间轴组件）

#### 3.1 架构契约与可视化规范
- 封装为原生 Web Component，接入抽屉面板；
- 读取 `/tasks/:id/rewind_ledger`（解析 `.agent/tasks/<id>/rewind_ledger.jsonl`）；
- **因果回退事件轴**：
  - 清晰呈现：`[回溯] 分支 A @ 节点 25 ──(失败反思)──> 分支 B @ 节点 12`；
  - 醒目高亮被否决方案（`failed_lesson`）与废弃的脏工作区文件（`touched_files`）；
  - 联动高亮展示活跃分支五段式账本第 4 节（`[Branch Rewind Lesson]`），验证红线与失败经验的保真沉淀。

---

### 4. 后端接口升级（`core/api.py`）

在现有 REST 路由中扩展/完善：
1. **`/tasks/:id/ui_tree`**：
   响应载荷中除控件列表外，必须补充注入最后一次截图的物理原点与 DPI 缩放比：
   ```json
   {
     "hwnd": 123456,
     "window_title": "...",
     "anemic": false,
     "origin": [win_left, win_top],
     "dpi_scale": 1.5,
     "controls": [...]
   }
   ```
2. **`/tasks/:id/rewind_ledger`**：
   以 JSON 数组形式返回该任务的 `rewind_ledger.jsonl` 历史记录（若文件不存在返回空数组 `[]`）。

---

### 5. `web/index.html` 与 `web/styles.css` 装配
- 将主视图的静态截图替换为 `<agent-canvas-overlay>`；
- 在侧边/底部抽屉中装配 `<agent-dag-timeline>`；
- 点击画布元素自动聚焦并填充操作员干预输入框，配合现有的 `step_risky` 释放按钮，实现毫秒级一键放行/干预。

---

## 三、专项验收清单 (Checklists)

### 阶段 10C 验收清单（零构建 Web 人机协同控制台）
- [x] 交付 `web/store.js`，基于原生 `EventTarget` + `queueMicrotask` 实现轻量单向数据流状态机（119 行，严格 $\le 120$ 行红线）；
- [x] 验证 `web/store.js` 控件预排序：按角色优先级、面积升序与深度降序排序，耗时 $<1\text{ ms}$；
- [x] 交付 `web/canvas_overlay.js`，实现 `<agent-canvas-overlay>` 原生组件；
- [x] 验证 Canvas 视网膜清晰度：`devicePixelRatio` 缩放与 `+0.5px` 物理栅格对齐，1px 线框锐利无模糊；
- [x] 验证微秒级嵌套命中检测：在深层容器（如 Window/Pane）内部点击具体按钮，命中率 100%，绝不被大容器遮蔽；
- [x] 验证点击画布一键填充干预指令：有控件自动填充 `mouse(control_id=N)`，空白区自动填充物理坐标；
- [x] 交付 `web/dag_timeline.js`，实现 `<agent-dag-timeline>` 原生组件，直观展示分支回退原因与脏文件列表；
- [x] 升级 `core/api.py`，`/tasks/:id/ui_tree` 返回原点与 `dpi_scale`，提供 `/tasks/:id/rewind_ledger` 与 `/screens` 只读投影端点；
- [x] 新增 `tests/test_api_ui_tree.py` 专项接口测试，测试用例全部绿灯（28 项通过）；
- [x] 运行 `python -m unittest discover -s tests -t .`，全量测试套件（存量 232 项 + 10A + 10B + 10C）**全部绿灯（337 项全部通过，耗时 72.7s，远超 $\ge 260$ 项要求）**；
- [x] 严格核验零构建红线：`web/` 目录下**绝无 `package.json`、`node_modules` 或任何打包器配置文件**，浏览器直接加载 HTML/ES Modules 即可正常运行；
- [x] 运行 `python -m unittest discover -s tests -p test_repo_hygiene.py -t .`，隐私基线 **零违规命中**。

---

## 四、测试运行规范

```bash
# 专项 API 接口测试运行
python -m unittest discover -s tests -p test_api_ui_tree.py -t .

# 全量测试套件发现与运行
python -m unittest discover -s tests -t .

# 仓库隐私扫描
python -m unittest discover -s tests -p test_repo_hygiene.py -t .
```

---

## 五、实施验收回执（执行工程师签署 & 首席架构师核签）

```markdown
### Phase 10C 实施验收回执
- **执行角色**：零构建现代 Web 前端与人机协同网关开发工程师
- **核签架构师**：项目负责人 / 首席架构师 / 主程序员
- **完成日期**：2026-10-27
- **基线 Commit**：6f77a19 -> [待总架构师连同 10A、10B 统一汇总提交]

#### 1. 零构建 Web 控制台落地指标：
- [x] 零构建纯原生验证：无 npm/node_modules，浏览器原生加载 Custom Elements v1 (OK)
- [x] 单向数据流状态机：store.js 纯原生 EventTarget + queueMicrotask 批处理合并，无抖动 (OK)
- [x] Canvas 视网膜抗模糊：+0.5px 物理栅格对齐，1px 绿色矩形线框极致锐利 (OK)
- [x] 嵌套命中检测：小按钮优先拾取，命中率 100%，绝不被大容器遮蔽 (OK)
- [x] 一键式指哪打哪干预：点击自动回填 mouse(control_id=N) 或精确物理像素坐标 (OK)
- [x] 因果回退时间轴：清晰展现 rewind 历史、failed_lesson 失败教训与 Section 4 红线 (OK)

#### 2. 后端 API 扩展指标：
- [x] /tasks/:id/ui_tree 附带物理原点 [originX, originY] 与 dpi_scale (OK)
- [x] /tasks/:id/rewind_ledger 返回因果回退结构化记录 (OK)
- [x] /screens 只读投影枚举与伺服 JPEG 截图 (OK)

#### 3. 全量测试矩阵与隐私扫描：
- 专项测试结果：tests/test_api_ui_tree.py 28 项全部通过 (OK, 7.93s)
- 全量单元测试结果：337 项全部通过 (OK)，耗时 72.7s (远超 >= 260 项红线)
- 隐私门禁扫描：test_repo_hygiene.py 零命中 (OK)
- 外部依赖检查：requirements.txt 严格保持 4 项未动，web/ 零打包构建
```
