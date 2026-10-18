/**
 * Unified Dual-Control Message Queue Panel (queue_panel.js)
 * Allows both human Web operators and external API agents to view, hold/resume,
 * edit content, reprioritize, and cancel queued instructions in .agent/queue/.
 */

export function renderQueuePanel(container, countBadge, queueItems, callbacks = {}) {
  if (countBadge) {
    countBadge.textContent = String((queueItems || []).length);
  }
  if (!container) return;
  container.innerHTML = "";

  if (!queueItems || !queueItems.length) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = "队列为空 (.agent/queue/ 无待处理指令)";
    container.appendChild(empty);
    return;
  }

  for (const item of queueItems) {
    const card = document.createElement("div");
    card.className = `queue-card status-${item.status}`;

    const meta = document.createElement("div");
    meta.className = "dag-node-header";
    meta.innerHTML = `<span>#${item.id} [prio=${item.priority}] (${item.status})</span><span>by: ${item.source}/${item.author}</span>`;
    card.appendChild(meta);

    const contentBox = document.createElement("div");
    contentBox.className = "msg-body";
    contentBox.textContent = item.content;
    card.appendChild(contentBox);

    const actions = document.createElement("div");
    actions.className = "queue-actions";

    const toggleHoldBtn = document.createElement("button");
    toggleHoldBtn.className = "btn btn-small";
    const nextStatus = item.status === "held" ? "queued" : "held";
    toggleHoldBtn.textContent = item.status === "held" ? "释放执行 (Resume)" : "挂起拦截 (Hold)";
    toggleHoldBtn.addEventListener("click", () => {
      if (callbacks.onModify) {
        callbacks.onModify(item.id, { status: nextStatus, by: "web-operator" });
      }
    });
    actions.appendChild(toggleHoldBtn);

    const editBtn = document.createElement("button");
    editBtn.className = "btn btn-small";
    editBtn.textContent = "修改内容 (Edit)";
    editBtn.addEventListener("click", () => {
      const updated = window.prompt("修改排队指令内容 (双端纠错):", item.content);
      if (updated !== null && updated.trim() && callbacks.onModify) {
        callbacks.onModify(item.id, { content: updated.trim(), by: "web-operator" });
      }
    });
    actions.appendChild(editBtn);

    const boostBtn = document.createElement("button");
    boostBtn.className = "btn btn-small";
    boostBtn.textContent = "提至高优 (Prio 10)";
    boostBtn.addEventListener("click", () => {
      if (callbacks.onModify) {
        callbacks.onModify(item.id, { priority: 10, by: "web-operator" });
      }
    });
    actions.appendChild(boostBtn);

    const cancelBtn = document.createElement("button");
    cancelBtn.className = "btn btn-small btn-danger";
    cancelBtn.textContent = "取消撤回 (Cancel)";
    cancelBtn.addEventListener("click", () => {
      if (callbacks.onCancel) {
        callbacks.onCancel(item.id);
      }
    });
    actions.appendChild(cancelBtn);

    card.appendChild(actions);
    container.appendChild(card);
  }
}
