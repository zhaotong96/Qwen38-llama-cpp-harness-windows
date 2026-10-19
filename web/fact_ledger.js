/**
 * Fact Ledger Board (fact_ledger.js)
 * Pure native ES Module. Projects the newest compaction node (or live operator
 * corrections) into a pinned, collapsible five-section fact ledger.
 */
import { contentToText } from "./tree_view.js";

const SECTION_META = [
  { index: 1, emoji: "🎯", title: "核心目标与进度", open: true },
  { index: 2, emoji: "🏛️", title: "已验证事实与环境基线", open: false },
  { index: 3, emoji: "📝", title: "已修改文件清单", open: false },
  { index: 4, emoji: "⛔", title: "被否决方案与操作员红线", open: true },
  { index: 5, emoji: "🚀", title: "待完成子目标与下一步行动", open: true },
  { index: 6, emoji: "🗂️", title: "已归档原始 DAG 节点索引表", open: false },
];

const REDLINE_MARKERS = ["[Operator correction]", "[EXECUTION_REJECTED]", "[EXECUTION_INTERRUPTED_AND_IGNORED]"];

function activeChain(treeData) {
  const nodes = (treeData && treeData.nodes) || [];
  const branches = (treeData && treeData.branches) || {};
  const branch = (treeData && treeData.current_branch) || "main";
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));
  const chain = [];
  let currId = branches[branch] || (nodes.length ? nodes[nodes.length - 1].id : null);
  const visited = new Set();
  while (currId && nodeMap.has(currId) && !visited.has(currId)) {
    visited.add(currId);
    const node = nodeMap.get(currId);
    chain.push(node);
    currId = node.parent_id;
  }
  return chain.reverse();
}

export function parseLedger(content) {
  const text = contentToText(content).replace(/^\[Context Summary\]\s*/, "");
  const sections = [];
  const pattern = /^\s*([1-6])\.\s*(.+)$/gm;
  const matches = [...text.matchAll(pattern)];
  for (let i = 0; i < matches.length; i += 1) {
    const match = matches[i];
    const start = match.index + match[0].length;
    const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
    sections.push({
      index: Number(match[1]),
      heading: match[2].trim(),
      body: text.slice(start, end).trim(),
    });
  }
  return sections;
}

function redlineLines(chain) {
  const lines = [];
  for (const node of chain) {
    const text = contentToText(node.content);
    for (const marker of REDLINE_MARKERS) {
      if (!text.includes(marker)) continue;
      for (const line of text.split("\n")) {
        const trimmed = line.trim();
        if (trimmed.includes(marker) && !lines.includes(trimmed)) lines.push(trimmed);
      }
    }
  }
  return lines;
}

function firstUserText(chain) {
  for (const node of chain) {
    if (node.role !== "user") continue;
    const text = contentToText(node.content).trim();
    if (text && !text.startsWith("[Operator correction]")) return text;
  }
  return "";
}

function makeSection(meta, body) {
  const details = document.createElement("details");
  details.className = `ledger-section ledger-section-${meta.index}`;
  if (meta.index === 4) details.classList.add("ledger-redlines");
  if (meta.open) details.open = true;

  const summary = document.createElement("summary");
  summary.textContent = `${meta.emoji} ${meta.index}. ${meta.title}`;
  details.appendChild(summary);

  const bodyEl = document.createElement("div");
  bodyEl.className = "ledger-section-body";
  bodyEl.textContent = body || "(暂无)";
  details.appendChild(bodyEl);
  return details;
}

export function renderFactLedger(container, badge, treeData) {
  if (!container) return;
  container.innerHTML = "";
  if (badge) badge.textContent = "live";

  const chain = activeChain(treeData);
  let latest = null;
  for (let i = chain.length - 1; i >= 0; i -= 1) {
    if (chain[i].type === "compaction") {
      latest = chain[i];
      break;
    }
  }

  if (latest) {
    if (badge) badge.textContent = `compaction ${latest.id}`;
    const parsed = parseLedger(latest.content);
    const byIndex = new Map(parsed.map((s) => [s.index, s]));
    for (const meta of SECTION_META) {
      const section = byIndex.get(meta.index);
      container.appendChild(makeSection(meta, section ? section.body : ""));
    }
    return;
  }

  const hint = document.createElement("div");
  hint.className = "ledger-live-hint";
  hint.textContent = "尚未触发压缩：以下为实时兜底投影（首条 user 指令 + 当前红线）。";
  container.appendChild(hint);

  const goalText = firstUserText(chain) || "(尚未提交 user 指令)";
  container.appendChild(makeSection(SECTION_META[0], goalText));

  const liveRedlines = redlineLines(chain);
  container.appendChild(makeSection(SECTION_META[3], liveRedlines.join("\n") || "(暂无操作员红线)"));

  const pending = [];
  for (const node of chain) {
    if (node.role !== "assistant") continue;
    const text = contentToText(node.content).trim();
    if (!text) continue;
    pending.push(text.split("\n")[0].slice(0, 200));
  }
  container.appendChild(
    makeSection(SECTION_META[4], pending.slice(-5).reverse().join("\n") || "(暂无待办投影)")
  );
}
