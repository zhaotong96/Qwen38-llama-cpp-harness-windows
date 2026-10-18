/**
 * Git-Style Context DAG Tree Renderer (tree_view.js)
 * Pure native ES Module — zero build step, 100% projection of .agent/tasks/<id>/ files.
 */

const KIND_BY_ROLE = {
  system: "system",
  user: "user",
  assistant: "assistant",
  tool: "tool",
};

const KIND_LABELS = {
  system: "system",
  user: "user",
  assistant: "assistant",
  reasoning: "reasoning",
  "tool-calls": "tool_calls",
  tool: "tool",
  compaction: "compaction",
  unknown: "unknown",
};

// UI state survives full re-renders driven by SSE refresh cycles.
const selectedIds = new Map(); // containerId -> node id
const expandedIds = new Set();

export function contentToText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (part && typeof part === "object") {
          if (typeof part.text === "string") return part.text;
          if (part.image_url) return `[image ${part.image_url?.url || "attached"}]`;
          return JSON.stringify(part);
        }
        return String(part);
      })
      .join("\n");
  }
  if (content === null || content === undefined) return "";
  return JSON.stringify(content);
}

export function nodeKind(node) {
  if (node.type === "compaction") return "compaction";
  if (node.role === "assistant" && node.tool_calls && node.tool_calls.length) return "tool-calls";
  if (node.role === "assistant" && node.reasoning_content && !contentToText(node.content).trim()) {
    return "reasoning";
  }
  return KIND_BY_ROLE[node.role] || "unknown";
}

function toolCallLines(node) {
  if (!node.tool_calls || !node.tool_calls.length) return [];
  return node.tool_calls.map((tc) => {
    const fn = tc.function || {};
    return `${fn.name || tc.type || "tool"}(${fn.arguments || ""})`;
  });
}

function nodeSnippet(node, kind) {
  const parts = [];
  const text = contentToText(node.content).trim();
  if (text) parts.push(text);
  const reasoning = (node.reasoning_content || "").trim();
  if (reasoning) parts.push(`思考链: ${reasoning}`);
  const calls = toolCallLines(node);
  if (calls.length) parts.push(`工具调用: ${calls.join(" | ")}`);
  if (!parts.length) {
    parts.push(kind === "tool" ? "(工具回执内容为空)" : "(空内容)");
  }
  return parts.join("\n").slice(0, 1800);
}

function buildDetail(node) {
  const detail = document.createElement("div");
  detail.className = "dag-node-detail";

  const text = contentToText(node.content);
  if (text) {
    const cap = document.createElement("div");
    cap.className = "detail-caption";
    cap.textContent = "content";
    const pre = document.createElement("pre");
    pre.textContent = text;
    detail.appendChild(cap);
    detail.appendChild(pre);
  }
  if (node.reasoning_content) {
    const cap = document.createElement("div");
    cap.className = "detail-caption";
    cap.textContent = "reasoning_content";
    const pre = document.createElement("pre");
    pre.textContent = node.reasoning_content;
    detail.appendChild(cap);
    detail.appendChild(pre);
  }
  const calls = toolCallLines(node);
  if (calls.length) {
    const cap = document.createElement("div");
    cap.className = "detail-caption";
    cap.textContent = "tool_calls";
    const pre = document.createElement("pre");
    pre.textContent = calls.join("\n");
    detail.appendChild(cap);
    detail.appendChild(pre);
  }
  const metaKeys = ["model", "tokens", "step_id", "abort_reason", "compacted_nodes", "reasoning_format"];
  const metaText = {};
  for (const key of metaKeys) {
    if (node.meta && node.meta[key] !== undefined) metaText[key] = node.meta[key];
  }
  if (Object.keys(metaText).length) {
    const cap = document.createElement("div");
    cap.className = "detail-caption";
    cap.textContent = "meta";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(metaText, null, 1);
    detail.appendChild(cap);
    detail.appendChild(pre);
  }
  return detail;
}

export function renderDagTree(container, treeData, callbacks = {}) {
  if (!container) return;
  container.innerHTML = "";

  const nodes = (treeData && treeData.nodes) || [];
  const branches = (treeData && treeData.branches) || {};
  const tags = (treeData && treeData.tags) || {};
  const currentBranch = (treeData && treeData.current_branch) || "main";

  if (!nodes.length) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = "当前会话暂无 Commit 节点";
    container.appendChild(empty);
    return;
  }

  // Determine active branch ancestor chain
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));
  const activeChainIds = new Set();
  let currId = branches[currentBranch] || nodes[nodes.length - 1].id;
  while (currId && nodeMap.has(currId) && !activeChainIds.has(currId)) {
    activeChainIds.add(currId);
    currId = nodeMap.get(currId).parent_id;
  }

  // Inverse maps for branch heads and tags
  const headsByNode = {};
  for (const [bName, nId] of Object.entries(branches)) {
    if (!headsByNode[nId]) headsByNode[nId] = [];
    headsByNode[nId].push(bName);
  }
  const tagsByNode = {};
  for (const [tName, nId] of Object.entries(tags)) {
    if (!tagsByNode[nId]) tagsByNode[nId] = [];
    tagsByNode[nId].push(tName);
  }

  const selectedId = selectedIds.get(container.id) || null;

  for (const node of nodes) {
    const kind = nodeKind(node);
    const card = document.createElement("div");
    const classes = ["dag-node", `node-${kind}`];
    if (activeChainIds.has(node.id)) classes.push("on-active-branch");
    else classes.push("off-active-branch");
    if (node.meta && node.meta.rejected_by_operator) classes.push("rejected-node");
    if (expandedIds.has(node.id)) classes.push("expanded");
    if (selectedId === node.id) classes.push("selected");
    card.className = classes.join(" ");
    card.dataset.nodeId = node.id;

    const header = document.createElement("div");
    header.className = "dag-node-header";
    const parentShort = node.parent_id ? `← ${node.parent_id.slice(0, 12)}` : "ROOT";
    const rejected = node.meta && node.meta.rejected_by_operator ? '<span class="rejected-tag">REJECTED</span>' : "";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "dag-node-toggle";
    toggle.textContent = expandedIds.has(node.id) ? "▾ 收起" : "▸ 展开";
    toggle.title = "展开/收起节点全文";
    header.innerHTML = `<span class="role-chip role-${kind}">${KIND_LABELS[kind]}</span><span class="node-id">#${node.id}</span>${rejected}`;
    const parentSpan = document.createElement("span");
    parentSpan.className = "node-parent";
    parentSpan.textContent = parentShort;
    header.appendChild(toggle);
    header.appendChild(parentSpan);
    card.appendChild(header);

    const refs = [];
    for (const b of headsByNode[node.id] || []) {
      refs.push(`[branch:${b}${b === currentBranch ? "*" : ""}]`);
    }
    for (const t of tagsByNode[node.id] || []) {
      refs.push(`[tag:${t}]`);
    }
    if (refs.length) {
      const refRow = document.createElement("div");
      refRow.className = "mono-meta";
      refRow.style.color = "#38bdf8";
      refRow.textContent = refs.join(" ");
      card.appendChild(refRow);
    }

    const snippet = document.createElement("div");
    snippet.className = "dag-node-snippet";
    snippet.textContent = nodeSnippet(node, kind);
    card.appendChild(snippet);

    let detail = null;
    const setExpanded = (wantExpanded) => {
      if (wantExpanded) {
        expandedIds.add(node.id);
        if (!detail) {
          detail = buildDetail(node);
          card.appendChild(detail);
        } else {
          detail.style.display = "";
        }
        card.classList.add("expanded");
        toggle.textContent = "▾ 收起";
      } else {
        expandedIds.delete(node.id);
        if (detail) detail.style.display = "none";
        card.classList.remove("expanded");
        toggle.textContent = "▸ 展开";
      }
    };

    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      setExpanded(!expandedIds.has(node.id));
    });

    card.addEventListener("click", () => {
      selectedIds.set(container.id, node.id);
      for (const el of container.querySelectorAll(".dag-node.selected")) {
        el.classList.remove("selected");
      }
      card.classList.add("selected");
      if (callbacks.onSelectNode) {
        callbacks.onSelectNode(node);
      }
    });

    container.appendChild(card);
  }
}

export function renderBranchBar(container, treeData, callbacks = {}) {
  if (!container) return;
  container.innerHTML = "";
  const branches = (treeData && treeData.branches) || { main: "" };
  const currentBranch = (treeData && treeData.current_branch) || "main";

  for (const [bName, headId] of Object.entries(branches)) {
    const chip = document.createElement("button");
    chip.className = `branch-chip ${bName === currentBranch ? "active" : ""}`;
    chip.textContent = `${bName} (${(headId || "").slice(0, 8)})`;
    chip.title = `Checkout branch ${bName} @ ${headId}`;
    chip.addEventListener("click", () => {
      if (bName !== currentBranch && callbacks.onCheckoutBranch) {
        callbacks.onCheckoutBranch(bName);
      }
    });
    container.appendChild(chip);
  }
}
