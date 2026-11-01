我将对这个仓库做推送前最终审计。先建立任务清单，然后系统性地进行只读检查。

首次发现关键线索：存在 `backup-pre-redact` 本地分支（脱敏前的备份），且 main 与 backup 在**同一提交信息但不同哈希**（`125d2b6` vs `30f57d1`），说明历史曾被重写脱敏。这将是本次审计的重点风险面。继续深挖：

网络对 github.com 被重置（沙箱环境无法直接核验远端），本地继续深挖历史重写的本质：

历史重写的本质已确认：**旧提交元数据含真实个人邮箱 `[REDACTED]`，新历史改为 GitHub noreply 邮箱**，树内容完全一致。继续核查悬空对象与全部提交元数据：

旧历史 8 个提交携带真实邮箱，且存在含旧邮箱的悬空 stash 对象（本地留存）。现在转入工作区内容审计——先盘点将推送的文件与全库敏感模式扫描：

无真实密钥泄露（测试中的 `sig_v7_*` 为合成 fixture），但发现**本地用户名 `zt` 泄露在已跟踪文档中**，以及一个可疑的未跟踪文件 `docs/rfc/.probe`。继续核查将推送的未跟踪内容与关键配置：

README 结构清晰，包含大量可验证声明。现在逐条对照实现进行核查：

核心声明基本吻合（4 依赖 ✓、GEMMA 环境变量 ✓、默认回环绑定 ✓、端口 8765 ✓）。但发现疑点：**README 称“22 个测试套件”，实际 `test_*.py` 已有 28 个**。继续核查并启动全量测试验证：

路径笔误，用绝对路径重跑全量测试：

docs/README.md 导航发现两个问题：引用了被 .gitignore 排除的 `.gdoc` 文件，且未收录将提交的新文档（handover/、Phase 12 RFC）。继续核实：

`.gdoc` 本地文件含真实 Gmail（已被 gitignore 挡住，不会推送，但 docs/README.md 导航引用了它们）。交接文档暴露 `G:\My Drive` 工作区路径。现在检查最关键的一点——**git 配置的提交身份是否会再次引入真实邮箱**：

提交身份配置正确（noreply 邮箱），fail-fast 验证通过。继续核查待推送增量与 README 其余声明：

全量测试 **393 项，OK（2 skipped），105.9s** —— 与 README 宣称的 393 一致 ✓。完成最后一批核查：

发现 `docs/rfc/Phase 11 架构专家验收报告.md` 内嵌大量 base64 图片（输出膨胀到 851KB）。且 SPEC.md §12 有专门的“发布检查”清单——这是仓库自定的推送前门槛，必须逐条对照：

SPEC §12 是仓库自带的发布门禁（含特定敏感模式扫描与 hygiene 测试）。现在**按仓库自己的门禁标准执行检查**：

门禁扫描通过 ✓，`3bb63a5` 已在重写中消失（SPEC §12 的背景叙述已过时）。hygiene 测试在嵌入式环境下按 SPEC 原命令无法导入——用发现模式重跑，并摸清 backup 分支的泄露半径：

backup 分支的**文件内容**干净（历史重写同时清理了内容），其唯一 PII 是提交元数据中的旧邮箱。最后几项确认：

门禁模式清单已明确（含 7 类，但**不含个人邮箱/本地用户路径**——盲区）。最后验证第三方依赖许可证兼容性：

所有证据收集完毕，依赖许可均为宽松型（mss 为 MIT，与另三项一致）。汇总输出审计报告：

# Qwen38-llama-cpp-harness-windows 推送前最终审计报告

**审计基线**：`main @ 125d2b6`（已与 `origin/main` 同步），审计对象 = 工作区 11 个修改文件 + 19 个未跟踪路径（推送时将首次公开的增量）。
**审计方法**：只读检查 + 按仓库自带门禁（SPEC §12）执行复验 + 全量测试实测 + git 对象级取证。

**总裁决：🟡 有条件通过 —— 无任何真实密钥/密码泄露，可推送；但存在 2 项必须先决策的事项、若干文档-实现相悖项（见下）。“有条件”的核心不是安全污点，而是两个不可逆成本点：base64 图片入库与 `git add -A` 误收文件。**

---

## 一、Git 历史取证：脱敏重写的现状与残留风险

这是本次审计最重要的发现面。本地存在 `backup-pre-redact` 分支（30f57d1），与 main 同信息不同哈希、**无共同祖先**（`git merge-base` 为空）——这是整体历史重写的特征，而非单次 amend：

| 证据 | 结果 |
| --- | --- |
| 树哈希对比 | `main^{tree}` == `backup^{tree}` == `7b960644`，**文件内容完全一致**，重写只动了提交元数据 |
| 旧历史身份 | 10 个提交中 **8 个 author/committer 为 `tong zhao <[REDACTED]>`**（真实个人邮箱） |
| 新历史身份 | main 全部 10 个提交均为 `40412861+zhaotong96@users.noreply.github.com` ✓ |
| 悬空对象 | `git fsck` 发现 2 个 unreachable stash 提交（55d85f9、6e97902），同样携带旧邮箱 |
| 旧内容检查 | backup 分支树内 **无** SPEC §12 所述的 `GOAL.md.bak`、内网端点字面量（`192[.]168.*`、`douyin`、`MS4w` 全部为空）——旧历史的内容污点仅剩**提交元数据邮箱**一项 |
| SPEC §12 时效 | `3bb63a5` 已不存在（对象级确认），§12 中“git 历史里仍存在旧版本内容，直接 push 会永久公开”的背景叙述**已过时**（详见 §三-C1） |

### 推论与风险

1. **`backup-pre-redact` 是一颗“PII 地雷”**：文件内容干净，但 8 个提交的作者邮箱是真实邮箱。任何 `git push --all` / `--mirror` / 未来误推该分支，都会把已清除的 PII 重新公开。建议：明确保留/删除决策；无论保留与否，**立即执行 `git stash clear` + `git gc --prune=now` 清掉两个含旧邮箱的悬空 stash 对象**。
2. **本地无法闭合的核验项**：若重写**之前**旧历史曾被推送过，GitHub 服务端仍会按 SHA 提供旧提交（GitHub 不即时 GC unreachable 对象）。本环境对 github.com 网络被重置，无法代查。请推送前在浏览器访问一次：
   `https://github.com/zhaotong96/Qwen38-llama-cpp-harness-windows/commit/30f57d1`
   —— 返回 404 则旧历史从未出过门，闭环；返回 200 则需要联系 GitHub Support 清除或改用新仓库。
3. **新提交身份安全**：本地 config 已锁定 noreply 邮箱（`user.email=40412861+zhaotong96@...`），本轮提交**不会再**引入真实邮箱 ✓。

---

## 二、敏感信息与开源合规扫描

### 2.1 绿灯项（逐条实证）

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| 真实密钥/密码/token | **无** | `core/config.py:165` API key 全部走环境变量、默认 `"local"`；`tests/benchmark_long_horizon.py:44-49` 的 `sig_v7_99a8b7`/`BENCH_SECRET_PORT` 为合成测试 fixture |
| SPEC §12 门禁 grep（192[.]168/MS4w/douyin//home/*/bin/） | **空，通过** | 按原命令执行 |
| `test_repo_hygiene` 自动门禁 | **OK**（需 discovery 模式运行） | 7 类模式：192[.]168 / MS4w / douyin[.]com / /home/*/bin/ / `Z:\[W]ork` / zt[-]nas / `file:///` |
| 个人邮箱（内容层） | **无**（outlook/gmail/qq 全域扫描） | 但注意 `.gdoc` 本地文件与提交元数据两处例外，见下 |
| 硬编码 IP | **无**（仅 127.0.0.1 回环） | `web/` 仅 SVG namespace 一个 URL，干净 |
| LICENSE | MIT，版权行 `Qwen38-llama-cpp-harness-windows contributors`（不含真实姓名，处理得当） | LICENSE:3 |
| 第三方依赖许可 | 4 项全为宽松许可，与 MIT 兼容、无 copyleft 传染：openai (Apache-2.0)、mss (MIT)、pillow (HPND/MIT-CMU)、websocket-client (Apache-2.0) | requirements.txt 与 PyPI 核对 |
| 无 vendored 第三方代码 | `web/` 为自研原生 ES Modules，无 node_modules/vendor 目录 | 目录遍历 |
| 运行态产物隔离 | `.agent/usage.jsonl`、`.agent/tasks`、截图 jpg 等均被 `.agent/*` 规则忽略（`check-ignore` 实测）；`logs/*` 仅 `.gitkeep` 入库 | .gitignore:9 |

### 2.2 黄灯项（存在但可控/需决策）

| # | 发现 | 位置 | 定性 |
| --- | --- | --- | --- |
| P1 | `.gdoc` 本地文件内含**真实 Gmail**：`[REDACTED]` | `docs/notes/*.gdoc`（2 个文件） | **不会被推送**（`*.gdoc` 已 gitignore），但 docs/README.md 导航公开引用了这两个文件名（见 D1），且 hygiene 门禁不覆盖邮箱模式——一旦未来有人放宽 gitignore 就会泄露 |
| P2 | 本地用户名 `zt` 已在**已推送**的 tracked 文档中 | `docs/ops/OPS_LLAMA_SERVER_UPGRADE.md:40`（`[REDACTED]/...`）、`docs/tasks/TASK_DEV_SYS_SANDBOX.md:91` 等 4 处（`[REDACTED]\...`） | 存量披露，非本轮增量。`zt` 为两字母缩写，指向性有限但可与 git 用户名关联。改文件无法收回已推送内容，仅在下次历史重写时一并处理——**建议接受现状**，但门禁应加模式防止增量 |
| P3 | 真实模型名 `Qwen3.8-27B-Uncensored-IQ4_XS.gguf`（"Uncensored" 字样）出现在已推送 tracked 文档 | `docs/notes/report.md:176`、`docs/ops/:40-44` | 同为存量披露。开源合规上不违规，但属于运营选择问题：公开仓库明示运行 uncensored 权重，可能引来社区标签化。**新增文档（验收报告：6、:12）延续同等级披露，未越界** |
| P4 | 拟提交的 handover 文档含物理工作区路径 `G:\My Drive\work\...`、前序会话 UUID、以及两处 `git push --force-with-lease` 指令 | `docs/handover/HANDOVER_PHASE10_TO_PHASE11.md:9,78,103` | 轻度环境信息泄露 + 对后续 AI 会话的强指令文本入库。建议提交前把该行路径泛化为 `<workspace>`，或暂缓提交此文档 |
| P5 | **根目录 `report.md`**（未跟踪，170 行）是未编辑的 AI 会话叙事实录，含真实模型名与逐过程排错细节 | 根目录 | 与 `docs/notes/report.md`（正式汇报单，188 行）**内容完全不同**。按 SPEC §12 的 `git add -A` 指引操作会把它误收进库。**建议：移出仓库或加入 .gitignore** |

---

## 三、文档-实现一致性核查（README/SPEC/AGENTS 声明 vs 代码）

### 3.1 逐条验证通过（✓ 清单）

| 声明 | 实证 |
| --- | --- |
| “仅依赖 4 个基础库” | requirements.txt 恰好 4 行 |
| `GEMMA_BASE_URL`/`GEMMA_MODEL` 环境变量及默认值 | `core/config.py:163-164` |
| HTTP API 默认回环绑定、端口 8765、`py -3 -m core.api` 可启动 | `core/api.py:1070-1071,1096` |
| `.agent/AGENTS.md` 缺失即 fail-fast | `core/agent.py:264-266` `raise RuntimeError` |
| `--resume` / `--goal` CLI 参数 | `main.py:222-233,251` |
| shell skill = PowerShell 7 | `skills/shell.py` → `core/tools.py:674` 确以 `pwsh` 调起 |
| 单飞 409 保护 / SubAgent `active.lock` 互斥 | `core/api.py:212,296,952`；`core/subagent_runner.py:25,72` |
| web/、skills/ 目录树所列文件全部存在 | 目录遍历 |
| **“393 项测试”** | **实测：`Ran 393 tests in 105.879s, OK (skipped=2)`** ✓（与 README:106、Phase 11 验收报告宣称一致） |

### 3.2 相悖项（❌ 需修正）

| # | 相悖点 | 证据 | 影响 |
| --- | --- | --- | --- |
| D1 | **docs/README.md 导航引用 2 个 `.gdoc` 文件，但它们被 gitignore 排除**——公网读者按图索骥 404 | docs/README.md notes/ 小节 vs .gitignore:11 | 导航完整性失真；且间接暴露本地文件存在性 |
| D2 | **README:106 “22 个自动化测试套件” 已失真——实际 28 个 `test_*.py`** | `ls tests/test_*.py \| wc -l` = 28 | 测试规模宣称缩水（22 < 28），属低估型失真，但仍是文档与仓库状态不同步 |
| D3 | **SPEC §12 背景叙述过时且操作指引不可执行**：所述 `3bb63a5` init 提交已不存在（历史整体重写），"amend 未推送 init 提交" 流程会误导下一位维护者 | `git cat-file -t 3bb63a5` → fatal | 中：门禁 grep 部分仍有效，叙事部分需重写 |
| D4 | **SPEC §12 命令 `py -3 -m unittest tests.test_repo_hygiene` 在本项目实际部署形态（嵌入式 Python，`._pth` 隔离）下必然失败**（`ModuleNotFoundError: No module named 'tests'`），须用 `discover` 形式；与 README:137 的 discover 命令不一致 | 本次实测复现 | 低：标准 Python 环境可跑，但与项目自述的运行环境矛盾 |
| D5 | **README web/ 树只列 5 个文件，实际 10 个**（缺 store.js、inspector.js、fact_ledger.js、canvas_overlay.js、dag_timeline.js） | 目录遍历 | 低：同 D2，欠同步 |
| D6 | **docs/README.md 未收录将提交的新文档**：`docs/handover/`、Phase 11 验收报告/委托书、Phase 12 RFC | git status 未跟踪清单 vs docs/README.md 全文 | 若这些文档入库而索引不更新，导航 immediately stale |
| D7 | **命名体系三元冲突**：仓库名 Qwen38 / README 称"Qwen 3.8 等模型" / 环境变量与运行时人格为 `GEMMA_*`+"你是Gemma"（.agent/AGENTS.md:5）。Gemma 是 Google 商标，作为 agent 人格与环境变量前缀易生混淆 | README:14,43-44；.agent/AGENTS.md | 低（不阻断）：建议 Phase 12 统一命名 |

---

## 四、推送不可逆成本警告（本轮独有的最后窗口）

1. **~1.24MB 的 base64 内嵌图片即将永久入库**：
   - `docs/rfc/Phase 12 架构专家咨询方案.md` — 660KB / **105 张** data-URI 图片
   - `docs/rfc/Phase 11 架构专家验收报告.md` — 609KB / **35 张**
   - 已推送的先例（Phase 10 方案研究.md，640KB）说明这是既定风格，但 git 对象库**只增不减**——一旦提交，未来任何瘦身都要再改写历史（与“不再改历史”的原则冲突）。**建议现在**（提交前）把图片抽出为 `docs/assets/*.png` 或剥离装饰性截图。这是最后一个零代价修正点。
2. **`git add -A` 是 SPEC §12 的字面指引**——当前工作区有 19 个未跟踪路径，其中根 `report.md`（P5）、handover（P4）是否入库需要**显式决策**而不是被 `-A` 顺手带走。
3. 顺带确认：根目录 `docs/Phase 10 验收与 Phase 11 架构前瞻.md` 的工作区删除是**正确的去重移动**（docs/rfc/ 下已有同名 tracked 文件），提交时保留删除即可。

---

## 五、其他工程建议（非阻断）

- **补 `.gitattributes`**：本次 git 对 9 个文件报 LF→CRLF 警告；跨平台开源协作必然产生行尾噪声提交。建议 `* text=auto` + `*.py text eol=lf` + `*.bat text eol=crlf`。
- **扩展 `test_repo_hygiene` 门禁模式**：现有 7 类不覆盖个人邮箱、`C:\Users\<name>`、`G:\My Drive`、`[A-Za-z]:\\My Drive`——本次审计发现的三类现实污点（.gdoc Gmail、[REDACTED]、G:\My Drive）全部落在盲区。建议按现有“拆串防自匹配”风格追加 3-4 条。
- **删除 tracked 空文件 `docs/rfc/.probe`**（0 字节，`git ls-files` 确认在库）：`git rm docs/rfc/.probe`。
- **api.py 加 `--host` 非回环运行时警告**：AGENTS.md 已声明回环默认与风险，代码默认正确，但绑定非回环时静默通过——一行 logger.warning 即可与文档形成闭环。
- **“专家”文档的出处披露**（由你定夺）：Phase 11 验收报告以“领域架构专家团……一致裁定”口吻出具正式裁决（并含“微软 MVP”呈递语境）。若该评审实为 AI 辅助咨询产物，公开仓库中建议在 docs/README.md 或文档头部加一行来源说明（如“AI 辅助架构咨询记录”），避免外部读者误判为独立人类第三方评审——这属于开源透明度的诚信项，不是合规硬伤。
- MIT 项目不强制第三方声明文件，但可选加一个简短 `THIRD_PARTY_NOTICES.md` 列 4 依赖及其许可证，与 README 徽章呼应。

---

## 六、推送前行动清单（按优先级）

| 优先级 | 动作 |
| --- | --- |
| **必须** | 浏览器核验旧 SHA 远端可达性（§一.2）——唯一无法本地闭合的项 |
| **必须决策** | 根 `report.md` 与 `docs/handover/` 是否入库（建议：report.md 移出/忽略；handover 泛化路径后入库或暂缓） |
| **强烈建议** | Phase 12 RFC / Phase 11 验收报告的 base64 图片抽出（§四.1） |
| 建议 | 修 D1/D2/D6：docs/README.md 去 .gdoc 引用、补新文档索引；README "22"→28 |
| 建议 | 重写 SPEC §12 背景叙述 + 统一测试命令为 discover 形式（D3/D4） |
| 建议 | `.gitattributes`、删 `.probe`、`git stash clear` + `git gc --prune=now` |
| 可选 | hygiene 门禁扩模式、api.py host 警告、专家文档出处声明 |

**底线结论**：代码、测试、密钥卫生、许可证四个硬性维度全部干净且 README 核心声明可复现（393 测试实测复验通过）。只要完成上表前两项决策并处理好 base64 膨胀，本轮推送在开源合规层面是安全的；`backup-pre-redact` 分支保持本地、永不推送，即可把历史重写的残留风险收敛为零。

Sources: [python-mss (PyPI) — MIT License](https://pypi.org/project/mss)