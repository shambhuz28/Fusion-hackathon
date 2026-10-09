// Avoidance manoeuvre for the analysed conjunction: asks src/server.py (/api/maneuver) for plans, shows
// the details, draws the temporary path (dashed, from the avoidance burn until the satellite is back on its
// original orbit) with its burns, and lets the manoeuvring satellite fly it.
//
// The suggestion is also shown over the simulation (#planHud): the burns with live countdowns, where the
// satellite is on its detour, and the result of checking the detour against the other objects. In the
// scene each burn is labelled, and so are the points where the detour passes nearest to other objects.
//
// The detour leaves the orbit by a few km at most, far too little to see next to a 7,000 km orbit, so its
// off-orbit part (radial and cross-track) can be drawn enlarged. All numbers shown stay true.

import * as THREE from 'three';
import { toScene } from './orbit.js';
import { DATASET, makeSatrec } from './data.js';

const sat = window.satellite;
const $ = (id) => document.getElementById(id);
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const PATH_COLOR = 0x3ddc97;
const BURN_COLORS = [0x3ddc97, 0x5ce1e6, 0x5ce1e6];
const DRAWN_OFF_ORBIT_KM = 250;   // the largest off-orbit distance of an enlarged detour is drawn about this big
const NOT_MOVABLE = ['Debris', 'Rocket body'];
const NEAR_COLOR = 0xffffff;
const LABEL_HEIGHT = 0.026;       // label height as a fraction of the view (labels keep their size on screen)

const pad = (x) => String(x).padStart(2, '0');
function clock(ms) {
  const s = Math.floor(Math.abs(ms) / 1000);
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
}
const km = (x) => (x < 10 ? x.toFixed(2) : x.toFixed(0));

// Text label that starts just right of its position; `row` stacks labels that share a position.
function createLabel(color, row) {
  const canvas = document.createElement('canvas');
  canvas.width = 640; canvas.height = 48;
  const ctx = canvas.getContext('2d');
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false, sizeAttenuation: false }));
  sprite.scale.set(LABEL_HEIGHT * canvas.width / canvas.height, LABEL_HEIGHT, 1);
  sprite.center.set(-0.03, 0.5 - row * 1.15);
  sprite.setText = (text) => {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.font = '600 26px system-ui, sans-serif';
    ctx.fillStyle = 'rgba(8, 12, 22, 0.8)';
    ctx.fillRect(0, 4, Math.min(canvas.width, ctx.measureText(text).width + 20), 40);
    ctx.fillStyle = `#${color.toString(16).padStart(6, '0')}`;
    ctx.fillText(text, 10, 33);
    texture.needsUpdate = true;
  };
  return sprite;
}

// Same rule as can_manoeuvre in src/maneuver.py.
export function canManoeuvre(s) {
  const name = s.name.toUpperCase();
  return !NOT_MOVABLE.includes(s.group) && !name.includes(' DEB') && !name.includes('R/B');
}

// Radial and cross-track components of offset o (TEME km) for the orbital state (pos, vel).
function offOrbit(o, pos, vel) {
  const r = Math.hypot(pos.x, pos.y, pos.z);
  const rh = [pos.x / r, pos.y / r, pos.z / r];
  const n = [pos.y * vel.z - pos.z * vel.y, pos.z * vel.x - pos.x * vel.z, pos.x * vel.y - pos.y * vel.x];
  const nl = Math.hypot(...n);
  const nh = [n[0] / nl, n[1] / nl, n[2] / nl];
  return { rh, nh, x: o[0] * rh[0] + o[1] * rh[1] + o[2] * rh[2], z: o[0] * nh[0] + o[1] * nh[1] + o[2] * nh[2] };
}

// Offset o with its off-orbit part drawn `factor` times larger.
function enlarged(o, pos, vel, factor) {
  if (factor === 1) return o;
  const { rh, nh, x, z } = offOrbit(o, pos, vel);
  const k = factor - 1;
  return [0, 1, 2].map((c) => o[c] + k * (x * rh[c] + z * nh[c]));
}

function formatOffset(ms) {
  const m = Math.round(Math.abs(ms) / 60000);
  const text = m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
  return ms < 0 ? `${text} before TCA` : `${text} after TCA`;
}

// notBefore(): earliest allowed burn time (when the conjunction was screened). After a plan arrives the clock
// rewinds to just before the avoidance burn and plays at 60x, so the burn, the pass and the return are seen.
export function createManeuverView({ scene, state, satByNorad, onPlanChange, notBefore, setSpeed }) {
  const path = new THREE.Line(new THREE.BufferGeometry(),
    new THREE.LineDashedMaterial({ color: PATH_COLOR, dashSize: 0.12, gapSize: 0.06, transparent: true, opacity: 0.95 }));
  const burns = [0, 1, 2].map((k) => new THREE.Mesh(new THREE.SphereGeometry(0.06, 14, 10),
    new THREE.MeshBasicMaterial({ color: BURN_COLORS[k] })));
  const burnLabels = burns.map((_, k) => createLabel(BURN_COLORS[k], k));
  // Where the detour passes nearest to other objects (from the server's check of the whole detour).
  const nears = [0, 1, 2].map(() => new THREE.Mesh(new THREE.SphereGeometry(0.035, 12, 8),
    new THREE.MeshBasicMaterial({ color: NEAR_COLOR })));
  const nearLabels = nears.map((_, k) => createLabel(NEAR_COLOR, -1 - k));
  const group = new THREE.Group();
  group.add(path, ...burns, ...burnLabels, ...nears, ...nearLabels);
  group.visible = false;
  scene.add(group);

  let event = null;        // the conjunction the plans belong to
  let result = null;       // server response
  let selected = 0;        // index into result.plans
  let requestId = 0;
  let failure = null;      // why planning failed (no server, bad request)
  const hud = $('planHud');
  let hudHtml = '', lastHud = 0;

  const planEl = () => $('conjPlan');
  const plan = () => (result?.plans ?? [])[selected];
  const flying = () => plan()?.possible && $('planFly')?.checked !== false;

  // SGP4 states at the path samples up to the last burn, and the enlargement that makes the detour visible.
  function geometry(p) {
    if (p.geometry) return p.geometry;
    const rec = makeSatrec(satByNorad.get(p.manoeuvring_norad));
    const { t0_ms: t0, step_ms: step, offset_km: off } = p.path;
    const last = Math.min(off.length - 1, Math.ceil((p.back_on_orbit_ms - t0) / step));
    const samples = [];
    let far = 0;
    for (let k = 0; k <= last; k++) {
      const s = sat.propagate(rec, new Date(t0 + k * step));
      if (!s?.position) continue;
      const { x, z } = offOrbit(off[k], s.position, s.velocity);
      far = Math.max(far, Math.hypot(x, z));
      samples.push({ ms: t0 + k * step, o: off[k], pos: s.position, vel: s.velocity });
    }
    const raw = far > 0 ? DRAWN_OFF_ORBIT_KM / far : 1;
    const digit = 10 ** Math.floor(Math.log10(Math.max(raw, 1)));
    p.geometry = { samples, factor: Math.max(1, Math.round(raw / digit) * digit) };
    return p.geometry;
  }
  const factor = () => (plan()?.possible && $('planBig')?.checked !== false ? geometry(plan()).factor : 1);

  // Offset (TEME km) of the manoeuvring satellite from its SGP4 position, interpolated from the server's samples.
  function offset(norad, ms) {
    const p = plan();
    if (!flying() || p.manoeuvring_norad !== norad) return null;
    const { t0_ms: t0, step_ms: step, offset_km: off } = p.path;
    const k = (ms - t0) / step;
    if (k <= 0 || k >= off.length - 1) return null;
    const i = Math.floor(k), f = k - i, a = off[i], b = off[i + 1];
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
  }

  // The same offset as it is drawn; s is the satellite's SGP4 state at ms.
  function drawnOffset(norad, ms, s) {
    const o = offset(norad, ms);
    return o && enlarged(o, s.position, s.velocity, factor());
  }

  function drawPath() {
    const p = plan();
    group.visible = !!p?.possible;
    if (!p?.possible) return;
    const { samples } = geometry(p);
    const f = factor();
    const place = (s, target) => {
      const o = enlarged(s.o, s.pos, s.vel, f);
      return toScene(s.pos.x + o[0], s.pos.y + o[1], s.pos.z + o[2], target);
    };
    path.geometry.dispose();
    path.geometry = new THREE.BufferGeometry().setFromPoints(samples.map((s) => place(s, new THREE.Vector3())));
    path.computeLineDistances();
    const at = (ms) => samples.reduce((a, s) => (Math.abs(s.ms - ms) < Math.abs(a.ms - ms) ? s : a));
    burns.forEach((mesh, k) => {
      const b = p.burns[k];
      mesh.visible = burnLabels[k].visible = !!b && samples.length > 0;
      if (!mesh.visible) return;
      burnLabels[k].position.copy(place(at(b.time_ms), mesh.position));
      burnLabels[k].setText(`${k + 1}  ${b.label} · ${b.dv_mps.toFixed(3)} m/s`);
    });
    nears.forEach((mesh, k) => {
      const c = (p.secondary.closest ?? [])[k];
      mesh.visible = nearLabels[k].visible = !!c && samples.length > 0;
      if (!mesh.visible) return;
      nearLabels[k].position.copy(place(at(c.time_ms), mesh.position));
      nearLabels[k].setText(`${c.name} passes ${km(c.min_km)} km away`);
    });
  }

  // ---------- suggestion over the simulation ----------
  function hudContent() {
    if (!event) return ['', ''];
    if (failure) return ['red', `<div class="title">Avoidance manoeuvre: planning failed</div>${escapeHtml(failure)}`];
    if (!result) return ['', '<span class="spinner"></span>Planning an avoidance manoeuvre and checking it against nearby objects…'];
    if (result.plans.length && result.plans.every((q) => q.not_needed)) {
      return ['', `<div class="title">✓ No manoeuvre needed</div>${escapeHtml(result.plans[0].reason)}`];
    }
    const p = plan();
    if (!p?.possible) {
      return ['red', `<div class="title">⚠ No safe avoidance manoeuvre: collision risk remains</div>
        ${escapeHtml(p?.reason ?? result.reason ?? '')}`];
    }
    const now = state.simTime;
    const steps = p.burns.map((b) => `<li class="${now >= b.time_ms ? 'done' : ''}"><b>${escapeHtml(b.label)}</b>
      ${b.dv_mps.toFixed(3)} m/s${b === p.burns[0] ? ` ${escapeHtml(p.direction_name)}` : ''} ·
      ${now >= b.time_ms ? 'done' : `in ${clock(b.time_ms - now)}`}</li>`).join('');
    const o = offset(p.manoeuvring_norad, now);
    const where = !flying() ? 'Not flown in the simulation (see the conjunction card).'
      : now < p.burns[0].time_ms ? 'On its original orbit, before the avoidance burn.'
        : now < p.back_on_orbit_ms && o ? `On the temporary path, <b>${km(Math.hypot(...o))} km</b> from where it would have been.`
          : 'Back on its original orbit.';
    const sec = p.secondary;
    const near = (sec.closest ?? [])[0];
    const nearText = near ? ` Nearest other object: ${escapeHtml(near.name)} at <b>${km(near.min_km)} km</b>` +
      `${Math.abs(near.min_km - near.was_km) > 0.05 ? ` (${km(near.was_km)} km without the manoeuvre)` : ' (the same without the manoeuvre)'}.` : '';
    return ['', `<button id="hudReplay">Replay</button>
      <div class="title">✓ Suggested manoeuvre: ${escapeHtml(p.manoeuvring)} avoids ${escapeHtml(p.other)}</div>
      <ol>${steps}</ol>
      ${where}<br>
      At closest approach: miss ${(p.miss_before_km * 1000).toFixed(0)} m → <b>${(p.miss_after_km * 1000).toFixed(0)} m</b>,
      Pc ${p.pc_before.toExponential(1)} → <b>${p.pc_after.toExponential(1)}</b> · total ${p.total_dv_mps.toFixed(3)} m/s<br>
      <span class="safe">✓ No new collision risk from the manoeuvre:</span> the whole detour was checked against
      ${sec.objects_checked.toLocaleString()} objects and comes within ${sec.threshold_km} km of none it was not already
      passing.${nearText}`];
  }
  function renderHud() {
    const [kind, html] = hudContent();
    if (html === hudHtml) return;
    hudHtml = html;
    hud.className = `card ${kind}`;
    hud.style.display = html ? 'block' : 'none';
    hud.innerHTML = html;
  }
  // Redrawn while the clock runs, so act on pointerdown rather than click.
  hud.addEventListener('pointerdown', (ev) => { if (ev.target.closest('#hudReplay')) replay(); });
  function update() {
    const now = performance.now();
    if (now - lastHud < 250) return;
    lastHud = now;
    renderHud();
  }

  function render() {
    const el = planEl();
    if (!result) { el.innerHTML = ''; return; }
    if (result.plans.length && result.plans.every((p) => p.not_needed)) {
      el.innerHTML = `<div class="check ok"><div class="title">✓ No manoeuvre needed</div>
        ${escapeHtml(result.plans[0].reason)}</div>`;
      return;
    }
    if (!result.plans.length || !result.possible) {
      const reasons = result.plans.length
        ? result.plans.map((p) => `<li>${escapeHtml(p.manoeuvring)}: ${escapeHtml(p.reason)}</li>`).join('')
        : `<li>${escapeHtml(result.reason)}</li>`;
      el.innerHTML = `<div class="check red"><div class="title">⚠ No safe avoidance manoeuvre found: collision risk remains</div>
        <ul class="small">${reasons}</ul></div>`;
      return;
    }
    const p = plan();
    const options = result.plans.map((q, k) => `<option value="${k}"${k === selected ? ' selected' : ''}>` +
      `${escapeHtml(q.manoeuvring)} manoeuvres${q.possible ? ` · ${q.total_dv_mps.toFixed(2)} m/s` : ' · not possible'}` +
      `${k === 0 ? ' (recommended)' : ''}</option>`).join('');
    const other = satByNorad.get(p.other_norad);
    const note = result.plans.length === 1 && other && ['Debris', 'Rocket body'].includes(other.group)
      ? `<div class="small muted">${escapeHtml(p.other)} is ${other.group.toLowerCase()} and cannot manoeuvre.</div>` : '';
    if (!p.possible) {
      el.innerHTML = `<select id="planSel" style="width:100%">${options}</select>
        <div class="check red"><div class="title">⚠ Not possible for ${escapeHtml(p.manoeuvring)}</div>${escapeHtml(p.reason)}</div>`;
    } else {
      const tca = result.tca_ms;
      const burnRows = p.burns.map((b, k) => `<tr><td><span class="dot" style="background:#${BURN_COLORS[k].toString(16)}"></span>
        ${escapeHtml(b.label)}</td><td>${formatOffset(b.time_ms - tca)}</td><td>${b.dv_mps.toFixed(3)} m/s</td></tr>`).join('');
      const sec = p.secondary;
      el.innerHTML = `
        ${result.plans.length > 1 ? `<select id="planSel" style="width:100%">${options}</select>` : ''}
        <div class="check ok">
          <div class="title">✓ Suggested manoeuvre for ${escapeHtml(p.manoeuvring)}</div>
          ${p.burns[0].dv_mps.toFixed(3)} m/s ${escapeHtml(p.direction_name)} burn ${formatOffset(p.burns[0].time_ms - tca)}.
          <table class="plan"><tr><th>Burn</th><th>When</th><th>Δv</th></tr>${burnRows}</table>
          <dl>
            <dt>Miss distance</dt><dd>${(p.miss_before_km * 1000).toFixed(0)} m → <b>${(p.miss_after_km * 1000).toFixed(0)} m</b></dd>
            <dt>Collision probability</dt><dd>${p.pc_before.toExponential(1)} → <b>${p.pc_after.toExponential(1)}</b></dd>
            <dt>Total Δv</dt><dd>${p.total_dv_mps.toFixed(3)} m/s (smallest that meets both limits)</dd>
            <dt>Propellant</dt><dd>${(p.propellant_kg * 1000).toFixed(1)} g, assuming ${p.assumed_mass_kg} kg and
              I<sub>sp</sub> ${p.assumed_isp_s} s</dd>
            <dt>Back on original orbit</dt><dd>${formatOffset(p.back_on_orbit_ms - tca)}</dd>
            <dt>Position on that orbit</dt><dd>${Math.abs(p.slot_offset_km) < 0.01 ? 'original slot'
              : `${Math.abs(p.slot_offset_km).toFixed(1)} km ${p.slot_offset_km > 0 ? 'ahead of' : 'behind'} original slot`}</dd>
          </dl>
          <div class="small">New path checked against ${sec.objects_checked.toLocaleString()} nearby objects:
            no new approach under ${sec.threshold_km} km.${p.rejected.length
              ? ` ${p.rejected.length} cheaper option(s) rejected because they created one.` : ''}</div>
          <label class="small"><input type="checkbox" id="planFly" checked> Fly the suggested path in the simulation</label>
          ${geometry(p).factor > 1 ? `<label class="small"><input type="checkbox" id="planBig" checked>
            Draw the detour's distance from the orbit ×${geometry(p).factor.toLocaleString()} so it can be seen</label>` : ''}
          <div class="small muted">Dashed line: temporary path from the avoidance burn until it rejoins the original orbit.</div>
        </div>${note}`;
    }
    const sel = $('planSel');
    if (sel) sel.onchange = () => { selected = Number(sel.value); render(); drawPath(); renderHud(); onPlanChange(); };
    const fly = $('planFly');
    if (fly) fly.onchange = () => { group.visible = fly.checked; renderHud(); onPlanChange(); };
    const big = $('planBig');
    if (big) big.onchange = () => { drawPath(); group.visible = flying(); onPlanChange(); };
  }

  // Rewind to just before the avoidance burn and play the manoeuvre.
  function replay() {
    if (!plan()?.possible) return;
    state.simTime = plan().burns[0].time_ms - 120000;
    setSpeed(60);
  }

  // rewind: also play the manoeuvre from its first burn once the plan is there.
  async function request(e, rewind = true) {
    if (e === event && result) {   // already planned
      if (rewind) replay();
      return;
    }
    event = e;
    result = failure = null;
    selected = 0;
    group.visible = false;
    renderHud();
    const id = ++requestId;
    planEl().innerHTML = '<div class="planning"><span class="spinner"></span>' +
      'Planning: searching burns and checking the new path against nearby objects…</div>';
    try {
      const res = await fetch(`api/maneuver?n1=${e.norad_1}&n2=${e.norad_2}&tca_ms=${e.tca_ms}` +
        `&not_before_ms=${Math.round(notBefore())}` + (DATASET ? `&dataset=${encodeURIComponent(DATASET)}` : ''));
      if (id !== requestId) return;
      if (res.status === 404) throw new Error('Start the app with ./run.sh (src/server.py) to plan manoeuvres.');
      const body = await res.json();
      if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`);
      result = body;
      render();
      drawPath();
      renderHud();
      onPlanChange();
      if (rewind) replay();
    } catch (err) {
      if (id !== requestId) return;
      failure = err.message;
      renderHud();
      planEl().innerHTML = `<div class="check yellow"><div class="title">Planning failed</div>${escapeHtml(err.message)}</div>`;
    }
  }

  function clear() {
    requestId++;
    event = result = failure = null;
    group.visible = false;
    planEl().innerHTML = '';
    renderHud();
  }

  return {
    request, clear, offset, drawnOffset, update,
    forEvent: (e) => e === event,
    summary: () => (plan()?.possible && flying() ? plan() : null),
    warning: () => (result && !result.possible),
  };
}
