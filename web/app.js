/**
 * Main Web Console Application (app.js)
 * Connects REST endpoints and SSE stream (/events) to project .agent/ file state.
 */
import { renderDagTree, renderBranchBar } from "./tree_view.js";
import { renderQueuePanel } from "./queue_panel.js";

const state = {
  currentSession: null,
  currentBranch: "main",
  busy: false,
  tree: null,
  queue: [],
  subagents: [],
  activeSubagent: null,
  sse: null,
  lastSeq: 0,
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

function renderActiveMessages(treeData) {
  const container = document.getElementById("messages-container");
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
    const rejectedTag = node.meta?.rejected_by_operator ? " [REJECTED]" : "";
    meta.innerHTML = `<span><strong>${node.role}</strong> #${node.id}${rejectedTag}</span><span>${node.timestamp || ""}</span>`;
    card.appendChild(meta);

    const body = document.createElement("div");
    body.className = "msg-body";
    let text = typeof node.content === "string" ? node.content : JSON.stringify(node.content);
    if (node.tool_calls && node.tool_calls.length) {
      text += "\n" + node.tool_calls.map((tc) => `-> ${tc.function?.name}(${tc.function?.arguments})`).join("\n");
    }
    body.textContent = text;
    card.appendChild(body);
    container.appendChild(card);
  }
  container.scrollTop = container.scrollHeight;
}

function renderSubagents(subagents, activeSubagent) {
  const badge = document.getElementById("subagent-lock-badge");
  if (badge) {
    badge.textContent = activeSubagent ? `active: ${activeSubagent}` : "idle";
  }
  const list = document.getElementById("subagent-list");
  if (!list) return;
  list.innerHTML = "";
  if (!subagents || !subagents.length) {
    list.innerHTML = '<div class="empty-hint">暂无 SubAgent 沙箱记录</div>';
    return;
  }
  for (const sub of subagents) {
    const item = document.createElement("div");
    item.className = "queue-card";
    item.innerHTML = `
      <div class="dag-node-header">
        <span>${sub.id}</span>
        <span>status: <strong>${sub.status}</strong> (${sub.message_count} msgs)</span>
      </div>
      <div class="dag-node-snippet">${sub.objective || ""}</div>
      <div class="mono-meta">${sub.audit_path}</div>
    `;
    list.appendChild(item);
  }
}

async function refreshAll() {
  const [{ data: health }, { data: tasksData }, { data: queueData }, { data: subData }, { data: provData }, { data: usageData }] =
    await Promise.all([
      apiCall("GET", "/health"),
      apiCall("GET", "/tasks"),
      apiCall("GET", "/queue"),
      apiCall("GET", "/subagents"),
      apiCall("GET", "/config/providers"),
      apiCall("GET", "/usage"),
    ]);

  state.busy = Boolean(health.busy);
  state.currentSession = health.active_task || health.current_session;
  state.currentBranch = health.current_branch || "main";

  const dot = document.getElementById("daemon-dot");
  if (dot) dot.className = `status-dot ${state.busy ? "busy" : ""}`;
  document.getElementById("session-badge").textContent = `session: ${state.currentSession || "none"}`;
  document.getElementById("branch-badge").textContent = `branch: ${state.currentBranch}`;
  document.getElementById("model-info-badge").textContent = `model: ${health.model} (n_ctx=${health.n_ctx})`;

  // Populate task selector
  const select = document.getElementById("task-select");
  const tasks = tasksData.tasks || [];
  if (select) {
    select.innerHTML = "";
    for (const t of tasks) {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = `${t.id} [${t.status}] (${t.message_count})`;
      if (t.id === state.currentSession) opt.selected = true;
      select.appendChild(opt);
    }
    if (!state.currentSession && tasks.length) {
      state.currentSession = tasks[0].id;
    }
  }

  // Load DAG tree for current session
  if (state.currentSession) {
    const { status, data: tree } = await apiCall("GET", `/tasks/${state.currentSession}/tree`);
    if (status === 200) {
      state.tree = tree;
      const phase = tree.step_state?.phase || (state.busy ? "running" : "idle");
      document.getElementById("phase-badge").textContent = `phase: ${phase}${tree.step_state?.rejected ? " (REJECTED)" : ""}`;
      renderBranchBar(document.getElementById("branch-list"), tree, {
        onCheckoutBranch: async (branchName) => {
          await apiCall("POST", `/tasks/${state.currentSession}/checkout`, { branch: branchName });
          await refreshAll();
        },
      });
      renderDagTree(document.getElementById("dag-tree-container"), tree, {
        onSelectNode: (node) => {
          const forkInput = document.getElementById("fork-node-id");
          if (forkInput) forkInput.value = node.id;
        },
      });
      renderActiveMessages(tree);
    }
  }

  // Render Unified Queue
  state.queue = queueData.queue || [];
  renderQueuePanel(
    document.getElementById("queue-list"),
    document.getElementById("queue-count-badge"),
    state.queue,
    {
      onModify: async (id, patch) => {
        await apiCall("PATCH", `/queue/${id}`, patch);
        await refreshAll();
      },
      onCancel: async (id) => {
        await apiCall("DELETE", `/queue/${id}`);
        await refreshAll();
      },
    }
  );

  // Render SubAgents
  renderSubagents(subData.subagents || [], subData.active_subagent);

  // Render Providers & Usage
  const costBadge = document.getElementById("usage-cost-badge");
  if (costBadge && usageData) {
    costBadge.textContent = `$${Number(usageData.total_cost_usd || 0).toFixed(4)} (${usageData.total_calls || 0} calls)`;
  }
  const provSummary = document.getElementById("providers-summary");
  if (provSummary && provData.resolved_roles) {
    provSummary.innerHTML = Object.entries(provData.resolved_roles)
      .map(([role, cfg]) => `<div class="mono-meta"><strong>${role}</strong>: ${cfg.model} @ ${cfg.base_url} (n_ctx=${cfg.n_ctx})</div>`)
      .join("");
  }
  const provEditor = document.getElementById("providers-json-input");
  if (provEditor && !provEditor.dataset.loaded && provData.providers) {
    provEditor.value = JSON.stringify(provData.providers, null, 2);
    provEditor.dataset.loaded = "1";
  }
}

function connectSse() {
  if (state.sse) {
    state.sse.close();
  }
  const url = `/events?since_seq=${state.lastSeq}`;
  const es = new EventSource(url);
  state.sse = es;

  const streamBox = document.getElementById("live-stream-output");
  const seqLabel = document.getElementById("stream-seq-label");
  const stepLabel = document.getElementById("stream-step-label");

  const handleDelta = (evt) => {
    try {
      const payload = JSON.parse(evt.data);
      if (payload.seq) {
        state.lastSeq = Math.max(state.lastSeq, payload.seq);
        if (seqLabel) seqLabel.textContent = `seq: ${state.lastSeq}`;
      }
      if (payload.step_id && stepLabel) {
        stepLabel.textContent = `步骤: ${payload.step_id} (${payload.event_type})`;
      }
      if (streamBox) {
        if (typeof payload.delta === "string") {
          streamBox.textContent += payload.delta;
        } else {
          streamBox.textContent += `\n[${payload.event_type}] ${JSON.stringify(payload.delta)}`;
        }
        streamBox.scrollTop = streamBox.scrollHeight;
      }
    } catch (_) {}
  };

  for (const evtType of ["reasoning_delta", "content_delta", "tool_call_delta", "tool_start", "tool_rejected", "tool_interrupted"]) {
    es.addEventListener(evtType, handleDelta);
  }

  for (const syncType of ["step_phase", "queue_updated", "queue_consumed", "branch_created", "branch_checked_out", "interrupt_set", "providers_updated"]) {
    es.addEventListener(syncType, (evt) => {
      handleDelta(evt);
      refreshAll();
    });
  }
}

function bindEvents() {
  document.getElementById("btn-refresh")?.addEventListener("click", () => refreshAll());

  document.getElementById("task-select")?.addEventListener("change", async (e) => {
    const taskId = e.target.value;
    if (taskId) {
      await apiCall("POST", "/session", { task_id: taskId });
      await refreshAll();
    }
  });

  document.getElementById("btn-resume-task")?.addEventListener("click", async () => {
    if (!state.currentSession) return;
    await apiCall("POST", `/tasks/${state.currentSession}/resume`, { branch: state.currentBranch });
    await refreshAll();
  });

  document.getElementById("btn-create-branch")?.addEventListener("click", async () => {
    if (!state.currentSession) return;
    const nameInput = document.getElementById("new-branch-name");
    const nodeInput = document.getElementById("fork-node-id");
    const name = nameInput?.value.trim();
    if (!name) return;
    await apiCall("POST", `/tasks/${state.currentSession}/branches`, {
      name,
      from_node_id: nodeInput?.value.trim() || null,
      checkout: true,
    });
    if (nameInput) nameInput.value = "";
    await refreshAll();
  });

  document.getElementById("btn-interrupt")?.addEventListener("click", async () => {
    const mode = document.getElementById("abort-mode-select")?.value || "reject_execution";
    await apiCall("POST", "/interrupt", {
      task_id: state.currentSession,
      source: "web",
      reason: "Operator clicked Interrupt in Web Console",
      mode,
      halt_loop: false,
    });
    await refreshAll();
  });

  document.getElementById("btn-enqueue")?.addEventListener("click", async () => {
    const input = document.getElementById("task-input");
    const text = input?.value.trim();
    if (!text) return;
    const priority = Number(document.getElementById("queue-priority")?.value || 50);
    const status = document.getElementById("queue-initial-status")?.value || "queued";
    await apiCall("POST", "/queue", {
      content: text,
      priority,
      status,
      source: "web",
      author: "web-operator",
      target_branch: state.currentBranch || "main",
    });
    input.value = "";
    await refreshAll();
  });

  document.getElementById("task-composer")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = document.getElementById("task-input");
    const text = input?.value.trim();
    if (!text) return;
    const { status } = await apiCall("POST", "/tasks", {
      task: text,
      enqueue_if_busy: true,
      source: "web",
    });
    if (status === 202 || status === 200) {
      input.value = "";
    }
    await refreshAll();
  });

  document.getElementById("btn-spawn-subagent")?.addEventListener("click", async () => {
    const input = document.getElementById("subagent-objective");
    const objective = input?.value.trim();
    if (!objective) return;
    input.value = "";
    await apiCall("POST", "/subagents", { objective });
    await refreshAll();
  });

  document.getElementById("btn-save-providers")?.addEventListener("click", async () => {
    const editor = document.getElementById("providers-json-input");
    if (!editor) return;
    try {
      const parsed = JSON.parse(editor.value);
      await apiCall("PUT", "/config/providers", parsed);
      await refreshAll();
    } catch (err) {
      alert("JSON 格式错误: " + err.message);
    }
  });
}

window.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  await refreshAll();
  connectSse();
});
