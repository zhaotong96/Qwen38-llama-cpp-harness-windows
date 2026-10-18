# 任务委派书：Phase 5B 后端网关守护进程与 CLI 真实联调验收

- **委派发起人**：项目架构负责人 / 主程序员
- **执行角色**：开发工程师（后端与Daemon）
- **前置依赖**：Commit `3f9156b`（仓库卫生与发布治理已完成，全量 107 个单元测试绿灯通过）
- **权威契约**：[SPEC.md](SPEC.md) §7（HTTP API 契约）、§6（CLI 契约）、§8（状态一致性模型）
- **状态**：待领用 (Pending Assignment)

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

- [ ] `python -m core.api` 能够无报错持续运行
- [ ] `/health` 返回正确的 `ok=true` 且附带当前模型配置
- [ ] `/skills` 能够列出全部 13 个原子技能
- [ ] `/queue` 写入能在 `.agent/queue/` 产生可读明文文件
- [ ] `/interrupt` 写入能在磁盘产生 `abort.flag`
- [ ] `main.py` CLI 正常启动、列出任务并干净退出
- [ ] 发现的任何代码缺陷直接就地提交修复 PR 或 Issue 记录
