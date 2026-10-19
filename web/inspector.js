/**
 * Whitebox Inspector Drawer (inspector.js)
 * Pure native ES Module. Slides out the raw, unmodified JSON payload of any node.
 */

export function openInspector(node) {
  const drawer = document.getElementById("whitebox-inspector-drawer");
  const pre = document.getElementById("inspector-json");
  const label = document.getElementById("inspector-node-id");
  if (!drawer || !pre) return;
  pre.textContent = JSON.stringify(node, null, 2);
  if (label) label.textContent = node && node.id ? `#${node.id}` : "";
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
}

export function closeInspector() {
  const drawer = document.getElementById("whitebox-inspector-drawer");
  if (!drawer) return;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
}

export function bindInspector() {
  document.getElementById("btn-close-inspector")?.addEventListener("click", closeInspector);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeInspector();
  });
}
