# Qwen38-llama-cpp-harness-windows

![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?style=flat-square&logo=windows)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python)
![Architecture](https://img.shields.io/badge/Architecture-KISS%20%7C%20Zero--Build-success?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

> 本地 Windows 计算机操作智能体（Computer Use Agent）长程运行时与 Web/API/CLI 双端协同控制网关。

---

## 🌟 项目定位与设计哲学

本项目为运行在 Windows 宿主机上的本地长程计算机操作智能体，支持连接本地推理服务（如 `llama.cpp` 运行的 Qwen 3.8 等模型）以及标准 OpenAI 兼容端点。系统提供统一单例内核，驱动原生 Web 双控控制台、CLI 交互终端与外部 AI 助手 HTTP API。

### 核心不可动摇原则

1. **单一核心，多前端**：CLI（`main.py`）、HTTP API（`core/api.py`）与原生 Web 控制台仅作为传输层薄前端，共享同一单例 Agent 内核。
2. **万物皆 Skill**：所有操作能力解耦为 `skills/*.py` 自描述脚本文件，遵循标准 OpenAI Function Calling 契约，支持热加载。
3. **万物皆文件 & 透明状态反黑盒**：拒绝黑盒内存数据库或不可见状态。任务快照（`checkpoint.jsonl`）、分支指针（`branches/`）、队列项（`.agent/queue/`）、打断标记（`abort.flag`）均为可读明文文件。
4. **单飞约束（Single-flight）与单 SubAgent 互斥**：同一时刻单任务执行（409 保护）；SubAgent 沙箱通过 `active.lock` 严格单实例互斥，剥离递归元技能，杜绝上下文污染。
5. **KISS / 奥卡姆剃刀**：零构建链（无 Node.js/npm）、标准库 HTTP/SSE 网关；运行时依赖严格精简为 4 项。

---

## 🛠️ 快速开始

### 1. 安装依赖

环境要求：Python 3.11+ (Windows)

```bash
pip install -r requirements.txt
```

*仅依赖 4 个基础库：`openai`（协议客户端）、`mss`（屏幕捕获）、`pillow`（图像处理）、`websocket-client`（Chrome CDP）。*

### 2. 配置推理端点（可选）

系统默认连接本地回环地址 `http://127.0.0.1:8080/v1`。如需自定义，可配置环境变量：

```powershell
$env:GEMMA_BASE_URL = "http://127.0.0.1:8080/v1"
$env:GEMMA_MODEL = "local-model"
```

系统亦支持在 `.agent/providers.json` 中配置 `main` / `subagent` / `compactor` 三级分角色路由。

### 3. 启动运行

#### 方式 A：启动双端协同 Web 控制台（推荐）

```bash
py -3 -m core.api
```

启动后在浏览器打开：`http://127.0.0.1:8765/web/index.html`，即可进入包含 Git 式上下文分支树、实时流式解码与文件排队面板的原生 Web 双控台。

#### 方式 B：启动 CLI 交互模式

```bash
py -3 main.py
```

*支持 `--resume` 列出与恢复历史任务，支持 `--goal` 长期目标模式。*

---

## 📁 架构全景

```
Qwen38-llama-cpp-harness-windows/
├── main.py                     # 薄前端 1：CLI 交互入口
├── core/                       # 核心业务模块（单例内核）
│   ├── api.py                  # 薄前端 2：HTTP REST & SSE 网关服务
│   ├── agent.py                # ReAct 循环引擎与技能分发
│   ├── persistence.py          # Git 式上下文 DAG 存储与 NTFS 安全写入
│   ├── queue_manager.py        # 显式文件基双端同步队列 (.agent/queue/)
│   ├── interrupt.py            # 三道实时打断门禁 (Gate 1-3) 与崩溃自愈
│   ├── subagent_runner.py      # 单实例隔离 SubAgent 沙箱运行器
│   ├── context.py              # 会话上下文生命周期与分支重组
│   ├── config.py               # 三级角色路由、模型窗口探测与费用审计
│   ├── tools.py                # 内置安全命令执行、文件读写与 CDP 通信
│   ├── validator.py            # 参数 Schema 校验与死循环防护
│   └── logger.py               # 控制台与文件日志输出
├── skills/                     # 可扩展原子技能目录（万物皆 Skill）
│   ├── shell.py                # PowerShell 7 安全执行
│   ├── chrome_dom.py           # Chrome DevTools Protocol (CDP) 交互
│   ├── screenshot.py           # 屏幕捕获与无损降级
│   ├── subagent.py             # 派生隔离子智能体沙箱
│   └── ...                     # read_file, write_file, mouse, keyboard 等
├── web/                        # 零构建原生 ES Modules Web 控制台
│   ├── index.html              # 控制台页面结构
│   ├── styles.css              # 原生自适应样式
│   ├── app.js                  # 核心控制器与 SSE 事件总线
│   ├── tree_view.js            # DAG 分支树可视化投影
│   └── queue_panel.js          # 队列操作面板
├── tests/                      # 14 个自动化测试套件（107 项测试）
├── .agent/                     # 运行时状态（仅 AGENTS.md 纳入版本控制）
│   └── AGENTS.md               # 核心系统提示词与行为规则
├── SPEC.md                     # 项目持久化规格说明（权威契约）
└── requirements.txt            # 运行时依赖清单
```

---

## 🧪 测试与质量门禁

运行全量测试套件（零外部服务依赖，测试全套件秒级完成）：

```bash
py -3 -m unittest discover -s tests -t . -v
```

或使用 pytest：

```bash
pytest
```

---

## 📄 许可证

本项目基于 [MIT License](LICENSE) 开源。