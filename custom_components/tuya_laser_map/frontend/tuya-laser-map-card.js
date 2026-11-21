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
const inPoly = (pt, poly) => {  // ray casting
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < ((xj - xi) * (pt[1] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
};
const CLEAN_MODES = [["sweep", "Aspirar"], ["mop", "Fregar"], ["sweep_and_mop", "Ambos"]];
const LEVELS = { low: "Baja", medium: "Media", high: "Alta" };
// tuya-local entity suffixes on the robot's device: [life sensor, reset button, label]
const PARTS = [["filter_life", "reset_filter", "Filtro"], ["side_brush_lifetime", "reset_edge_brush", "Cepillo lateral"],
  ["rolling_brush_lifetime", "reset_roll_brush", "Cepillo principal"], ["duster_cloth_lifetime", "reset_mop_cloth", "Mopa"]];
const ACTIVE = ["cleaning", "paused", "returning"];

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
    this._z = { s: 1, x: 0, y: 0 };  // zoom/pan of the map
    if (!this.shadowRoot) this._build();
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.states).find((e) => e.startsWith("camera.") && hass.states[e].attributes.calibration_points);
    return { entity };
  }

  getCardSize() { return 9; }

  // --- the robot's other entities (tuya-local), found on the device of the vacuum we mirror ---------
  _relatedIds(hass, vac) {
    const src = (vac && vac.attributes.source_vacuum) || this._cfg.vacuum;
    const ents = hass.entities || {}, dev = ents[src] && ents[src].device_id;
    if (!dev) return [];
    if (this._relKey !== dev) {
      this._relKey = dev;
      this._ids = Object.keys(ents).filter((id) => ents[id].device_id === dev);
    }
    return this._ids;
  }

  _ent(domain, suffix) {  // entity id on the robot's device ending in _<suffix>
    return (this._ids || []).find((id) => id.startsWith(domain + ".") && id.endsWith("_" + suffix));
  }

  _state(domain, suffix) { const id = this._ent(domain, suffix); return id && this._hass.states[id]; }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._cfg.entity];
    const vac = this._cfg.vacuum && hass.states[this._cfg.vacuum];
    const rel = this._relatedIds(hass, vac).map((e) => hass.states[e]);
    if (st === this._st && vac === this._vac && rel.every((r, i) => r === (this._rel || [])[i])) return;
    this._st = st;
    this._vac = vac;
    this._rel = rel;
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
    const r = this._svg.getBoundingClientRect();
    return this._toRobot((ev.clientX - r.left) / r.width * this._img.naturalWidth,
      (ev.clientY - r.top) / r.height * this._img.naturalHeight);
  }

  get _k() {  // image px per screen px, so handles stay finger-sized
    const w = this._svg.clientWidth || 1;
    return (this._img.naturalWidth || w) / w / this._z.s;
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
          -webkit-user-select: none; -webkit-touch-callout: none; touch-action: pan-y; }
        .map.editing { touch-action: none; }
        .map.zoomed { touch-action: none; }
        .stage { position: relative; transform-origin: 0 0; }
        .map img { display: block; width: 100%; height: auto; }
        .map svg { position: absolute; inset: 0; width: 100%; height: 100%; }
        .zoom { position: absolute; top: 8px; right: 8px; display: flex; flex-direction: column; gap: 6px; }
        .zoom button { width: 32px; height: 32px; border: 0; border-radius: 9px; background: rgba(255,255,255,.92);
          box-shadow: 0 1px 4px rgba(0,0,0,.18); font-size: 18px; line-height: 1; color: #1c1c1e; cursor: pointer; }
        .menu { position: absolute; display: none; flex-direction: column; min-width: 170px; background: rgba(255,255,255,.97);
          border-radius: 12px; box-shadow: 0 6px 24px rgba(0,0,0,.22); overflow: hidden; z-index: 2; }
        .menu.show { display: flex; }
        .menu button { border: 0; background: none; text-align: left; padding: 11px 14px; font: inherit; font-size: 14px;
          color: #1c1c1e; cursor: pointer; }
        .menu button + button { border-top: 1px solid rgba(60,60,67,.14); }
        .alert { display: none; margin-bottom: 10px; padding: 10px 12px; border-radius: 10px; background: rgba(255,59,48,.12);
          color: #FF3B30; font-size: 14px; font-weight: 500; }
        .alert.show { display: block; }
        .opts { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 12px; }
        .opts:empty { display: none; }
        .opt { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--secondary-text-color); }
        .maint { margin-top: 12px; font-size: 14px; }
        .maint summary { cursor: pointer; color: var(--secondary-text-color); font-size: 13px; }
        .row { display: flex; align-items: center; gap: 10px; padding: 7px 0; }
        .row + .row { border-top: 1px solid rgba(60,60,67,.12); }
        .row .name { flex: 1; }
        .row .val { color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
        .row .val.due { color: #FF3B30; font-weight: 600; }
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
        <div class="alert"></div>
        <div class="map"><div class="stage"><img alt=""><svg xmlns="${NS}"></svg></div>
          <div class="zoom"><button data-z="1.5" title="Acercar">+</button><button data-z="0.667" title="Alejar">−</button></div>
          <div class="menu"></div><div class="toast"></div></div>
        <div class="opts"></div>
        <div class="bar"></div>
        <details class="maint"><summary>Mantenimiento</summary><div class="parts"></div></details>
      </ha-card>`;
    this._img = root.querySelector("img");
    this._svg = root.querySelector("svg");
    this._bar = root.querySelector(".bar");
    this._mapEl = root.querySelector(".map");
    this._stage = root.querySelector(".stage");
    this._menu = root.querySelector(".menu");
    root.querySelectorAll(".zoom button").forEach((b) => b.addEventListener("click", () => {
      const r = this._mapEl.getBoundingClientRect();
      this._zoomAt(this._z.s * Number(b.dataset.z), r.width / 2, r.height / 2);
    }));
    this._gestures();
    this._img.addEventListener("load", () => {
      this._svg.setAttribute("viewBox", `0 0 ${this._img.naturalWidth} ${this._img.naturalHeight}`);
      this._render();
    });
    root.querySelectorAll(".head .seg button").forEach((b) => b.addEventListener("click", () => this._setMode(b.dataset.m)));
    this._svg.addEventListener("pointerdown", (ev) => {
      if (ev.target === this._svg && this._sel) { this._sel = null; this._render(); }
      this._menu.classList.remove("show");
    });
  }

  // --- zoom, pan, long press ----------------------------------------------------------------
  _zoomAt(s, cx, cy) {  // new scale s, keeping the map point under (cx, cy) (px inside .map) still
    const z = this._z, w = this._mapEl.clientWidth, h = this._mapEl.clientHeight;
    s = Math.min(5, Math.max(1, s));
    z.x = cx - (cx - z.x) * (s / z.s);
    z.y = cy - (cy - z.y) * (s / z.s);
    z.s = s;
    this._pan(0, 0, w, h);
  }

  _pan(dx, dy, w = this._mapEl.clientWidth, h = this._mapEl.clientHeight) {
    const z = this._z;
    z.x = Math.min(0, Math.max(w - w * z.s, z.x + dx));
    z.y = Math.min(0, Math.max(h - h * z.s, z.y + dy));
    this._stage.style.transform = z.s === 1 ? "" : `translate(${z.x}px, ${z.y}px) scale(${z.s})`;
    this._mapEl.classList.toggle("zoomed", z.s > 1);
    clearTimeout(this._zoomT);  // redraw handles at finger size once the gesture settles
    this._zoomT = setTimeout(() => this._render(), 120);
  }

  _gestures() {
    const el = this._mapEl, pts = new Map();
    let start = null, pinch = null, moved = false, timer = null;
    const local = (e) => { const r = el.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    el.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".zoom, .menu")) return;
      pts.set(e.pointerId, local(e));
      if (pts.size === 2) {
        clearTimeout(timer);
        const [a, b] = [...pts.values()];
        pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), s: this._z.s };
        moved = true;
      } else if (pts.size === 1) {
        start = local(e);
        moved = false;
        pinch = null;
        if (this._mode === "clean") {
          timer = setTimeout(() => { moved = true; this._longPress(e); }, 550);
        }
      }
    });
    el.addEventListener("pointermove", (e) => {
      if (!pts.has(e.pointerId)) return;
      const p = local(e), prev = pts.get(e.pointerId);
      pts.set(e.pointerId, p);
      if (pinch && pts.size === 2) {
        const [a, b] = [...pts.values()];
        this._zoomAt(pinch.s * Math.hypot(a[0] - b[0], a[1] - b[1]) / pinch.d, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
      } else if (pts.size === 1 && start) {
        if (Math.hypot(p[0] - start[0], p[1] - start[1]) > 8) { moved = true; clearTimeout(timer); }
        if (moved && this._z.s > 1) this._pan(p[0] - prev[0], p[1] - prev[1]);
      }
    });
    const end = (e) => {
      pts.delete(e.pointerId);
      clearTimeout(timer);
      if (!pts.size && moved) {  // a pan, pinch or long press is not also a tap on a room
        this._noClick = true;
        setTimeout(() => { this._noClick = false; }, 60);
      }
      if (pts.size < 2) pinch = null;
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    el.addEventListener("wheel", (e) => {  // trackpad pinch / ctrl+wheel
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const [x, y] = local(e);
      this._zoomAt(this._z.s * Math.exp(-e.deltaY / 200), x, y);
    }, { passive: false });
  }

  _longPress(ev) {
    if (!this._toRobot) return;
    const pt = this._pt(ev), rooms = (this._st && this._st.attributes.rooms) || {};
    const hit = Object.entries(rooms).find(([, r]) => inPoly(pt, r.outline));
    const names = this._cfg.room_names || {}, m = this._menu;
    m.replaceChildren();
    const item = (label, fn) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.addEventListener("click", async () => { m.classList.remove("show"); await fn(); });
      m.appendChild(b);
    };
    item("Ir aquí", async () => {
      if (await this._call("tuya_laser_map", "goto", { x: pt[0], y: pt[1] })) this._toast("Yendo al punto");
    });
    if (hit) {
      const [id, r] = hit;
      item(`Limpiar ${names[id] || r.name}`, async () => {
        if (await this._call("tuya_laser_map", "clean_rooms", { rooms: [Number(id)], times: this._times })) {
          this._toast("Enviado al robot");
        }
      });
    }
    const r = this._mapEl.getBoundingClientRect();
    m.style.left = `${Math.min(ev.clientX - r.left, r.width - 180)}px`;
    m.style.top = `${Math.min(ev.clientY - r.top, r.height - 100)}px`;
    m.classList.add("show");
    if (navigator.vibrate) navigator.vibrate(10);
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
    this.shadowRoot.querySelectorAll(".head .seg button").forEach((b) => b.classList.toggle("on", b.dataset.m === this._mode));
    const names = this._cfg.room_names || {};
    const limits = this._mode === "limits" ? this._draft : this._limitsFromState();

    // rooms: tap to select (clean mode), always labelled; while a job runs, the robot's room is outlined
    const active = this._vac && ACTIVE.includes(this._vac.state);
    const cur = active ? a.current_room : null, done = new Set(active ? (a.visited_rooms || []) : []);
    for (const [id, r] of Object.entries(a.rooms || {})) {
      const on = this._rooms.has(Number(id)), here = Number(id) === cur;
      const p = this._el("polygon", { points: this._poly(r.outline), fill: on || here ? BLUE : "transparent",
        "fill-opacity": on ? 0.28 : here ? 0.1 : 0, stroke: here ? BLUE : "none", "stroke-width": 2.5 * k,
        "stroke-linejoin": "round" });
      if (this._mode !== "clean") p.setAttribute("pointer-events", "none");
      else {
        p.style.cursor = "pointer";
        p.addEventListener("click", () => {
          if (this._noClick) return;
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
      t.textContent = (done.has(Number(id)) && Number(id) !== cur ? "✓ " : "") + (names[id] || r.name);
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
    const vac = this._vac, a = this._st ? this._st.attributes : {}, val = (d, sfx) => {
      const e = this._state(d, sfx);
      return e && !["unknown", "unavailable"].includes(e.state) ? e.state : null;
    };
    const parts = [];
    if (vac) parts.push(this._hass.formatEntityState ? this._hass.formatEntityState(vac) : vac.state);
    if (vac && ACTIVE.includes(vac.state)) {
      const r = a.rooms && a.current_room != null && a.rooms[a.current_room];
      if (r) parts.push((this._cfg.room_names || {})[a.current_room] || r.name);
      if (val("sensor", "cleaning_area") != null) parts.push(`${val("sensor", "cleaning_area")} m²`);
      if (val("sensor", "cleaning_time") != null) parts.push(`${val("sensor", "cleaning_time")} min`);
    }
    const bat = val("sensor", "battery") ?? (vac && vac.attributes.battery_level);
    if (bat != null) parts.push(`${bat} %`);
    root.querySelector(".sub").textContent = parts.join(" · ");
    this._renderAlert(vac);
    this._renderOpts();
    this._renderParts();
    this._bar.replaceChildren();
    if (!this._st || !this._toPx) return;
    this._mode === "limits" ? this._limitsBar() : this._cleanBar();
  }

  _renderAlert(vac) {
    const el = this.shadowRoot.querySelector(".alert"), prob = this._state("binary_sensor", "problem");
    const err = (prob && prob.state === "on") || (vac && vac.state === "error");
    const desc = prob && prob.attributes.description;
    el.textContent = err ? `⚠︎ ${desc && desc !== "ok" ? desc : "El robot tiene un problema"}` : "";
    el.classList.toggle("show", !!err);
  }

  _chips(parent, label, options, current, onPick) {
    const wrap = Object.assign(document.createElement("div"), { className: "opt" });
    wrap.appendChild(document.createTextNode(label));
    const seg = Object.assign(document.createElement("div"), { className: "seg" });
    for (const [v, text] of options) {
      const b = Object.assign(document.createElement("button"), { textContent: text, className: v === current ? "on" : "" });
      b.addEventListener("click", () => v !== current && onPick(v));
      seg.appendChild(b);
    }
    wrap.appendChild(seg);
    parent.appendChild(wrap);
  }

  _renderOpts() {
    const el = this.shadowRoot.querySelector(".opts");
    el.replaceChildren();
    if (this._mode !== "clean") return;
    const mode = this._state("select", "cleaning_mode"), water = this._state("select", "mopping"), vac = this._vac;
    const pick = (ent, option) => this._call("select", "select_option", { entity_id: ent.entity_id, option });
    if (mode) this._chips(el, "", CLEAN_MODES.filter(([v]) => mode.attributes.options.includes(v)), mode.state, (v) => pick(mode, v));
    const m = mode ? mode.state : "sweep";
    const speeds = (vac && vac.attributes.fan_speed_list) || [];
    if (m !== "mop" && speeds.length) {
      this._chips(el, "Aspiración", speeds.map((v) => [v, LEVELS[v.toLowerCase()] || v]), vac.attributes.fan_speed,
        (v) => this._call("vacuum", "set_fan_speed", { entity_id: this._cfg.vacuum, fan_speed: v }));
    }
    if (m !== "sweep" && water) {
      this._chips(el, "Agua", water.attributes.options.filter((v) => v !== "off").map((v) => [v, LEVELS[v] || v]),
        water.state, (v) => pick(water, v));
    }
  }

  _renderParts() {
    const el = this.shadowRoot.querySelector(".parts");
    el.replaceChildren();
    const row = (name, value, due, btn) => {
      const r = Object.assign(document.createElement("div"), { className: "row" });
      r.append(Object.assign(document.createElement("span"), { className: "name", textContent: name }));
      if (value != null) r.append(Object.assign(document.createElement("span"), { className: `val ${due ? "due" : ""}`, textContent: value }));
      if (btn) r.append(btn);
      el.appendChild(r);
    };
    const button = (label, ent, confirmText, done) => {
      const b = Object.assign(document.createElement("button"), { className: "btn", textContent: label });
      b.addEventListener("click", async () => {
        if (confirmText && !confirm(confirmText)) return;
        if (await this._call("button", "press", { entity_id: ent })) this._toast(done);
      });
      return b;
    };
    for (const [life, reset, name] of PARTS) {
      const s = this._state("sensor", life), btn = this._ent("button", reset);
      if (!s || isNaN(Number(s.state))) continue;
      const h = Math.round(Number(s.state) / 60);  // minutes left; negative = overdue
      row(name, h > 0 ? `${h} h` : "Cambiar", h <= 0,
        btn && button("Reiniciar", btn, `¿Reiniciar el contador de ${name.toLowerCase()}? Hazlo solo si lo acabas de cambiar.`, "Contador reiniciado"));
    }
    const empty = this._ent("button", "empty_dust");
    if (empty) row("Depósito de polvo", null, false, button("Vaciar", empty, null, "Vaciando depósito"));
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
    if (vac && (busy || state === "paused")) this._btn("Parar", "", () => this._call("vacuum", "stop", { entity_id: vac }));
    if (vac) {
      this._btn("Localizar", "", async () => {
        if (await this._call("vacuum", "locate", { entity_id: vac })) this._toast("El robot está sonando");
      });
      this._btn("Base", "", () => this._call("vacuum", "return_to_base", { entity_id: vac }));
    }
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
      : "Toca habitaciones para elegirlas, o añade una zona. Mantén pulsado un punto para ir allí o limpiar esa habitación.");
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
