/**
 * <agent-dag-timeline> (dag_timeline.js) — pure native Custom Element v1.
 * Renders the causal rewind event axis from /tasks/:id/rewind_ledger: every
 * autonomous branch_rewind is drawn as a dashed red SVG arc from the abandoned
 * branch head to the new exploration branch, highlighting the failed_lesson
 * and the dirty workspace files left behind, plus the active branch ledger
 * Section 4 ([Branch Rewind Lesson] red lines) fed via the shared store.
 */
import { store } from "./store.js";

const ARC_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}, text = null) {
  const node = document.createElementNS(ARC_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== null) node.textContent = text;
  return node;
}

function shortReason(text, max = 26) {
  const compact = String(text || "").replace(/\s+/g, " ").trim();
  return compact.length > max ? `${compact.slice(0, max)}…` : compact || "路线失败";
}

function rewindArcSvg(event, index) {
  const svg = svgEl("svg", {
    class: "rewind-arc-svg",
    viewBox: "0 0 640 88",
    preserveAspectRatio: "xMidYMid meet",
    role: "img",
    "aria-label": `rewind ${index + 1}`,
  });
  const defs = svgEl("defs");
  const marker = svgEl("marker", {
    id: `rewind-arrow-${index}`,
    viewBox: "0 0 10 10",
    refX: "9",
    refY: "5",
    markerWidth: "7",
    markerHeight: "7",
    orient: "auto-start-reverse",
  });
  marker.appendChild(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#ef4444" }));
  defs.appendChild(marker);
  svg.appendChild(defs);

  svg.appendChild(svgEl("path", { d: "M 14 44 Q 320 100, 622 44", fill: "none", stroke: "#ef4444",
    "stroke-width": "1.6", "stroke-dasharray": "5,4", "marker-end": `url(#rewind-arrow-${index})` }));

  const leftLabel = svgEl("text", { x: "18", y: "22", class: "rewind-label rewind-label-dead" },
    `✕ ${event.from_branch || "?"}`);
  const leftNode = svgEl("text", { x: "18", y: "38", class: "rewind-node-id" }, `@ ${event.from_head || "?"}`);
  const rightLabel = svgEl("text", { x: "622", y: "22", "text-anchor": "end", class: "rewind-label rewind-label-alive" },
    `➤ ${event.new_branch || "?"}`);
  const rightNode = svgEl("text", { x: "622", y: "38", "text-anchor": "end", class: "rewind-node-id" },
    `@ ${event.anchor_node_id || event.target_node_id || "?"}`);
  svg.appendChild(leftLabel);
  svg.appendChild(leftNode);
  svg.appendChild(rightLabel);
  svg.appendChild(rightNode);

  svg.appendChild(svgEl("text", { x: "320", y: "84", "text-anchor": "middle", class: "rewind-reason" },
    `──( ${shortReason(event.failed_lesson)} )──`));
  return svg;
}

function rewindEventCard(event, index) {
  const card = document.createElement("div");
  card.className = "rewind-card";

  const meta = document.createElement("div");
  meta.className = "rewind-meta";
  meta.innerHTML = `<span class="rewind-badge">[回溯 #${index + 1}]</span>` +
    `<span class="mono-meta">${event.timestamp || ""} · plan_hash: ${event.plan_hash || "-"}</span>`;
  card.appendChild(meta);
  card.appendChild(rewindArcSvg(event, index));

  const lesson = document.createElement("div");
  lesson.className = "rewind-lesson";
  lesson.textContent = `⛔ failed_lesson: ${event.failed_lesson || "(未记录)"}`;
  card.appendChild(lesson);

  const files = Array.isArray(event.touched_files) ? event.touched_files : [];
  if (files.length) {
    const filesRow = document.createElement("div");
    filesRow.className = "rewind-files";
    const caption = document.createElement("span");
    caption.className = "detail-caption";
    caption.textContent = "废弃脏文件 (touched_files):";
    filesRow.appendChild(caption);
    for (const file of files) {
      const chip = document.createElement("code");
      chip.className = "rewind-file-chip";
      chip.textContent = file;
      filesRow.appendChild(chip);
    }
    card.appendChild(filesRow);
  }
  return card;
}

export class AgentDagTimeline extends HTMLElement {
  constructor() {
    super();
    this._render = this._render.bind(this);
  }

  connectedCallback() {
    this._render();
    store.addEventListener("slice:dag", this._render);
    store.addEventListener("slice:ledger", this._render);
  }

  disconnectedCallback() {
    store.removeEventListener("slice:dag", this._render);
    store.removeEventListener("slice:ledger", this._render);
  }

  _render() {
    const { dag, factLedgerSection4 } = store.state;
    this.innerHTML = "";

    const branchBadge = document.createElement("div");
    branchBadge.className = "rewind-branch-line";
    branchBadge.innerHTML = `<span class="badge branch-badge">活跃分支: ${dag.currentBranch || "main"}</span>` +
      `<span class="mono-meta">rewind 事件: ${(dag.rewindEvents || []).length}</span>`;
    this.appendChild(branchBadge);

    const events = Array.isArray(dag.rewindEvents) ? dag.rewindEvents : [];
    if (!events.length) {
      const hint = document.createElement("div");
      hint.className = "empty-hint";
      hint.textContent = "当前任务暂无因果回溯记录（rewind_ledger.jsonl 不存在或为空）";
      this.appendChild(hint);
    }
    for (let i = 0; i < events.length; i++) {
      this.appendChild(rewindEventCard(events[i], i));
    }

    const section = document.createElement("details");
    section.className = "rewind-ledger-section";
    section.open = true;
    const summary = document.createElement("summary");
    summary.textContent = "⛔ 活跃分支账本 · 第 4 节（被否决方案与红线）";
    section.appendChild(summary);
    const body = document.createElement("div");
    body.className = "rewind-ledger-body";
    if (!factLedgerSection4.length) {
      body.innerHTML = '<div class="empty-hint">第 4 节暂无条目</div>';
    }
    for (const line of factLedgerSection4) {
      const item = document.createElement("div");
      item.className = line.includes("[Branch Rewind Lesson]") ? "rewind-ledger-line lesson" : "rewind-ledger-line";
      item.textContent = line;
      body.appendChild(item);
    }
    section.appendChild(body);
    this.appendChild(section);
  }
}

customElements.define("agent-dag-timeline", AgentDagTimeline);
