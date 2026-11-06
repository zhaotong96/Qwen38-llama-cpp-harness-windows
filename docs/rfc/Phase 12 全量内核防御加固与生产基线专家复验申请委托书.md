# Qwen38-llama-cpp-harness-windows Phase 12 具身常驻中枢与自主演进系统：全量内核防御加固与生产基线专家复验申请委托书

## 一、委托综述与复验背景

本委托书系项目负责人针对外部架构专家团此前出具的《Phase 12 具身常驻中枢与事件驱动自主演进系统：架构专家穿透式协查与终审验收报告》（文档编号：`RFC-20261102-PHASE12-EXPERT-ACCEPTANCE-VERDICT`）所正式发起的**全量内核防御性加固复验与生产基线固化终审专家审计请求**。

| 审计元数据维度 | 规格内容与核验基线 |
| :--- | :--- |
| **委托书编号** | `RFC-20261102-PHASE12-HARDENING-REVIEW-REQ` |
| **关联前序报告编号** | `RFC-20261102-PHASE12-EXPERT-ACCEPTANCE-VERDICT`（终审验收报告） |
| **演进基线跨度** | Phase 11 Milestone (`81412dc`) ➔ Phase 12 Milestone Candidate + 5 项安全硬化加固 |
| **物理宿主与端点拓扑** | Windows 真实桌面宿主 / 远端 Tesla M40 异构计算 / `Qwen3.8-27B-Uncensored-IQ4_XS.gguf` |
| **运行时依赖规模** | `requirements.txt` 严格锁定 **4 项**（全量加固 100% 纯 Python 3.10+ 标准库实现） |
| **系统核心哲学** | 严格贯彻 [docs/SPEC.md](../SPEC.md) §2 确立的 **KISS 原则与奥卡姆剃刀**（最小充分机制，零外部依赖，零越权特权调用） |

在前序终审验收中，专家团给出了**“准予终审通过（Conditional Pass）并签发里程碑晋级许可”**的积极裁决，并作为附随技术指导提出了 5 项深水区内核级边界防御机制建议（Patch-12A-1 至 Patch-12D-1）。项目团队已遵照系统最高架构准则（KISS 与奥卡姆剃刀原则），将全部 5 项加固的数据与逻辑闭环完整合入工程核心，并对任何潜在危害宿主稳定性和触发安全沙箱警报的系统越权操作（`os.kill` 探测与 `taskkill` 清剿）进行了彻底清零与安全净化。

现特请领域专家团对本次加固成果及当前准备提交的 Phase 12 生产基线执行最终的穿透式复验与代码审计。

---

## 二、五大安全防御加固实现与代码地图

本次加固严格坚守“零新增外部依赖”、“万物皆文件”及“纯原生零构建”三大铁律，全部代码均基于 Python 3.10+ 标准库（`os`, `time`, `random`, `secrets`, `hmac`, `importlib`, `ast`）实现。

| 补丁编号与加固专项 | 目标代码路径 | 核心机制与物理代码落点 | 消除的深水区系统边界风险 |
| :--- | :--- | :--- | :--- |
| **Patch-12A-1：NTFS 原子替换抗抖动与单飞锁活跃心跳** | [core/event_bus.py](../../core/event_bus.py)<br>[core/agent.py](../../core/agent.py) | 1. 封装 `_atomic_replace_retry(src, dst)`（捕获 `WinError 5/32/33` 执行 4 次指数抖动退避重试）；<br>2. 封装 `touch_flight_slot(marker)`（纯基于 `os.utime` 安全刷新锁文件 mtime）；<br>3. `core/agent.py` 的 `run_daemon` 循环调度中全程调用心跳刷新；<br>4. 彻底拔除一切 `os.kill` 进程探测代码。 | 免疫 Windows Defender / 索引器并发扫描导致的瞬态句柄独占冲突；根除 7×24h 守护进程连续运行超 6 小时被误判为陈旧锁的假阳性脑裂风险。 |
| **Patch-12A-2：紧急打断未配对工具回执合成补齐** | [core/agent.py](../../core/agent.py) | 1. 实现 `_ensure_bipartite_tool_pairing(self)`；<br>2. 遍历末尾 `assistant` 消息的 `tool_calls`，对未闭合调用自动补齐注入带有 `"status": "ABORTED_BY_LEVEL0_EMERGENCY"` 的结构化 `role: "tool"` 回执；<br>3. 在紧急打断及任务落盘 Checkpoint 前强制调用。 | 确保中断场景下的 ChatML 消息历史严格遵循 OpenAI 工具调用二部图配对不变性，消除恢复上下文时远端 llama-server 抛出的 400 Bad Request 协议异常，守护 1024-Token 网格对齐。 |
| **Patch-12B-1：大文件动态晋升 Level 3 与软链接防逃逸** | [core/saga_compensator.py](../../core/saga_compensator.py) | 1. `classify_action_reversibility` 中增加大文件检测：已存在且体积 $> 1\text{MB}$ 的目标文件自动从 Level 2 晋升为 Level 3 挂起工单；<br>2. 结合 `_resolve_repo_path` 真实路径解析与 `cand.is_symlink()` 软链接穿透校验，严防逃逸或篡改红线文件。 | 消除超 1MB 文件无全量备份导致的不可逆覆写回滚盲区；彻底阻断利用 NTFS 目录联接点（Junction）或符号链接绕过 `REDLINE_WRITE_PATHS` 的安全漏洞。 |
| **Patch-12C-1：宿主内存态临时 HMAC 验签** | [core/subagent_gate.py](../../core/subagent_gate.py) | 1. 宿主内存随机私密密钥 `_HOST_EPHEMERAL_KEY = secrets.token_bytes(32)`（不落盘、不透传环境变量）；<br>2. `generate_verification_ticket` 签发时生成 HMAC-SHA256 签名 `signature_hmac`；<br>3. `verify_ticket_integrity` 校验 HMAC 签名；<br>4. 彻底拔除一切 `taskkill` 外部系统命令，清理回归 Git 原生操作。 | 将三阶接力流水线（Scout/Builder/Verifier）票据防伪从公开 SHA-256 完整性检验提升至密码学不可伪造级别（EUF-CMA），根除沙箱子智能体篡改票据伪造绿灯的道德风险。 |
| **Patch-12D-1：重结晶前缀原子步解包与模块缓存失效** | [core/macro_compiler.py](../../core/macro_compiler.py)<br>[core/skill_sandbox.py](../../core/skill_sandbox.py) | 1. 实现 `_extract_unrolled_stale_macro_prefix`，静态解析旧宏 AST 的 `MACRO_STEPS`，将已成功执行的前 `completed_steps_count` 个底层原子动作解包拼接至新宏最前端；<br>2. 技能热部署后调用 `importlib.invalidate_caches()` 并清理 `sys.modules` 对应模块缓存。 | 彻底攻克宏断点接管自愈中的“前缀失忆悖论”，确保新宏拥有从 Step 0 到最终态的端到端完整闭环，且杜绝宏自递归调用；消除高频热重结晶下的 `.pyc` 字节码陈旧失效。 |

---

## 三、请求领域专家重点协查与审计的五大议题

为确保生产基线固化前在系统控制论与工程实践上的绝对可靠，特请专家团对以下五大议题展开重点穿透式复核：

### 协查议题 1：纯文件时间戳心跳（`os.utime`）在单飞锁防脑裂中的充分性
在彻底拔除具有特权安全隐患的 `os.kill` 探测后，系统仅依托 `touch_flight_slot` 每回合调用 `os.utime` 刷新锁文件 mtime。请专家核验：在 Windows NTFS 文件系统的时间戳分辨率与文件属性缓存机制下，该心跳方案是否已足以在数学与工程双重意义上 100% 杜绝运行超过 6 小时的常驻守护进程被外部实例误判破锁？

### 协查议题 2：合成回执 `ABORTED_BY_LEVEL0_EMERGENCY` 的协议自洽性
请专家推演：当多工具并行调用在执行第 $k$ 个工具时被阻断，系统自动注入包含结构化 JSON 载荷（`ok: false`, `status: "ABORTED_BY_LEVEL0_EMERGENCY"`）的合成 `role: "tool"` 回执后，落盘的 Checkpoint 在后续被 `resume_task` 或新任务载入时，远端 llama-server 的 ChatML 渲染器是否能 100% 顺畅解析，且是否满足 DeltaNet 1024-Token 网格序列化边界？

### 协查议题 3：Saga 补偿器在混合文件类型下的动态晋升与路径规范化
请专家审查：[`core/saga_compensator.py`](../../core/saga_compensator.py) 对 `REDLINE_WRITE_PATHS`（`requirements.txt`, `docs/SPEC.md`, `package.json`）的防护以及对 $> 1\text{MB}$ 文件的 Level 3 晋升逻辑，在面对相对路径（如 `./docs/SPEC.md`）、反斜杠路径（Windows 格式）以及跨目录符号链接时，是否存在任何路径规范化解析逃逸漏洞？

### 协查议题 4：基于 `secrets` 的进程生命周期 HMAC 签名的密码学边界
请专家从对抗博弈视角评估：在宿主 Python 进程加载 `core/subagent_gate.py` 时生成的 256-bit 随机密钥 `_HOST_EPHEMERAL_KEY`，由于其驻留在宿主私有堆内存中且绝不写入磁盘或环境变量，在 Worktree 独立沙箱中运行的 Builder 或其他子进程，是否存在任何信息论上伪造该票据 HMAC 签名的可能性？

### 协查议题 5：宏前缀原子步解包代数（Unroll-Prefix）的端到端完备性
请专家形式化核查：[`core/macro_compiler.py`](../../core/macro_compiler.py) 中的 `_extract_unrolled_stale_macro_prefix` 函数，直接解包旧宏 AST 语法树中 `MACRO_STEPS[:completed_steps_count]` 的底层原子动作（`op` 与 `args`），与主模型接管后提纯出的主干（Winning Spine）进行首尾缝合，是否在任何情况下均能保持宏定义的静态纯度（AST 顶层零调用、Schema 校验、独立进程干运行三闸）？新生成的技能是否既无前置动作丢失，又完全免疫无限递归？

---

## 四、四大架构不变性符合性核验基准

本委托书郑重确认，当前准备提交的 Phase 12 生产候选基线无条件恪守以下四大架构红线：

1. **依赖纯净性**：`requirements.txt` 保持 4 项依赖不变，全套 5 项加固零新增第三方包；
2. **零构建 Web 控制台**：绝无引入任何 Node.js、npm 或打包构建工具；
3. **UNIX-like 透明落盘契约**：所有事件、工单、清单、票据与账本均以结构化明文 JSON 落盘；
4. **仓库整洁与路径规范**：全篇文档与代码均使用标准相对路径，严禁任何本机绝对路径。

---

## 五、委托总结与期望结论

项目团队已对交付物全景与安全加固补丁完成了细致的自测与加固净化。请外部架构专家团依照上述协查议题开展穿透式复核，并在审计完成后出具复验审计结论书，以便项目负责人正式签署 Phase 12 生产里程碑提交（Git Commit & Push）并发布基线固化决议。
