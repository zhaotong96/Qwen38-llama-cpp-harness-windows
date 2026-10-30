# 任务委派书：Phase 5B 后端网关守护进程与 CLI 真实联调验收

- **委派发起人**：项目架构负责人 / 主程序员
- **执行角色**：开发工程师（后端与Daemon）
- **前置依赖**：Commit `3f9156b`（仓库卫生与发布治理已完成，全量 107 个单元测试绿灯通过）
- **权威契约**：[SPEC.md](SPEC.md) §7（HTTP API 契约）、§6（CLI 契约）、§8（状态一致性模型）
- **状态**：已完成验收（2026-10-18；执行：开发工程师-后端与Daemon；回执见文末"四、验收回执"）

---

## 一、任务背景与目标

目前系统全量代码与 14 个测试套件（基于 Mock 的单元测试）已全部通过。本任务的核心目标是**脱离 Mock 测试，在 Windows 本地宿主环境中真实启动守护进程与 CLI**，完成真实环境下的进程健壮性、端点功能性与断电自愈机制验收。

---

## 二、工作范围与具体执行步骤

### 1. 启动 Unified Agent Web Daemon (网关守护进程)
在仓库根目录下运行标准启动命令：
```bash
python -m core.api --host 127.0.0.1 --port 8765
```
**检查要点**：
- 服务是否成功绑定 `127.0.0.1:8765`；
- 启动时是否自动执行孤儿锁清理与断电自愈（检查 `.agent/subagents/active.lock` 与未决排队项）；
- 控制台日志是否正常输出。

### 2. 真实 REST API 核心接口连通性验证
使用 `curl` 或 Python 脚本发起针对本服务的只读与控制测试：
```bash
# 存活探针与运行时状态
curl http://127.0.0.1:8765/health

# 技能注册表投影（必须与 skills/ 目录文件严格对应）
curl http://127.0.0.1:8765/skills

# 会话指针与任务历史读取
curl http://127.0.0.1:8765/session
curl http://127.0.0.1:8765/tasks

# 队列读取
curl http://127.0.0.1:8765/queue
```
**验收标准**：全部返回 HTTP 200/JSON，字段契约符合 [SPEC.md §7.1](SPEC.md#L149)。

### 3. 排队与打断机制真实通道验证
- 向 `/queue` 提交一条测试消息：
  ```bash
  curl -X POST http://127.0.0.1:8765/queue -H "Content-Type: application/json" -d "{\"content\":\"test step\",\"priority\":50,\"status\":\"queued\"}"
  ```
  检查 `.agent/queue/` 目录下是否物理生成了标准字典序命名的 `.json` 文件。
- 测试紧急免锁打断通道：
  ```bash
  curl -X POST http://127.0.0.1:8765/interrupt -H "Content-Type: application/json" -d "{\"reason\":\"test abort\",\"mode\":\"reject_execution\"}"
  ```
  检查当前任务目录下是否生成了 `abort.flag`。

### 4. CLI 薄前端冒烟测试
在另一个终端中测试命令行入口交互：
```bash
# 1. 交互式启动与退出测试
python main.py
# 输入 exit 或 quit 退出

# 2. 列出恢复任务
python main.py --resume
```

---

## 三、交付物与验收清单

请在完成验证后，在下方回填测试结果：

- [x] `python -m core.api` 能够无报错持续运行（守护进程持续运行；本机嵌入版 Python 隔离模式阻断字面命令，改用等效引导命令，详见回执 §1）
- [x] `/health` 返回正确的 `ok=true` 且附带当前模型配置
- [x] `/skills` 能够列出全部 13 个原子技能（注册表全量 19 项 = 13 原子 + 6 元技能，13 项均在列）
- [x] `/queue` 写入能在 `.agent/queue/` 产生可读明文文件
- [x] `/interrupt` 写入能在磁盘产生 `abort.flag`
- [x] `main.py` CLI 正常启动、列出任务并干净退出
- [x] 发现的任何代码缺陷直接就地提交修复 PR 或 Issue 记录（仅记录环境缺陷 1 项；无仓库代码缺陷）

---

## 四、验收回执（执行人：开发工程师-后端与Daemon；日期：2026-10-18）

**环境**：Windows 本机；`python` 3.14.2（嵌入式发行包，`python314._pth` 隔离模式）；`py -3` 启动器不存在（report.md 已记录）；模型端点 `http://127.0.0.1:8080/v1` 未启动，`n_ctx` 探测按设计回退 32768。仓库代码零改动，`requirements.txt` 保持 4 项依赖。

### 1. 守护进程启动与断电自愈
- 本机唯一解释器为嵌入式 Python：cwd 不入 `sys.path` 且忽略 `PYTHONPATH`（已实测），字面命令 `python -m core.api` 在本机必然 `ModuleNotFoundError`（环境缺陷，非仓库缺陷；标准 CPython 下字面命令按 CPython 语义可直接运行）。改用等效引导（不触碰仓库代码与 `core/api.py` 的标准包导入契约）：
  `python -c "import sys, os; sys.path.insert(0, os.getcwd()); from core.api import main; sys.exit(main())" --host 127.0.0.1 --port 8765`
- 服务持续运行无 ERROR（PID 18760，仍在运行，供前端联调使用；停止：`Stop-Process -Id 18760`）。启动横幅输出 api/web/model/cwd/skills/state 六行，skills 行列出全量 19 个工具。
- **断电自愈**（预置受控崩溃残留：`active.lock`→`sub_20261018_120000_000000` 且其 status=running；队列项 `qcrash01` status=processing）：启动后 active.lock 被清除、子代理 status → `cancelled`、队列项 processing → `queued`（audit 记录 `self_healing/reset_processing_to_queued`）。

### 2. REST 端点冒烟（全部 HTTP 200 / JSON）
| 端点 | 结果 |
| --- | --- |
| `GET /health` | `{"ok":true,"model":"local-model","base_url":"...","n_ctx":32768,"busy":false,"active_subagent":null,"queue_count":2,...}` |
| `GET /skills` | 19 项（13 原子 + 6 元：add_skill/modify_skill/read_skill/subagent/task_done/task_blocked），与模型工具注册表一致 |
| `GET /session` `/tasks` `/queue` | 200；`/tasks` 与磁盘 checkpoint 一致；`/queue` 返回恢复项与新增项 |
| `GET /` | 200 端点索引（32 项）；非法 JSON 请求 → 400 `{"error":...}`（契约一致） |
| `GET /events?once=1` | SSE 正常：回放 live_stream.jsonl（session_changed、interrupt_set）+ snapshot 帧 |
| `GET /web/index.html` | 200 `text/html; charset=utf-8`（前端联调就绪） |

### 3. 排队与打断落盘
- `POST /queue` → 201；磁盘生成标准字典序文件 `50_20261018_061738_984117_qcff677.json`（`{priority:02d}_{yyyyMMdd_HHMMSS_ffffff}_{id}.json`），内容为可读明文 JSON（含 seq/audit_log）。
- `POST /session` → 建立会话 `20261018_061742_933333`，写 `.agent/current_session`。
- `POST /interrupt`（无 task_id，命中 current_session）→ 200 `{"ok":true,...}`；生成 `.agent/tasks/20261018_061742_933333/abort.flag`；`GET /tasks/<id>/step_state` 同步可见 abort_flag。

### 4. CLI 薄前端冒烟
- `python main.py`：交互 Shell 正常启动，输入 `exit` → `bye`，退出码 0。
- `python main.py --resume`：列出任务 `20261018_060000_000001 created=2026-10-18T06:17:56 status=done branch=main messages=2`，退出码 0；与 `GET /tasks` 看到同一份磁盘事实。（种子任务经 `core.persistence.save_checkpoint/save_status` 正式落盘生成，用于验证列表展示链路。）
- 已知噪音：模型不可达时启动前有 `[ctx] context probe failed ... using fallback` 警告（预置日志 handler 之前输出，report.md 已记录同类现象），不影响功能与退出码。

### 5. 红线核验
- **单飞锁（Single-flight）**：后台以 `POST /skills/shell`（`ping -n 6` 约 5 秒）占锁期间发起 `POST /tasks` → **409** `{"error":"agent busy"}`；skill 200 返回后 `/health busy=false`。锁未被破坏。
- **依赖红线**：`requirements.txt` 仍为 4 项，无新增第三方依赖；仓库代码未改动（工作树仅本文件回填）。

### 6. 回归基线
- `python -m unittest discover -v`：**Ran 107 tests … OK**（32.2s），与前置 Commit `3f9156b` 基线一致。

### 7. 缺陷与观察记录
- **环境缺陷（已记录，未修改系统，待负责人决策）**：本机嵌入式 Python 隔离模式使 `python -m core.api` / `py -3` 字面命令不可用；可选本机修复：在 `python314._pth` 追加仓库根路径一行，或改用标准 CPython 安装。仓库侧无需改动。
- **观察（无功能影响，不作代码修改）**：`core/api.py:361` 的 `current_branch = DEFAULT_BRANCH = "main"` 为冗余局部别名；skill 直接调用占锁时 409 响应中 `active_task` 为 null（该字段仅由 submit/resume 赋值，语义正确）。
