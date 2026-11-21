/* Tuya Laser Map card: live map, room/zone cleaning and a drag-to-edit editor for virtual walls and
 * no-go zones. Served by the tuya_laser_map integration; no build step, no dependencies.
 * Geometry is kept in robot coordinates; calibration_points (robot -> image px) give the transform. */

const MODES = { 0: ["Prohibido", "#FF3B30"], 1: ["No aspirar", "#FF9500"], 2: ["No fregar", "#AF52DE"], 3: ["Prohibido", "#FF3B30"] };
const BLUE = "#0A84FF";
const NS = "http://www.w3.org/2000/svg";

const solve3 = (m, v) => {  // Cramer's rule, 3x3
  const det = (a) => a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
    + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]);
  const d = det(m);
  return [0, 1, 2].map((i) => det(m.map((row, r) => row.map((x, c) => (c === i ? v[r] : x)))) / d);
};
const isRect = (pts) => pts.length === 4 && pts.every((p, i) => {
  const q = pts[(i + 1) % 4];
  return p[0] === q[0] || p[1] === q[1];
});
const rectPts = (a, b) => {
  const [x1, x2] = [Math.min(a[0], b[0]), Math.max(a[0], b[0])], [y1, y2] = [Math.min(a[1], b[1]), Math.max(a[1], b[1])];
  return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]];
};
const clone = (o) => JSON.parse(JSON.stringify(o));

class TuyaLaserMapCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) throw new Error("Falta 'entity' (la cámara del mapa)");
    this._cfg = config;
    this._mode = "clean";
    this._rooms = new Set();
    this._zones = [];      // zones to clean: [{points}]
    this._times = 1;
    this._draft = null;    // limits being edited: {walls, zones}
    this._sel = null;      // {kind: "wall"|"zone"|"clean", i}
    if (!this.shadowRoot) this._build();
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.states).find((e) => e.startsWith("camera.") && hass.states[e].attributes.calibration_points);
    return { entity };
  }

  getCardSize() { return 9; }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._cfg.entity];
    const vac = this._cfg.vacuum && hass.states[this._cfg.vacuum];
    if (st === this._st && vac === this._vac) return;
    this._st = st;
    this._vac = vac;
    if (!st) return;
    const a = st.attributes;
    if (a.entity_picture && this._img.getAttribute("src") !== a.entity_picture) this._img.src = a.entity_picture;
    this._calibrate(a.calibration_points || []);
    this._render();
  }

  // --- geometry -------------------------------------------------------------------------
  _calibrate(cal) {
    const key = JSON.stringify(cal);
    if (key === this._calKey || cal.length < 3) return;
    this._calKey = key;
    const m = cal.map((c) => [c.vacuum.x, c.vacuum.y, 1]);
    const ax = solve3(m, cal.map((c) => c.map.x)), ay = solve3(m, cal.map((c) => c.map.y));
    this._toPx = (x, y) => [ax[0] * x + ax[1] * y + ax[2], ay[0] * x + ay[1] * y + ay[2]];
    const det = ax[0] * ay[1] - ax[1] * ay[0];
    this._toRobot = (px, py) => {
      const u = px - ax[2], v = py - ay[2];
      return [Math.round((ay[1] * u - ax[1] * v) / det), Math.round((ax[0] * v - ay[0] * u) / det)];
    };
  }

  _pt(ev) {
    const p = this._svg.createSVGPoint();
    p.x = ev.clientX;
    p.y = ev.clientY;
    const q = p.matrixTransform(this._svg.getScreenCTM().inverse());
    return this._toRobot(q.x, q.y);
  }

  get _k() {  // image px per screen px, so handles stay finger-sized
    const w = this._svg.clientWidth || 1;
    return (this._img.naturalWidth || w) / w;
  }

  get _unitsPerMeter() { return 1000 / ((this._st && this._st.attributes.resolution_cm) || 5); }

  _center() {
    const w = this._img.naturalWidth, h = this._img.naturalHeight;
    return this._toRobot(w / 2, h / 2);
  }

  // --- DOM --------------------------------------------------------------------------------
  _build() {
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        ha-card { padding: 14px; overflow: hidden; }
        .head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
        .title { font-size: 17px; font-weight: 600; letter-spacing: -.2px; }
        .sub { font-size: 13px; color: var(--secondary-text-color); }
        .seg { display: inline-flex; background: rgba(120,120,128,.16); border-radius: 9px; padding: 2px; }
        .seg button { border: 0; background: none; font: inherit; font-size: 13px; font-weight: 500; padding: 6px 14px;
          border-radius: 7px; color: var(--primary-text-color); cursor: pointer; }
        .seg button.on { background: var(--card-background-color, #fff); box-shadow: 0 1px 3px rgba(0,0,0,.15); }
        .map { position: relative; border-radius: 12px; overflow: hidden; background: #fff; user-select: none;
          -webkit-user-select: none; touch-action: pan-y; }
        .map.editing { touch-action: none; }
        .map img { display: block; width: 100%; height: auto; }
        .map svg { position: absolute; inset: 0; width: 100%; height: 100%; }
        .drag { cursor: grab; touch-action: none; }
        .bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 12px; }
        .btn { border: 0; border-radius: 10px; padding: 9px 14px; font: inherit; font-size: 14px; font-weight: 500;
          background: rgba(120,120,128,.14); color: var(--primary-text-color); cursor: pointer; }
        .btn.on, .btn.primary { background: ${BLUE}; color: #fff; }
        .btn.danger { color: #FF3B30; }
        .btn:disabled { opacity: .4; cursor: default; }
        .spacer { flex: 1; }
        .hint { width: 100%; font-size: 12px; color: var(--secondary-text-color); }
        .toast { position: absolute; left: 50%; bottom: 12px; transform: translateX(-50%); background: rgba(0,0,0,.78);
          color: #fff; padding: 8px 14px; border-radius: 10px; font-size: 13px; opacity: 0; transition: opacity .2s;
          pointer-events: none; white-space: nowrap; }
        .toast.show { opacity: 1; }
      </style>
      <ha-card>
        <div class="head">
          <div><div class="title"></div><div class="sub"></div></div>
          <div class="seg"><button data-m="clean">Limpiar</button><button data-m="limits">Límites</button></div>
        </div>
        <div class="map"><img alt=""><svg xmlns="${NS}"></svg><div class="toast"></div></div>
        <div class="bar"></div>
      </ha-card>`;
    this._img = root.querySelector("img");
    this._svg = root.querySelector("svg");
    this._bar = root.querySelector(".bar");
    this._mapEl = root.querySelector(".map");
    this._img.addEventListener("load", () => {
      this._svg.setAttribute("viewBox", `0 0 ${this._img.naturalWidth} ${this._img.naturalHeight}`);
      this._render();
    });
    root.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => this._setMode(b.dataset.m)));
    this._svg.addEventListener("pointerdown", (ev) => {
      if (ev.target === this._svg && this._sel) { this._sel = null; this._render(); }
    });
  }

  _setMode(m) {
    if (m === this._mode) return;
    if (this._mode === "limits" && this._dirty() && !confirm("¿Descartar los cambios sin guardar?")) return;
    this._mode = m;
    this._sel = null;
    this._draft = m === "limits" ? this._limitsFromState() : null;
    this._render();
  }

  _limitsFromState() {
    const a = this._st.attributes;
    return { walls: clone(a.walls || []), zones: clone((a.no_go_zones || []).map((z) => ({ mode: z.mode, points: z.points }))) };
  }

  _dirty() { return this._draft && JSON.stringify(this._draft) !== JSON.stringify(this._limitsFromState()); }

  _toast(msg) {
    const t = this.shadowRoot.querySelector(".toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(this._toastT);
    this._toastT = setTimeout(() => t.classList.remove("show"), 2200);
  }

  async _call(domain, service, data) {
    try {
      await this._hass.callService(domain, service, data);
      return true;
    } catch (e) {
      this._toast(`Error: ${e.message || e}`);
      return false;
    }
  }

  // --- drawing ----------------------------------------------------------------------------
  _el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    (parent || this._svg).appendChild(e);
    return e;
  }

  _poly(pts) { return pts.map((p) => this._toPx(p[0], p[1]).join(",")).join(" "); }

  _render() {
    if (!this._st || !this._toPx || !this._img.naturalWidth) return this._renderBar();
    const a = this._st.attributes, k = this._k, svg = this._svg;
    svg.replaceChildren();
    this._mapEl.classList.toggle("editing", this._mode === "limits");
    this.shadowRoot.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("on", b.dataset.m === this._mode));
    const names = this._cfg.room_names || {};
    const limits = this._mode === "limits" ? this._draft : this._limitsFromState();

    // rooms: tap to select (clean mode), always labelled
    for (const [id, r] of Object.entries(a.rooms || {})) {
      const on = this._rooms.has(Number(id));
      const p = this._el("polygon", { points: this._poly(r.outline), fill: on ? BLUE : "transparent",
        "fill-opacity": on ? 0.28 : 0, stroke: "none" });
      if (this._mode !== "clean") p.setAttribute("pointer-events", "none");
      else {
        p.style.cursor = "pointer";
        p.addEventListener("click", () => {
          this._rooms.has(Number(id)) ? this._rooms.delete(Number(id)) : this._rooms.add(Number(id));
          this._zones = [];
          this._render();
        });
      }
    }
    for (const [id, r] of Object.entries(a.rooms || {})) {
      const [x, y] = this._toPx(r.label[0], r.label[1]);
      const t = this._el("text", { x, y, "text-anchor": "middle", "dominant-baseline": "middle", "font-size": 13 * k,
        "font-weight": 600, fill: "#1c1c1e", stroke: "#fff", "stroke-width": 3.5 * k, "paint-order": "stroke",
        "font-family": "-apple-system, system-ui, sans-serif", "pointer-events": "none" });
      t.textContent = names[id] || r.name;
    }

    // no-go zones and walls (editable in limits mode)
    const edit = this._mode === "limits";
    limits.zones.forEach((z, i) => this._drawZone(z, MODES[z.mode] ? MODES[z.mode][1] : "#FF3B30", edit, "zone", i, limits.zones));
    limits.walls.forEach((w, i) => this._drawWall(w, i, edit));
    // zones to clean
    if (!edit) this._zones.forEach((z, i) => this._drawZone(z, BLUE, true, "clean", i, this._zones));
    this._renderBar();
  }

  _isSel(kind, i) { return this._sel && this._sel.kind === kind && this._sel.i === i; }

  _drawZone(z, color, editable, kind, i, list) {
    const k = this._k, sel = this._isSel(kind, i);
    const body = this._el("polygon", { points: this._poly(z.points), fill: color, "fill-opacity": sel ? 0.3 : 0.18,
      stroke: color, "stroke-width": (sel ? 3 : 2) * k, "stroke-linejoin": "round" });
    if (!editable) { body.setAttribute("pointer-events", "none"); return; }
    body.classList.add("drag");
    body.addEventListener("pointerdown", (ev) => {
      this._sel = { kind, i };
      const snap = clone(z.points);
      this._drag(ev, (p, d) => { list[i].points = snap.map((q) => [q[0] + d[0], q[1] + d[1]]); });
    });
    if (!sel) return;
    z.points.forEach((pt, j) => this._handle(pt, color, (ev) => {
      const snap = clone(z.points);
      this._drag(ev, (p) => {
        list[i].points = isRect(snap) ? rectPts(snap[(j + 2) % 4], p) : snap.map((q, n) => (n === j ? p : q));
      });
    }));
  }

  _drawWall(w, i, editable) {
    const k = this._k, sel = this._isSel("wall", i);
    const [x1, y1] = this._toPx(w[0], w[1]), [x2, y2] = this._toPx(w[2], w[3]);
    this._el("line", { x1, y1, x2, y2, stroke: "#FF3B30", "stroke-width": (sel ? 6 : 4) * k, "stroke-linecap": "round",
      "pointer-events": "none" });
    if (!editable) return;
    const hit = this._el("line", { x1, y1, x2, y2, stroke: "transparent", "stroke-width": 24 * k, "stroke-linecap": "round",
      class: "drag" });
    hit.addEventListener("pointerdown", (ev) => {
      this._sel = { kind: "wall", i };
      const snap = [...w];
      this._drag(ev, (p, d) => { this._draft.walls[i] = [snap[0] + d[0], snap[1] + d[1], snap[2] + d[0], snap[3] + d[1]]; });
    });
    if (!sel) return;
    [[w[0], w[1]], [w[2], w[3]]].forEach((pt, j) => this._handle(pt, "#FF3B30", (ev) => {
      this._drag(ev, (p) => {
        const cur = this._draft.walls[i];
        this._draft.walls[i] = j === 0 ? [p[0], p[1], cur[2], cur[3]] : [cur[0], cur[1], p[0], p[1]];
      });
    }));
  }

  _handle(pt, color, onDown) {
    const k = this._k, [x, y] = this._toPx(pt[0], pt[1]);
    this._el("circle", { cx: x, cy: y, r: 9 * k, fill: "#fff", stroke: color, "stroke-width": 3 * k, "pointer-events": "none" });
    const hit = this._el("circle", { cx: x, cy: y, r: 22 * k, fill: "transparent", class: "drag" });
    hit.addEventListener("pointerdown", onDown);
  }

  _drag(ev, move) {
    ev.preventDefault();
    ev.stopPropagation();
    const svg = this._svg, start = this._pt(ev);
    svg.setPointerCapture(ev.pointerId);
    this._render();
    const mv = (e) => {
      const p = this._pt(e);
      move(p, [p[0] - start[0], p[1] - start[1]]);
      this._render();
    };
    const up = () => {
      svg.removeEventListener("pointermove", mv);
      svg.removeEventListener("pointerup", up);
      svg.removeEventListener("pointercancel", up);
    };
    svg.addEventListener("pointermove", mv);
    svg.addEventListener("pointerup", up);
    svg.addEventListener("pointercancel", up);
  }

  // --- action bar -------------------------------------------------------------------------
  _btn(label, cls, onClick, disabled) {
    const b = document.createElement("button");
    b.className = `btn ${cls || ""}`;
    b.textContent = label;
    b.disabled = !!disabled;
    b.addEventListener("click", onClick);
    this._bar.appendChild(b);
    return b;
  }

  _renderBar() {
    const root = this.shadowRoot;
    root.querySelector(".title").textContent = this._cfg.title || "Mapa";
    const vac = this._vac;
    root.querySelector(".sub").textContent = vac
      ? [this._hass.formatEntityState ? this._hass.formatEntityState(vac) : vac.state,
         vac.attributes.battery_level != null ? `${vac.attributes.battery_level} %` : ""].filter(Boolean).join(" · ")
      : "";
    this._bar.replaceChildren();
    if (!this._st || !this._toPx) return;
    this._mode === "limits" ? this._limitsBar() : this._cleanBar();
  }

  _cleanBar() {
    const vac = this._cfg.vacuum, state = this._vac && this._vac.state;
    const busy = state === "cleaning" || state === "returning";
    this._btn("+ Zona", "", () => {
      const [cx, cy] = this._center(), h = this._unitsPerMeter * 0.75;
      this._rooms.clear();
      this._zones.push({ points: rectPts([cx - h, cy - h], [cx + h, cy + h]) });
      this._sel = { kind: "clean", i: this._zones.length - 1 };
      this._render();
    });
    if (this._sel && this._sel.kind === "clean") {
      this._btn("Quitar zona", "danger", () => { this._zones.splice(this._sel.i, 1); this._sel = null; this._render(); });
    }
    this._btn(`${this._times} pasada${this._times > 1 ? "s" : ""}`, "", () => { this._times = (this._times % 3) + 1; this._renderBar(); });
    this._bar.appendChild(Object.assign(document.createElement("div"), { className: "spacer" }));
    if (vac && busy) {
      this._btn("Pausar", "", () => this._call("vacuum", "pause", { entity_id: vac }));
    }
    if (vac) this._btn("Base", "", () => this._call("vacuum", "return_to_base", { entity_id: vac }));
    const n = this._zones.length || this._rooms.size;
    const label = this._zones.length ? `Limpiar ${n} zona${n > 1 ? "s" : ""}`
      : this._rooms.size ? `Limpiar ${n} habitaci${n > 1 ? "ones" : "ón"}` : state === "paused" ? "Reanudar" : "Limpiar todo";
    this._btn(label, "primary", async () => {
      let ok;
      if (this._zones.length) {
        ok = await this._call("tuya_laser_map", "clean_zone", { times: this._times, zones: this._zones.map((z) => {
          const xs = z.points.map((p) => p[0]), ys = z.points.map((p) => p[1]);
          return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
        }) });
      } else if (this._rooms.size) {
        ok = await this._call("tuya_laser_map", "clean_rooms", { rooms: [...this._rooms], times: this._times });
      } else if (vac) {
        ok = await this._call("vacuum", "start", { entity_id: vac });
      } else return this._toast("Toca habitaciones o añade una zona");
      if (ok) { this._toast("Enviado al robot"); this._zones = []; this._rooms.clear(); this._sel = null; this._render(); }
    });
    this._hint(this._zones.length ? "Arrastra la zona; tócala para ver sus esquinas y estirarla."
      : "Toca habitaciones para elegirlas, o añade una zona.");
  }

  _limitsBar() {
    const d = this._draft, sel = this._sel, u = this._unitsPerMeter;
    this._btn("+ Muro", "", () => {
      const [cx, cy] = this._center();
      d.walls.push([cx - u / 2, cy, cx + u / 2, cy]);
      this._sel = { kind: "wall", i: d.walls.length - 1 };
      this._render();
    });
    this._btn("+ Zona", "", () => {
      const [cx, cy] = this._center();
      d.zones.push({ mode: 0, points: rectPts([cx - u / 2, cy - u / 2], [cx + u / 2, cy + u / 2]) });
      this._sel = { kind: "zone", i: d.zones.length - 1 };
      this._render();
    });
    if (sel && sel.kind === "zone") {
      const z = d.zones[sel.i];
      [[0, "Prohibido"], [1, "No aspirar"], [2, "No fregar"]].forEach(([m, label]) =>
        this._btn(label, (z.mode === m || (m === 0 && z.mode === 3)) ? "on" : "", () => { z.mode = m; this._render(); }));
    }
    if (sel) {
      this._btn("Eliminar", "danger", () => {
        (sel.kind === "wall" ? d.walls : d.zones).splice(sel.i, 1);
        this._sel = null;
        this._render();
      });
    }
    this._bar.appendChild(Object.assign(document.createElement("div"), { className: "spacer" }));
    const dirty = this._dirty();
    this._btn("Descartar", "", () => { this._draft = this._limitsFromState(); this._sel = null; this._render(); }, !dirty);
    this._btn("Guardar", "primary", async () => {
      const ok = await this._call("tuya_laser_map", "set_limits", {
        walls: d.walls.map((w) => w.map(Math.round)),
        no_go_zones: d.zones.map((z) => ({ mode: z.mode, points: z.points.map((p) => p.map(Math.round)) })),
      });
      if (ok) { this._toast("Guardado en el robot"); this._sel = null; this._draft = null; this._mode = "clean"; this._render(); }
    }, !dirty);
    this._hint("Toca un muro o zona para seleccionarlo; arrástralo o estira sus puntos. Nada se envía hasta Guardar.");
  }

  _hint(text) {
    this._bar.appendChild(Object.assign(document.createElement("div"), { className: "hint", textContent: text }));
  }
}

customElements.define("tuya-laser-map-card", TuyaLaserMapCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: "tuya-laser-map-card", name: "Tuya Laser Map", description: "Mapa, limpieza por habitación/zona y editor de límites" });
