# 任务委派书：GitHub 发布前隐私清理与工程硬化 (Task Delegation)

- **委派发起人**：项目架构负责人 / 主程序员
- **执行角色**：开发工程师（代码与配置治理） / 安全合规工程师（Git 历史审计）
- **依据规范**：[SPEC.md](SPEC.md) §12、§13 与 [AGENTS.md](AGENTS.md)
- **状态**：已完成签收 (Signed Off)

---

## 一、任务背景

仓库目前已完整落地 Phase 0 至 Phase 4 核心能力（Git 式上下文 DAG 树、双端统一文件基队列、三道打断门禁、单 SubAgent 隔离沙箱、零构建原生 Web 双控控制台）。
在推送到公开 GitHub 仓库前，经过架构级扫描，发现存在缺少 `.gitignore`、Git 历史遗留敏感信息、`report.md` 云盘链接外泄、`README.md` 架构严重脱节等多项安全与规范隐患。

---

## 二、工作分解与验收标准

### 任务 1：创建标准根目录 `.gitignore` (优先级：P0) — [已由架构负责人完成 ✅]
已完成根目录 `.gitignore` 配置，彻底忽略 `.agent/tasks/`、`.agent/queue/` 等全部运行时状态与日志。

### 任务 2：脱敏 `report.md` 与文档校准 (优先级：P1) — [已由架构负责人完成 ✅]
已完成 `report.md` 内所有个人 Google Drive 私有分享链接清理，替换为内部相对路径；`SPEC.md` 标题已统一。

### 任务 3：重写 `README.md` 与实际架构完全对齐 (优先级：P1) — [已由架构负责人完成 ✅]
已完成 `README.md` 重构，如实反映当前 Python + 原生 Web + 文件基 DAG 架构，补充启动指南。

### 任务 4：执行 Git 历史重写与敏感词审计 (优先级：P0) — [已完成审查与签收 ✅]
**目标**：彻底清除历史提交中可能遗留的敏感字面量与旧备份文件。
**具体要求**：
1. 遵循 `SPEC.md` §12 指引，检查当前未推送提交中的内容。
2. 确保已彻底抹除 `.agent/GOAL.md.bak`（历史遗留的第三方账号与敏感端点）。
3. 运行本地隐私门禁测试：`py -3 -m unittest tests.test_repo_hygiene -v`。
4. 确认所有修改被暂存，在获得用户明确批准后执行 commit 重写与 push：
   ```bash
   git add -A
   git commit -m "chore: repository hygiene, zero-build web harness, and docs alignment"
   git push origin main
   ```

---

## 三、交付物检查清单

- [x] 根目录生成 `.gitignore` 并已验证规则有效性
- [x] `report.md` 脱敏完毕，无任何 Google Drive 私有链接
- [x] `README.md` 真实体现当前架构与启动说明
- [x] 增加标准开源 `LICENSE` 文件
- [x] 经过用户批准后完成 Git 提交与安全推送

