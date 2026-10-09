// Avoidance manoeuvre for the analysed conjunction: asks src/server.py (/api/maneuver) for plans, shows
// the details, draws the suggested path with its burns, and lets the manoeuvring satellite fly it.

import * as THREE from 'three';
import { toScene } from './orbit.js';
import { DATASET, makeSatrec } from './data.js';

const sat = window.satellite;
const $ = (id) => document.getElementById(id);
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const PATH_COLOR = 0x3ddc97;
const BURN_COLORS = [0x3ddc97, 0x5ce1e6, 0x5ce1e6];

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
  const group = new THREE.Group();
  group.add(path, ...burns);
  group.visible = false;
  scene.add(group);

  let event = null;        // the conjunction the plans belong to
  let result = null;       // server response
  let selected = 0;        // index into result.plans
  let requestId = 0;

  const planEl = () => $('conjPlan');
  const plan = () => (result?.plans ?? [])[selected];
  const flying = () => plan()?.possible && $('planFly')?.checked !== false;

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

  function drawPath() {
    const p = plan();
    group.visible = !!p?.possible;
    if (!p?.possible) return;
    const rec = makeSatrec(satByNorad.get(p.manoeuvring_norad));
    const { t0_ms: t0, step_ms: step, offset_km: off } = p.path;
    const pts = [];
    off.forEach((o, k) => {
      const pos = sat.propagate(rec, new Date(t0 + k * step))?.position;
      if (pos) pts.push(toScene(pos.x + o[0], pos.y + o[1], pos.z + o[2], new THREE.Vector3()));
    });
    path.geometry.dispose();
    path.geometry = new THREE.BufferGeometry().setFromPoints(pts);
    path.computeLineDistances();
    p.burns.forEach((b, k) => {
      const kk = Math.min(off.length - 1, Math.max(0, Math.round((b.time_ms - t0) / step)));
      const pos = sat.propagate(rec, new Date(b.time_ms))?.position;
      if (pos) toScene(pos.x + off[kk][0], pos.y + off[kk][1], pos.z + off[kk][2], burns[k].position);
    });
  }

  function render() {
    const el = planEl();
    if (!result) { el.innerHTML = ''; return; }
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
            <dt>Total Δv</dt><dd>${p.total_dv_mps.toFixed(3)} m/s</dd>
            <dt>Back on original orbit</dt><dd>${formatOffset(p.back_on_orbit_ms - tca)}</dd>
            <dt>Position on that orbit</dt><dd>${Math.abs(p.slot_offset_km) < 0.01 ? 'original slot'
              : `${Math.abs(p.slot_offset_km).toFixed(1)} km ${p.slot_offset_km > 0 ? 'ahead of' : 'behind'} original slot`}</dd>
          </dl>
          <div class="small">New path checked against ${sec.objects_checked.toLocaleString()} nearby objects:
            no new approach under ${sec.threshold_km} km.${p.rejected.length
              ? ` ${p.rejected.length} cheaper option(s) rejected because they created one.` : ''}</div>
          <label class="small"><input type="checkbox" id="planFly" checked> Fly the suggested path in the simulation</label>
        </div>${note}`;
    }
    const sel = $('planSel');
    if (sel) sel.onchange = () => { selected = Number(sel.value); render(); drawPath(); onPlanChange(); };
    const fly = $('planFly');
    if (fly) fly.onchange = () => { group.visible = fly.checked; onPlanChange(); };
  }

  async function request(e) {
    event = e;
    result = null;
    selected = 0;
    group.visible = false;
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
      onPlanChange();
      if (plan()?.possible) {
        state.simTime = plan().burns[0].time_ms - 120000;
        setSpeed(60);
      }
    } catch (err) {
      if (id !== requestId) return;
      planEl().innerHTML = `<div class="check yellow"><div class="title">Planning failed</div>${escapeHtml(err.message)}</div>`;
    }
  }

  function clear() {
    requestId++;
    event = result = null;
    group.visible = false;
    planEl().innerHTML = '';
  }

  return {
    request, clear, offset,
    forEvent: (e) => e === event,
    summary: () => (plan()?.possible && flying() ? plan() : null),
    warning: () => (result && !result.possible),
  };
}
