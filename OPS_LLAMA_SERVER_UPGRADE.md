# OPS_LLAMA_SERVER_UPGRADE.md: 远程模型宿主生产级配置升级与运维手册

- **阶段目标**：升级 Linux 模型宿主（Tesla M40 24GB）上的 `llama-server.service` 参数，激活 MTP 安全档位、128k 统一 KV 池保护、DeltaNet 状态稠密检查点与前缀移位复用
- **执行角色**：基础设施与运维工程师 / 架构负责人 (Infra & DevOps Engineer)
- **文档状态**：**【冷备待命 (Pending)】**
- **严正红线（不可违背）**：
  > **当前服务器 Slot 1 正在执行其他长时推理任务（`is_processing: true`），严禁在此时重启服务！**
  > **必须等待服务器当前任务自然完成、显存与算力完全释放后，方可由运维人员按本手册执行升级！**

---

## 一、升级背景与技术参数对比

经结合底层内核分析与实际硬件实测决断，现行的 `llama-server.service` 参数升级策略明确如下：

| 参数项 | 变更前 | **生产升级设定值** | 架构决断与底层物理依据 |
| :--- | :--- | :--- | :--- |
| **`--spec-draft-n-max`** | `4` | **`4` *(维持原值)*** | **绝对维持 4**。Tesla M40 显存带宽受限（288 GB/s），计算与访存延迟显著；实测草稿长度为 4 时有效接受命中率最高，能最大化摊薄权重搬运开销。 |
| **`--cache-reuse`** | *(默认 0 禁用)* | **`0` *(严禁开启)*** | **绝对禁止开启**。当前服务已挂载 `--mmproj` 多模态视觉模块。`llama.cpp` 中视觉 Patch 嵌入与 Token 级前缀缓存复用机制存在结构性冲突，开启极易引发视觉向量错位与内核断言崩溃，专家此处为严重误判，必须坚决禁用。 |
| **`--no-cache-idle-slots`** | *(默认未配置，即 true)* | **显式声明启用** | 阻断当 Slot 1 启动短周期子任务或压缩器时，触发 `[TAG_IDLE_SLOT_CLEAR]` 逻辑将处于暂态闲置的 Slot 0 主分支 KV 缓存清空。 |
| **`--checkpoint-min-step` (`-cms`)** | `8192` *(编译期默认)* | **`1024`** | 将 Qwen3.8 混合架构（48 层 DeltaNet 循环状态）检查点的最小留存间距从 8,192 降至 1,024 Token，确保每 1~2 轮 ReAct 交互均有可回滚快照。 |
| **`--ctx-checkpoints` (`-ctxcp`)** | `32` *(编译期默认)* | **`64`** | 将每个槽位可保存的状态快照数翻倍，支持多达 64 层的深分支回滚。 |
| **`--cache-ram` (`-cram`)** | `8192` *(8 GiB)* | **`16384` *(16 GiB)*** | 分配 16 GiB 宿主机系统内存池用于容纳 64 个混合检查点，完全不消耗 Tesla M40 宝贵的 24GB 显存。 |

---

## 二、目标服务配置文件完整范本

升级后的 `/etc/systemd/system/llama-server.service` 标准文件内容如下：

```ini
[Unit]
Description=Llama.cpp Server (Hardened for Qwen3.8 Hybrid DAG Agent)
After=network.target nvidia-persistenced.service

[Service]
User=llama
Group=llama
SupplementaryGroups=video render
WorkingDirectory=/home/zt/JonathanColetti.Qwen3.8-27B-Uncensored-GGUF

ExecStart=/home/zt/llama.cpp-0.6.0/build/bin/llama-server \
  --model Qwen3.8-27B-Uncensored-IQ4_XS.gguf \
  --mmproj mmproj-Qwen3.8-27B-Uncensored-F16.gguf \
  --spec-type draft-mtp \
  --spec-draft-n-max 4 \
  --spec-draft-n-min 1 \
  -ngl 99 \
  -c 128000 \
  -np 2 \
  -ctk q8_0 \
  -ctv q8_0 \
  --kv-unified \
  --no-cache-idle-slots \
  --cache-ram 16384 \
  --ctx-checkpoints 64 \
  --checkpoint-min-step 1024 \
  --jinja \
  --chat-template-file chat_template.jinja \
  --metrics \
  --load-mode none \
  --host 0.0.0.0 \
  --port 8080

ExecStartPre=+/usr/sbin/sysctl vm.swappiness=10
LimitMEMLOCK=infinity
Restart=on-failure
RestartSec=5
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
```

---

## 三、标准运维操作流程 (SOP)

*(请严格在确认当前长时任务结束后执行以下指令)*

### 1. 登录与就绪检查
通过 SSH 登录模型宿主，核验当前已无活跃任务正在推理：
```bash
# 检查当前 GPU 利用率与 Slot 状态
curl -s http://127.0.0.1:8080/slots | jq .
nvidia-smi
```
确认两个槽位的 `is_processing` 均为 `false`。

### 2. 更新服务单元配置
```bash
# 备份旧配置
sudo cp /etc/systemd/system/llama-server.service /etc/systemd/system/llama-server.service.bak.$(date +%Y%m%d)

# 写入上述第二节的新版配置
sudo nano /etc/systemd/system/llama-server.service
```

### 3. 重载并重启服务
```bash
sudo systemctl daemon-reload
sudo systemctl restart llama-server.service
sudo systemctl status llama-server.service
```

### 4. 生产验证与端点冒烟
```bash
# 检查模型是否成功加载
curl -s http://127.0.0.1:8080/v1/models | jq .

# 检查 props 参数是否包含新版特性
curl -s http://127.0.0.1:8080/props | jq '{model: .model_path, slots: .total_slots, caps: .chat_template_caps}'
```
确认服务正常监听在 `8080` 端口，显存占用稳定在 ~23.4 GB 且无启动告警。

---
*本文档由 Qwen38-llama-cpp-harness-windows 项目架构组编制，冷备于版本库以备随时运维交付。*
