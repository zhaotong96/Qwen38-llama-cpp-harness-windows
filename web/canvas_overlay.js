/**
 * <agent-canvas-overlay> (canvas_overlay.js) — pure native Custom Element v1.
 * Layers a crosshair <canvas> over the live screenshot and projects the UIA
 * control tree onto it with +0.5 physical-pixel grid alignment (crisp 1px
 * frames on HiDPI) and microsecond nested-rect hit-testing driven by the
 * pre-sorted store controls.
 */
import { store } from "./store.js";

export class AgentCanvasOverlay extends HTMLElement {
  constructor() {
    super();
    this._rafId = 0;
    this._transform = { scale: 1, offsetX: 0, offsetY: 0, dpr: 1, cssW: 0, cssH: 0 };
  }

  connectedCallback() {
    this.style.position = "relative";
    this.style.display = "block";
    this.style.overflow = "hidden";
    this.style.userSelect = "none";
    this.style.background = "var(--bg-deep, #090d16)";

    this.innerHTML = `
      <img id="agent-screen-img" style="width:100%;height:100%;object-fit:contain;display:block;" alt="Desktop Screenshot" />
      <canvas id="uia-overlay" style="position:absolute;inset:0;width:100%;height:100%;cursor:crosshair;"></canvas>
      <div id="uia-tooltip" style="position:absolute;pointer-events:none;display:none;z-index:20;
           background:rgba(17,17,17,0.92);color:#fff;padding:6px 10px;border-radius:4px;
           font:12px/1.4 monospace;box-shadow:0 2px 8px rgba(0,0,0,0.25);white-space:nowrap;"></div>
    `;

    this._img = this.querySelector("#agent-screen-img");
    this._canvas = this.querySelector("#uia-overlay");
    this._tooltip = this.querySelector("#uia-tooltip");
    this._ctx = this._canvas.getContext("2d");

    this._img.addEventListener("load", () => {
      store.patch(
        {
          screenshot: {
            ...store.state.screenshot,
            naturalWidth: this._img.naturalWidth || 1920,
            naturalHeight: this._img.naturalHeight || 1080,
          },
        },
        ["screenshot"]
      );
      this._scheduleRedraw();
    });

    this._resizeObserver = new ResizeObserver(() => this._scheduleRedraw());
    this._resizeObserver.observe(this);

    this._canvas.addEventListener("pointermove", (e) => this._onPointerMove(e));
    this._canvas.addEventListener("pointerleave", () => {
      this._tooltip.style.display = "none";
      if (store.state.hoveredControlId !== null) {
        store.patch({ hoveredControlId: null }, ["uia"]);
      }
    });
    this._canvas.addEventListener("click", (e) => this._onClick(e));

    this._onSliceUpdate = (event) => {
      if (event && event.type === "slice:screenshot") {
        const { url } = store.state.screenshot;
        if (url && this._img.getAttribute("src") !== url) this._img.src = url;
      }
      this._scheduleRedraw();
    };
    store.addEventListener("slice:uia", this._onSliceUpdate);
    store.addEventListener("slice:screenshot", this._onSliceUpdate);
  }

  disconnectedCallback() {
    this._resizeObserver?.disconnect();
    store.removeEventListener("slice:uia", this._onSliceUpdate);
    store.removeEventListener("slice:screenshot", this._onSliceUpdate);
    if (this._rafId) cancelAnimationFrame(this._rafId);
  }

  _scheduleRedraw() {
    if (this._rafId) return;
    this._rafId = requestAnimationFrame(() => {
      this._rafId = 0;
      this._render();
    });
  }

  _computeTransform() {
    const rect = this._canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const natW = store.state.screenshot.naturalWidth || 1920;
    const natH = store.state.screenshot.naturalHeight || 1080;

    const physW = Math.max(1, Math.round(rect.width * dpr));
    const physH = Math.max(1, Math.round(rect.height * dpr));
    if (this._canvas.width !== physW || this._canvas.height !== physH) {
      this._canvas.width = physW;
      this._canvas.height = physH;
    }

    const scale = Math.min(rect.width / natW, rect.height / natH);
    const drawW = natW * scale;
    const drawH = natH * scale;
    const offsetX = (rect.width - drawW) * 0.5;
    const offsetY = (rect.height - drawH) * 0.5;

    this._transform = { scale, offsetX, offsetY, dpr, cssW: rect.width, cssH: rect.height };
    return this._transform;
  }

  _hitTestPhysical(clientX, clientY) {
    const { scale, offsetX, offsetY } = this._transform;
    const { originX, originY } = store.state.screenshot;
    const physX = originX + (clientX - offsetX) / scale;
    const physY = originY + (clientY - offsetY) / scale;

    // Sequential scan over the (rolePriority, area ASC, depth DESC) pre-sorted
    // array: the smallest actionable leaf wins, parents never shadow children.
    for (const control of store.state.uiTree.controls) {
      const [l, t, r, b] = control.rect;
      if (physX >= l && physX <= r && physY >= t && physY <= b) {
        return { control, physX, physY };
      }
    }
    return { control: null, physX, physY };
  }

  _onPointerMove(e) {
    const { control, physX, physY } = this._hitTestPhysical(e.offsetX, e.offsetY);
    const nextHoverId = control ? control.id : null;
    if (nextHoverId !== store.state.hoveredControlId) {
      store.patch({ hoveredControlId: nextHoverId }, ["uia"]);
    }

    if (control) {
      const [l, t, r, b] = control.rect;
      const cx = Math.round((l + r) * 0.5);
      const cy = Math.round((t + b) * 0.5);
      this._tooltip.innerHTML = `
        <div><b>[ID: ${control.id}] ${control.role}</b> "${control.name || ""}"</div>
        <div>Class: ${control.class_name || "-"} | AutoId: ${control.automation_id || "-"}</div>
        <div>Rect: (${l}, ${t}, ${r}, ${b}) | Center: (${cx}, ${cy})</div>
      `;
      this._tooltip.style.display = "block";
      const tx = Math.min(this._transform.cssW - 260, Math.max(8, e.offsetX + 14));
      const ty = Math.min(this._transform.cssH - 72, Math.max(8, e.offsetY + 14));
      this._tooltip.style.transform = `translate(${Math.round(tx)}px, ${Math.round(ty)}px)`;
    } else {
      this._tooltip.innerHTML = `<div>Custom Canvas Pixel: (${Math.round(physX)}, ${Math.round(physY)})</div>`;
      this._tooltip.style.display = "block";
      this._tooltip.style.transform = `translate(${Math.round(e.offsetX + 14)}px, ${Math.round(e.offsetY + 14)}px)`;
    }
  }

  _onClick(e) {
    const { control, physX, physY } = this._hitTestPhysical(e.offsetX, e.offsetY);
    if (control) {
      store.selectCanvasTarget({ controlId: control.id });
    } else {
      store.selectCanvasTarget({ physX, physY });
    }
  }

  _render() {
    const { scale, offsetX, offsetY, dpr } = this._computeTransform();
    const { originX, originY } = store.state.screenshot;
    const { controls } = store.state.uiTree;
    const { hoveredControlId, selectedControlId } = store.state;
    const ctx = this._ctx;

    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);

    if (!store.state.screenshot.url) {
      ctx.fillStyle = "rgba(148, 163, 184, 0.85)";
      ctx.font = `${Math.round(13 * dpr)}px monospace`;
      ctx.textAlign = "center";
      ctx.fillText("等待截图投影 (/screens) — 提交任务后自动加载", this._canvas.width / 2, this._canvas.height / 2);
      ctx.restore();
      return;
    }

    // Reverse order: large layout containers first, small leaf buttons last.
    for (let i = controls.length - 1; i >= 0; i--) {
      const control = controls[i];
      const [l, t, r, b] = control.rect;
      const cssX = offsetX + (l - originX) * scale;
      const cssY = offsetY + (t - originY) * scale;
      const cssW = (r - l) * scale;
      const cssH = (b - t) * scale;

      // +0.5 physical-pixel center alignment: 1px frames stay razor sharp.
      const devX = Math.floor(cssX * dpr) + 0.5;
      const devY = Math.floor(cssY * dpr) + 0.5;
      const devW = Math.max(1, Math.floor(cssW * dpr));
      const devH = Math.max(1, Math.floor(cssH * dpr));

      const isHovered = control.id === hoveredControlId;
      const isSelected = control.id === selectedControlId;

      if (isSelected || isHovered) {
        ctx.fillStyle = isSelected ? "rgba(26, 115, 232, 0.22)" : "rgba(250, 144, 62, 0.20)";
        ctx.fillRect(devX, devY, devW, devH);
        ctx.lineWidth = Math.max(2, Math.round(2 * dpr));
        ctx.strokeStyle = isSelected ? "#1A73E8" : "#FA903E";
      } else {
        ctx.lineWidth = 1;
        ctx.strokeStyle = control.rolePriority === 0 ? "rgba(18, 181, 203, 0.85)" : "rgba(129, 201, 149, 0.45)";
      }
      ctx.strokeRect(devX, devY, devW, devH);

      if (control.rolePriority === 0 || isHovered || isSelected) {
        const badgeText = `#${control.id} ${control.role}`;
        ctx.font = `${Math.round(11 * dpr)}px monospace`;
        const textW = ctx.measureText(badgeText).width + 6 * dpr;
        const badgeH = 15 * dpr;
        ctx.fillStyle = isSelected ? "#1A73E8" : isHovered ? "#FA903E" : "rgba(17, 17, 17, 0.78)";
        ctx.fillRect(devX, Math.max(0, devY - badgeH), textW, badgeH);
        ctx.fillStyle = "#FFFFFF";
        ctx.fillText(badgeText, devX + 3 * dpr, Math.max(11 * dpr, devY - 3 * dpr));
      }
    }
    ctx.restore();
  }
}

customElements.define("agent-canvas-overlay", AgentCanvasOverlay);
