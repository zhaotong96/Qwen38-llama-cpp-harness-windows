/**
 * AgentStore (store.js) — zero-build unidirectional data-flow state machine.
 * Pure native EventTarget: patch() merges state slices synchronously and
 * dispatches ONE batched CustomEvent per slice at the end of the current
 * microtask (queueMicrotask), so high-frequency SSE bursts never thrash layout.
 */
export class AgentStore extends EventTarget {
  constructor() {
    super();
    this._state = {
      taskId: null,
      status: "idle", // running | paused | done | blocked | finished
      stepRiskyPending: null, // step_risky paused high-risk tool call
      screenshot: { url: null, naturalWidth: 1920, naturalHeight: 1080, originX: 0, originY: 0, dpiScale: 1.0 },
      uiTree: { hwnd: 0, windowTitle: "", anemic: false, controls: [] },
      hoveredControlId: null,
      selectedControlId: null,
      interventionDraft: "", // auto-filled into the operator intervention box
      dag: { currentBranch: "main", branches: {}, nodes: [], rewindEvents: [] },
      factLedgerSection4: [], // five-section ledger Section 4 (red lines & rewind lessons)
    };
    this._dirtySlices = new Set();
    this._flushScheduled = false;
  }

  get state() {
    return this._state;
  }

  patch(partial, slices = ["global"]) {
    Object.assign(this._state, partial);
    for (const slice of slices) this._dirtySlices.add(slice);
    if (!this._flushScheduled) {
      this._flushScheduled = true;
      queueMicrotask(() => this._flush());
    }
  }

  _flush() {
    this._flushScheduled = false;
    const changed = Array.from(this._dirtySlices);
    this._dirtySlices.clear();
    for (const slice of changed) {
      this.dispatchEvent(new CustomEvent(`slice:${slice}`, { detail: { state: this._state, slice } }));
    }
    this.dispatchEvent(new CustomEvent("state:change", { detail: { state: this._state, changed } }));
  }

  /**
   * Ingest a /tasks/:id/ui_tree payload and pre-sort controls once at load time
   * by (interactive-role priority ASC, physical area ASC, depth DESC) so the
   * smallest actionable leaf controls are scanned first during hit-testing.
   */
  setUiTreeSnapshot(rawTreePayload) {
    const interactiveRoles = new Set([
      "Button", "Edit", "CheckBox", "ComboBox", "MenuItem",
      "ListItem", "TabItem", "Hyperlink", "TreeItem", "RadioButton", "Slider",
    ]);
    const rawControls = Array.isArray(rawTreePayload.controls) ? rawTreePayload.controls : [];
    const sortedControls = rawControls
      .map((control) => {
        const [l, t, r, b] = control.rect || [0, 0, 0, 0];
        const width = Math.max(0, r - l);
        const height = Math.max(0, b - t);
        return {
          ...control,
          width,
          height,
          area: width * height,
          rolePriority: interactiveRoles.has(control.role) ? 0 : 1,
        };
      })
      .filter((control) => control.area > 0)
      .sort((a, b) => {
        if (a.rolePriority !== b.rolePriority) return a.rolePriority - b.rolePriority;
        if (a.area !== b.area) return a.area - b.area;
        return (b.depth || 0) - (a.depth || 0);
      });

    this.patch(
      {
        uiTree: {
          hwnd: rawTreePayload.hwnd || 0,
          windowTitle: rawTreePayload.window_title || "",
          anemic: Boolean(rawTreePayload.anemic),
          controls: sortedControls,
        },
        screenshot: {
          ...this._state.screenshot,
          originX: rawTreePayload.origin?.[0] ?? 0,
          originY: rawTreePayload.origin?.[1] ?? 0,
          dpiScale: rawTreePayload.dpi_scale ?? 1.0,
        },
      },
      ["uia", "screenshot"]
    );
  }

  /**
   * One-click "point & shoot" operator override: hit a control -> control_id
   * directive; hit owner-drawn blank space -> exact physical pixel coordinates.
   */
  selectCanvasTarget({ controlId = null, physX = null, physY = null }) {
    let draft = "";
    if (controlId !== null) {
      const control = this._state.uiTree.controls.find((c) => c.id === controlId);
      const label = control ? ` (${control.role} "${control.name || control.automation_id || ""}")` : "";
      draft = `[Operator Override] 请直接调用 mouse(action="click", control_id=${controlId}) 点击目标控件${label}`;
    } else if (physX !== null && physY !== null) {
      draft = `[Operator Override] 目标为自绘区域，请直接调用 mouse(action="click", x=${Math.round(physX)}, y=${Math.round(physY)})`;
    }
    this.patch(
      { selectedControlId: controlId, interventionDraft: draft },
      ["uia", "intervention"]
    );
  }
}

export const store = new AgentStore();
