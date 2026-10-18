/**
 * Main Web Console Application (app.js)
 * Connects REST endpoints and SSE stream (/events) to project .agent/ file state.
 */
import { renderDagTree, renderBranchBar, contentToText } from "./tree_view.js";
import { renderQueuePanel } from "./queue_panel.js";

const STREAM_CAP = 200000;
const DELTA_EVENTS = ["reasoning_delta", "content_delta", "tool_call_delta", "tool_start", "tool_rejected", "tool_interrupted"];
const SYNC_EVENTS = ["step_phase", "queue_updated", "queue_consumed", "branch_created", "branch_checked_out", "interrupt_set", "providers_updated", "session_changed", "tag_created"];

const state = {
  currentSession: null,
  currentBranch: "main",
  busy: false,
  tasks: [],
  tree: null,
  queue: [],
  sse: null,
  lastSeq: 0,
  streamChars: 0,
  refreshTicket: 0,
  providersText: "",
  noticeToken: 0,
};

async function apiCall(method, path, body = null) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (body !== null) {
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  return { status: resp.status, data };
}

function el(id) {
  return document.getElementById(id);
}

function setNotice(text, kind = "") {
  const notice = el("composer-notice");
  if (!notice) return;
  state.noticeToken += 1;
  const token = state.noticeToken;
  if (!text) {
    notice.textContent = "";
    notice.className = "composer-notice";
    return;
  }
  const time = new Date().toLocaleTimeString();
  notice.textContent = `[${time}] ${text}`;
  notice.className = `composer-notice ${kind}`;
  window.setTimeout(() => {
    if (state.noticeToken === token) {
      notice.textContent = "";
      notice.className = "composer-notice";
    }
  }, 9000);
}

function errorText(data, fallback) {
  return (data && data.error) || fallback;
}

function updateCharLabel() {
  const label = el("stream-char-label");
  if (!label) return;
  label.textContent = `chars: ${state.streamChars} ≈ tokens: ${Math.ceil(state.streamChars / 4)}`;
}

function resetStream() {
  state.lastSeq = 0;
  state.streamChars = 0;
  const box = el("live-stream-output");
  if (box) box.textContent = "";
  const seqLabel = el("stream-seq-label");
  if (seqLabel) seqLabel.textContent = "seq: 0";
  const stepLabel = el("stream-step-label");
  if (stepLabel) stepLabel.textContent = "实时解码流 (Idle)";
  updateCharLabel();
}

function renderEmptyProjection(treeHint, messageHint) {
  const treeContainer = el("dag-tree-container");
  if (treeContainer) {
    treeContainer.innerHTML = "";
    const hint = document.createElement("div");
    hint.className = "empty-hint";
    hint.textContent = treeHint;
    treeContainer.appendChild(hint);
  }
  const branchList = el("branch-list");
  if (branchList) branchList.innerHTML = "";
  const messages = el("messages-container");
  if (messages) {
    messages.innerHTML = "";
    const hint = document.createElement("div");
    hint.className = "empty-hint";
    hint.textContent = messageHint;
    messages.appendChild(hint);
  }
}

function renderActiveMessages(treeData) {
  const container = el("messages-container");
  if (!container) return;
  container.innerHTML = "";

  if (!treeData || !treeData.nodes || !treeData.nodes.length) {
    container.innerHTML = '<div class="empty-hint">当前分支暂无对话历史</div>';
    return;
  }

  const nodes = treeData.nodes;
  const branches = treeData.branches || {};
  const branch = treeData.current_branch || "main";
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));

  const chain = [];
  let currId = branches[branch] || nodes[nodes.length - 1].id;
  const visited = new Set();
  while (currId && nodeMap.has(currId) && !visited.has(currId)) {
    visited.add(currId);
    const n = nodeMap.get(currId);
    chain.push(n);
    currId = n.parent_id;
  }
  chain.reverse();

  for (const node of chain) {
    if (node.role === "system" && node.type !== "compaction") continue;
    const card = document.createElement("div");
    card.className = `msg-card role-${node.role || "system"}`;

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    const rejected = node.meta?.rejected_by_operator ? ' <span class="rejected-tag">REJECTED</span>' : "";
    const left = document.createElement("span");
    left.innerHTML = `<strong>${node.role}</strong> #${node.id}${rejected}`;
    const right = document.createElement("span");
    const model = node.meta?.model ? ` · ${node.meta.model}` : "";
    right.textContent = `${node.timestamp || ""}${model}`;
    meta.appendChild(left);
    meta.appendChild(right);
    card.appendChild(meta);

    const body = document.createElement("div");
    body.className = "msg-body";
    body.textContent = contentToText(node.content) || "(空内容)";
    card.appendChild(body);

    if (node.reasoning_content) {
      const reasoning = document.createElement("div");
      reasoning.className = "msg-reasoning";
      reasoning.textContent = `思考链: ${node.reasoning_content}`;
      card.appendChild(reasoning);
    }

    if (node.tool_calls && node.tool_calls.length) {
      const calls = document.createElement("div");
      calls.className = "msg-tool-calls";
      calls.textContent = node.tool_calls
        .map((tc) => `-> ${tc.function?.name || tc.type || "tool"}(${tc.function?.arguments || ""})`)
        .join("\n");
      card.appendChild(calls);
    }

    container.appendChild(card);
  }
  container.scrollTop = container.scrollHeight;
}

function rebuildTaskSelect() {
  const select = el("task-select");
  if (!select) return;
  select.innerHTML = "";
  const seen = new Set();
  for (const t of state.tasks) {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = `${t.id} [${t.status}] (${t.message_count})`;
    select.appendChild(opt);
    seen.add(t.id);
  }
  if (state.currentSession && !seen.has(state.currentSession)) {
    const opt = document.createElement("option");
    opt.value = state.currentSession;
    opt.textContent = `${state.currentSession} [无 checkpoint]`;
    select.appendChild(opt);
  }
  if (state.currentSession) select.value = state.currentSession;
}

async function refreshAll() {
  const ticket = ++state.refreshTicket;
  let health, tasksData, queueData, subData, provData, usageData;
  try {
    [{ data: health }, { data: tasksData }, { data: queueData }, { data: subData }, { data: provData }, { data: usageData }] =
      await Promise.all([
        apiCall("GET", "/health"),
        apiCall("GET", "/tasks"),
        apiCall("GET", "/queue"),
        apiCall("GET", "/subagents"),
        apiCall("GET", "/config/providers"),
        apiCall("GET", "/usage"),
      ]);
  } catch (err) {
    if (ticket === state.refreshTicket) setNotice(`刷新失败: ${err.message}`, "error");
    return;
  }
  if (ticket !== state.refreshTicket) return;

  const sessionNow = health.active_task || health.current_session || null;
  if (sessionNow !== state.currentSession) {
    state.currentSession = sessionNow;
    state.tree = null;
    resetStream();
  }
  state.busy = Boolean(health.busy);
  state.currentBranch = health.current_branch || "main";

  const dot = el("daemon-dot");
  if (dot) dot.className = `status-dot ${state.busy ? "busy" : ""}`;
  el("session-badge").textContent = `session: ${state.currentSession || "none"}`;
  el("branch-badge").textContent = `branch: ${state.currentBranch}`;
  el("model-info-badge").textContent = `model: ${health.model} (n_ctx=${health.n_ctx})`;

  if (Array.isArray(tasksData.tasks)) {
    state.tasks = tasksData.tasks;
    rebuildTaskSelect();
  } else {
    setNotice(`GET /tasks 暂时失败（状态文件正在写入），稍后自动重试`, "error");
  }

  const currentMeta = state.tasks.find((t) => t.id === state.currentSession);
  if (state.currentSession && currentMeta) {
    const { status, data: tree } = await apiCall("GET", `/tasks/${state.currentSession}/tree`);
    if (ticket !== state.refreshTicket) return;
    if (status === 200) {
      state.tree = tree;
      const phase = tree.step_state?.phase || (state.busy ? "running" : "idle");
      const rejected = tree.step_state?.rejected ? " (REJECTED)" : "";
      el("phase-badge").textContent = `phase: ${phase}${rejected}`;
      renderBranchBar(el("branch-list"), tree, {
        onCheckoutBranch: async (branchName) => {
          const { status: code, data } = await apiCall("POST", `/tasks/${state.currentSession}/checkout`, { branch: branchName });
          if (code !== 200) {
            setNotice(`Checkout 失败: ${errorText(data, code)}`, "error");
          } else {
            setNotice(`已切换到分支 ${branchName} (head=${data.head_node_id || "-"})`, "ok");
          }
          await refreshAll();
        },
      });
      renderDagTree(el("dag-tree-container"), tree, {
        onSelectNode: (node) => {
          const forkInput = el("fork-node-id");
          if (forkInput) forkInput.value = node.id;
        },
      });
      renderActiveMessages(tree);
    } else {
      state.tree = null;
      el("phase-badge").textContent = `phase: ${state.busy ? "running" : "idle"}`;
      renderEmptyProjection(`树接口返回 ${status}: ${errorText(data, "无法读取 checkpoint")}`, "暂无可回放的对话节点");
    }
  } else if (state.currentSession) {
    state.tree = null;
    el("phase-badge").textContent = `phase: ${state.busy ? "running" : "idle"}`;
    renderEmptyProjection(
      `会话 ${state.currentSession} 暂无 checkpoint.jsonl（尚未产生可回放节点）`,
      "当前会话暂无对话历史（无 checkpoint 文件）"
    );
  } else {
    state.tree = null;
    el("phase-badge").textContent = "phase: idle";
    renderEmptyProjection("暂无会话，提交任务后实时映射 checkpoint.jsonl", "暂无对话历史");
  }

  // Render Unified Queue
  state.queue = queueData.queue || [];
  renderQueuePanel(
    el("queue-list"),
    el("queue-count-badge"),
    state.queue,
    {
      onModify: async (id, patch) => {
        const { status, data } = await apiCall("PATCH", `/queue/${id}`, patch);
        if (status !== 200) {
          setNotice(`队列更新失败: ${errorText(data, status)}`, "error");
        } else {
          setNotice(`已更新队列项 #${id}: ${Object.keys(patch).join(", ")}`, "ok");
        }
        await refreshAll();
      },
      onCancel: async (id) => {
        const { status, data } = await apiCall("DELETE", `/queue/${id}`);
        if (status !== 200) {
          setNotice(`队列取消失败: ${errorText(data, status)}`, "error");
        } else {
          setNotice(`已取消队列项 #${id}`, "ok");
        }
        await refreshAll();
      },
    }
  );

  // Render SubAgents
  const badge = el("subagent-lock-badge");
  if (badge) {
    const active = subData.active_subagent;
    badge.textContent = active ? `active: ${active}` : "idle";
  }
  const subList = el("subagent-list");
  if (subList) {
    subList.innerHTML = "";
    const subagents = subData.subagents || [];
    if (!subagents.length) {
      subList.innerHTML = '<div class="empty-hint">暂无 SubAgent 沙箱记录</div>';
    }
    for (const sub of subagents) {
      const item = document.createElement("div");
      item.className = "queue-card";
      const head = document.createElement("div");
      head.className = "dag-node-header";
      head.innerHTML = `<span>${sub.id}</span><span>status: <strong>${sub.status}</strong> (${sub.message_count} msgs)</span>`;
      const objective = document.createElement("div");
      objective.className = "dag-node-snippet";
      objective.textContent = sub.objective || "";
      const audit = document.createElement("div");
      audit.className = "mono-meta";
      audit.textContent = sub.audit_path || "";
      item.appendChild(head);
      item.appendChild(objective);
      item.appendChild(audit);
      subList.appendChild(item);
    }
  }

  // Render Providers & Usage
  const costBadge = el("usage-cost-badge");
  if (costBadge && usageData) {
    costBadge.textContent = `$${Number(usageData.total_cost_usd || 0).toFixed(4)} (${usageData.total_calls || 0} calls)`;
  }
  const provSummary = el("providers-summary");
  if (provSummary && provData.resolved_roles) {
    provSummary.innerHTML = Object.entries(provData.resolved_roles)
      .map(([role, cfg]) => `<div class="mono-meta"><strong>${role}</strong>: ${cfg.model} @ ${cfg.base_url} (n_ctx=${cfg.n_ctx})</div>`)
      .join("");
  }
  const provEditor = el("providers-json-input");
  if (provEditor && provData.providers) {
    state.providersText = JSON.stringify(provData.providers, null, 2);
    if (!provEditor.dataset.dirty && provEditor.value !== state.providersText) {
      provEditor.value = state.providersText;
    }
  }
}

function handleDelta(evt) {
  let payload;
  try {
    payload = JSON.parse(evt.data);
  } catch (_) {
    return;
  }
  const taskId = payload.task_id;
  if (taskId && state.currentSession && taskId !== state.currentSession) return;
  if (payload.seq) {
    if (payload.seq <= state.lastSeq) return;
    state.lastSeq = payload.seq;
    const seqLabel = el("stream-seq-label");
    if (seqLabel) seqLabel.textContent = `seq: ${payload.seq}`;
  }

  const type = payload.event_type || evt.type;
  const stepLabel = el("stream-step-label");
  if (stepLabel) {
    stepLabel.textContent = `实时解码流 (${type})`;
  }

  const box = el("live-stream-output");
  if (!box) return;
  let appended = "";
  if ((type === "content_delta" || type === "reasoning_delta") && typeof payload.delta === "string") {
    appended = payload.delta;
  } else if (type === "tool_start") {
    const d = payload.delta || {};
    appended = `\n⚙ tool_start: ${d.tool || d.name || "tool"}(${JSON.stringify(d.args ?? {})})\n`;
  } else if (type === "tool_rejected") {
    const d = payload.delta || {};
    appended = `\n⛔ tool_rejected: ${d.tool || d.name || "tool"} — ${d.reason || d.source || ""}\n`;
  } else if (type === "tool_interrupted") {
    const d = payload.delta || {};
    appended = `\n⛔ tool_interrupted: ${d.tool || d.name || "tool"} — ${d.reason || ""}\n`;
  }
  if (!appended) return;

  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
  box.textContent += appended;
  if (box.textContent.length > STREAM_CAP) {
    box.textContent = "⟨较早输出已截断⟩\n" + box.textContent.slice(-(STREAM_CAP - 40000));
  }
  state.streamChars = box.textContent.length;
  updateCharLabel();
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

function connectSse() {
  if (state.sse) {
    const old = state.sse;
    old.onerror = null;
    old.close();
    state.sse = null;
  }
  const es = new EventSource(`/events?since_seq=${state.lastSeq}`);
  state.sse = es;

  es.onerror = () => {
    if (state.sse !== es) return;
    es.close();
    state.sse = null;
    setNotice("SSE 连接断开，2 秒后自动重连…", "");
    window.setTimeout(() => {
      if (!state.sse) connectSse();
    }, 2000);
  };

  for (const evtType of DELTA_EVENTS) {
    es.addEventListener(evtType, handleDelta);
  }
  for (const syncType of SYNC_EVENTS) {
    es.addEventListener(syncType, (evt) => {
      handleDelta(evt);
      refreshAll();
    });
  }
}

function bindEvents() {
  el("btn-refresh")?.addEventListener("click", () => {
    setNotice("正在重新拉取磁盘投影…");
    refreshAll();
  });

  el("task-select")?.addEventListener("change", async (e) => {
    const taskId = e.target.value;
    if (!taskId) return;
    const { status, data } = await apiCall("POST", "/session", { task_id: taskId });
    if (status !== 200) {
      setNotice(`切换会话失败: ${errorText(data, status)}`, "error");
    } else {
      setNotice(`当前会话已切换为 ${data.current_session || taskId}`, "ok");
    }
    await refreshAll();
  });

  el("btn-resume-task")?.addEventListener("click", async () => {
    const btn = el("btn-resume-task");
    if (!state.currentSession || btn?.disabled) return;
    if (btn) btn.disabled = true;
    try {
      const { status, data } = await apiCall("POST", `/tasks/${state.currentSession}/resume`, { branch: state.currentBranch });
      if (status === 202) {
        setNotice(`已续跑任务 ${data.task_id || state.currentSession} (branch=${state.currentBranch})`, "ok");
      } else {
        setNotice(`续跑被拒绝: ${errorText(data, status)}`, "error");
      }
    } finally {
      if (btn) btn.disabled = false;
    }
    await refreshAll();
  });

  el("btn-create-branch")?.addEventListener("click", async () => {
    if (!state.currentSession) {
      setNotice("没有活动会话，无法创建分支", "error");
      return;
    }
    const nameInput = el("new-branch-name");
    const nodeInput = el("fork-node-id");
    const name = nameInput?.value.trim();
    if (!name) {
      setNotice("请输入新分支名", "error");
      return;
    }
    const { status, data } = await apiCall("POST", `/tasks/${state.currentSession}/branches`, {
      name,
      from_node_id: nodeInput?.value.trim() || null,
      checkout: true,
    });
    if (status !== 201 && status !== 200) {
      setNotice(`创建分支失败: ${errorText(data, status)}`, "error");
    } else {
      setNotice(`已创建并切换分支 ${data.name || name} @ ${data.head_node_id || "-"}`, "ok");
      if (nameInput) nameInput.value = "";
      if (nodeInput) nodeInput.value = "";
    }
    await refreshAll();
  });

  el("btn-interrupt")?.addEventListener("click", async () => {
    const btn = el("btn-interrupt");
    if (btn?.disabled) return;
    const mode = el("abort-mode-select")?.value || "reject_execution";
    if (btn) btn.disabled = true;
    try {
      const { status, data } = await apiCall("POST", "/interrupt", {
        task_id: state.currentSession,
        source: "web",
        reason: "Operator clicked Interrupt in Web Console",
        mode,
        halt_loop: false,
      });
      if (status === 200) {
        setNotice(`已写入 abort.flag (mode=${mode}, task=${data.task_id || state.currentSession})`, "ok");
      } else {
        setNotice(`打断失败: ${errorText(data, status)}`, "error");
      }
    } finally {
      if (btn) btn.disabled = false;
    }
    await refreshAll();
  });

  el("btn-enqueue")?.addEventListener("click", async () => {
    const input = el("task-input");
    const text = input?.value.trim();
    if (!text) {
      setNotice("请先输入指令内容", "error");
      return;
    }
    const priority = Number(el("queue-priority")?.value || 50);
    const statusValue = el("queue-initial-status")?.value || "queued";
    const { status, data } = await apiCall("POST", "/queue", {
      content: text,
      priority,
      status: statusValue,
      source: "web",
      author: "web-operator",
      target_branch: state.currentBranch || "main",
    });
    if (status !== 201 && status !== 200) {
      setNotice(`入队失败: ${errorText(data, status)}`, "error");
    } else {
      const item = data.item || {};
      setNotice(`已入队 #${item.id || "?"} (prio=${item.priority ?? priority}, status=${item.status || statusValue})`, "ok");
      if (input) input.value = "";
    }
    await refreshAll();
  });

  el("task-composer")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = el("task-input");
    const text = input?.value.trim();
    if (!text) {
      setNotice("请先输入指令内容", "error");
      return;
    }
    const { status, data } = await apiCall("POST", "/tasks", {
      task: text,
      enqueue_if_busy: true,
      source: "web",
    });
    if (status === 202 && data.queued) {
      setNotice(`主循环忙碌，已自动入队 #${data.item?.id || "?"}（等待消费）`, "ok");
      if (input) input.value = "";
    } else if (status === 202 && data.task_id) {
      state.currentSession = data.task_id;
      resetStream();
      setNotice(`任务已启动: ${data.task_id}`, "ok");
      if (input) input.value = "";
    } else {
      setNotice(`提交失败: ${errorText(data, status)}`, "error");
    }
    await refreshAll();
  });

  el("btn-spawn-subagent")?.addEventListener("click", async () => {
    const input = el("subagent-objective");
    const btn = el("btn-spawn-subagent");
    const objective = input?.value.trim();
    if (!objective) {
      setNotice("请输入 SubAgent 目标", "error");
      return;
    }
    if (btn?.disabled) return;
    if (btn) btn.disabled = true;
    try {
      const { status, data } = await apiCall("POST", "/subagents", { objective });
      if (status !== 200) {
        setNotice(`SubAgent 派生失败: ${errorText(data, status)}`, "error");
      } else {
        setNotice(`SubAgent 已派发: ${data.subagent_id || data.id || "ok"}`, "ok");
        if (input) input.value = "";
      }
    } finally {
      if (btn) btn.disabled = false;
    }
    await refreshAll();
  });

  el("btn-save-providers")?.addEventListener("click", async () => {
    const editor = el("providers-json-input");
    if (!editor) return;
    let parsed;
    try {
      parsed = JSON.parse(editor.value);
    } catch (err) {
      setNotice(`JSON 格式错误: ${err.message}`, "error");
      return;
    }
    const { status, data } = await apiCall("PUT", "/config/providers", parsed);
    if (status !== 200) {
      setNotice(`保存失败: ${errorText(data, status)}`, "error");
    } else {
      editor.dataset.dirty = "";
      state.providersText = JSON.stringify(data.providers, null, 2);
      setNotice("providers 路由已保存并热重载", "ok");
    }
    await refreshAll();
  });

  el("providers-json-input")?.addEventListener("input", (e) => {
    e.target.dataset.dirty = "1";
  });

  el("btn-clear-stream")?.addEventListener("click", () => {
    resetStream();
    setNotice("已清空实时解码流缓冲");
  });

  el("subagent-objective")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      el("btn-spawn-subagent")?.click();
    }
  });
}

window.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  await refreshAll();
  connectSse();
});
