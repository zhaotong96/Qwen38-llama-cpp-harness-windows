/**
 * Git-Style Context DAG Tree Renderer (tree_view.js)
 * Pure native ES Module — zero build step, 100% projection of .agent/tasks/<id>/ files.
 */

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

  for (const node of nodes) {
    const card = document.createElement("div");
    const classes = ["dag-node"];
    if (activeChainIds.has(node.id)) classes.push("on-active-branch");
    if (node.type === "compaction") classes.push("compaction-node");
    if (node.meta && node.meta.rejected_by_operator) classes.push("rejected-node");
    card.className = classes.join(" ");

    const header = document.createElement("div");
    header.className = "dag-node-header";
    const parentShort = node.parent_id ? `← ${node.parent_id.slice(0, 10)}` : "ROOT";
    header.innerHTML = `<span>#${node.id} (${node.role || node.type})</span><span>${parentShort}</span>`;
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
    let text = "";
    if (typeof node.content === "string") {
      text = node.content;
    } else if (Array.isArray(node.content)) {
      text = "[Multimodal Observation]";
    }
    if (node.tool_calls && node.tool_calls.length) {
      const toolNames = node.tool_calls.map((tc) => tc.function?.name || "tool").join(", ");
      text = `[tool_calls: ${toolNames}] ${text}`;
    }
    snippet.textContent = text || "(empty)";
    card.appendChild(snippet);

    card.addEventListener("click", () => {
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
