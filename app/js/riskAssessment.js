// ML risk assessment (from ml/assess_conjunctions.py): attaches the ML collision-risk probability,
// YES/NO decision, rank and precomputed avoidance burn to each conjunction, and renders the compact
// manoeuvre box at the top of the analysis card: burn direction (arrow; forward/back, left/right,
// up/down as seen from the satellite facing its direction of travel) and magnitude.
//
// The box shows the detailed plan from src/server.py (maneuverView.js) when there is one, and the
// burn precomputed by the ML pipeline otherwise (e.g. when the app is served without src/server.py).

import * as THREE from 'three';
import { toScene } from './orbit.js';
import { dataUrl } from './data.js';

const sat = window.satellite;
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const key = (tca, n1, n2) => `${tca}:${n1}:${n2}`;
const utc = (ms) => new Date(ms).toISOString().slice(0, 19).replace('T', ' ');

export async function loadRiskAssessment() {
  try {
    const res = await fetch(dataUrl('risk_assessment.json'));
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

// Adds ml_prob, ml_yes, ml_rank and ml_plan to every event the assessment covers; returns how many matched.
export function attachRisk(events, risk) {
  if (!risk) return 0;
  const rows = new Map(risk.rows.map((r) => {
    const o = Object.fromEntries(risk.fields.map((f, k) => [f, r[k]]));
    return [key(o.tca_ms, o.norad_1, o.norad_2), o];
  }));
  let matched = 0;
  for (const e of events) {
    const k = key(e.tca_ms, e.norad_1, e.norad_2);
    const r = rows.get(k);
    if (!r || r.ml_prob == null) continue;
    matched++;
    e.ml_prob = r.ml_prob;
    e.ml_yes = r.ml_yes === 1;
    e.ml_rank = r.rank;
    e.ml_plan = risk.maneuvers[k] ?? null;
  }
  return matched;
}

// Burn speeds: cm/s below 1 m/s.
export const formatDv = (m_s) => (m_s < 1 ? `${(m_s * 100).toFixed(1)} cm/s` : `${m_s.toFixed(2)} m/s`);

// Main direction words of a burn [radial, along-track, cross-track] (m/s): components >= 25 % of the total.
export function directionWords([r, t, n]) {
  const total = Math.hypot(r, t, n) || 1;
  return [[t, 'Forward', 'Backward'], [n, 'Left', 'Right'], [r, 'Up', 'Down']]
    .filter(([v]) => Math.abs(v) >= 0.25 * total)
    .sort((a, b) => Math.abs(b[0]) - Math.abs(a[0]))
    .map(([v, pos, neg]) => (v >= 0 ? pos : neg)).join(' + ');
}

// Top view as seen from the satellite facing its direction of travel: forward up, left to the left.
function arrowSvg([r, t, n]) {
  const h = Math.hypot(t, n), total = Math.hypot(r, t, n);
  const size = 76, c = size / 2, len = 25;
  let arrow = '';
  if (h > 0.15 * total) {
    const x = c + (-n / h) * len, y = c + (-t / h) * len;
    arrow = `<line x1="${c}" y1="${c}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" class="man-arrow" marker-end="url(#manHead)"/>`;
  }
  const vertical = Math.abs(r) > 0.15 * total
    ? `<text x="${size - 4}" y="${size - 4}" text-anchor="end" class="man-vert">${r > 0 ? '▲ up' : '▼ down'}</text>` : '';
  return `<svg class="man-svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true">
    <defs><marker id="manHead" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto">
      <path d="M0,0 L7,3.5 L0,7 z" class="man-head"/></marker></defs>
    <circle cx="${c}" cy="${c}" r="${len + 4}" class="man-ring"/>
    <line x1="${c}" y1="${c - len - 4}" x2="${c}" y2="${c + len + 4}" class="man-cross"/>
    <line x1="${c - len - 4}" y1="${c}" x2="${c + len + 4}" y2="${c}" class="man-cross"/>
    <text x="${c}" y="8" text-anchor="middle" class="man-lbl">F</text>
    <text x="${c}" y="${size - 1}" text-anchor="middle" class="man-lbl">B</text>
    <text x="3" y="${c + 3}" class="man-lbl">L</text>
    <text x="${size - 3}" y="${c + 3}" text-anchor="end" class="man-lbl">R</text>
    ${arrow}<circle cx="${c}" cy="${c}" r="2.5" class="man-dot"/>${vertical}</svg>`;
}

const component = (value, pos, neg) => (value === 0 ? '0' : `${formatDv(Math.abs(value))} ${value > 0 ? pos : neg}`);

function fmtDur(ms) {
  let s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const pad = (x) => String(x).padStart(2, '0');
  return `${d ? `${d}d ` : ''}${pad(h)}:${pad(m)}:${pad(s)}`;
}
const burnWhen = (burnMs, simTime) => {
  const dt = burnMs - simTime;
  return Math.abs(dt) < 1000 ? 'burn now' : dt > 0 ? `burn in ${fmtDur(dt)}` : `burned ${fmtDur(-dt)} ago`;
};

// Arrow, magnitude and direction of one burn; `extra` goes under the direction.
function header(dvRtn, dvMps, extra) {
  return `<div class="man-main">${arrowSvg(dvRtn)}
    <div><div class="man-dv">${formatDv(dvMps)}</div><div class="man-dir">${escapeHtml(directionWords(dvRtn))}</div>${extra}</div></div>`;
}

// The compact box for event e. serverPlan: the possible plan shown by maneuverView.js (or null);
// serverDone: the server has answered for this event (with or without a possible plan).
export function maneuverBoxHtml(e, { serverPlan, serverDone, simTime, risk }) {
  if (serverPlan) {
    const b = serverPlan.burns[0];
    return `<div class="man-box ok">
      <div class="man-title">Avoidance burn · ${escapeHtml(serverPlan.manoeuvring)}</div>
      ${header(b.dv_rtn_mps, b.dv_mps, `<div class="muted small">${burnWhen(b.time_ms, simTime)} ·
        ${Math.round((e.tca_ms - b.time_ms) / 60000)} min before TCA</div>`)}
      <div class="muted small">First burn of the detailed plan below (return to orbit included).</div></div>`;
  }
  if (serverDone || e.ml_prob == null) return '';   // the server's own result is shown below
  const pcHigh = (risk?.model?.high_risk_pc ?? 1e-6).toExponential(0);
  if (!e.ml_yes) {
    return `<div class="man-box none">ML: this conjunction is predicted to stay below Pc ${pcHigh}; no burn needed.</div>`;
  }
  const m = e.ml_plan;
  if (!m) return '';
  if (m.status === 'not_needed') return `<div class="man-box none">${escapeHtml(m.reason)}.</div>`;
  if (m.status === 'warning') {
    const conflicts = (m.conflicts ?? []).map((c) =>
      `<li>${escapeHtml(c.name)} · ${c.miss_km.toFixed(2)} km · Pc ${c.pc.toExponential(1)} at ${utc(c.tca_ms).slice(11)}</li>`).join('');
    return `<div class="man-box warn"><div class="man-title">⚠ COLLISION WARNING</div>
      ${escapeHtml(m.reason)}.${m.dv_m_s ? ` Smallest burn: ${formatDv(m.dv_m_s)} ${escapeHtml(m.direction)} by
      ${escapeHtml(m.maneuver_name)}.` : ''}
      ${conflicts ? `<div class="small" style="margin-top:4px">It would approach:</div><ul class="man-conf">${conflicts}</ul>` : ''}</div>`;
  }
  const [r, t, n] = m.dv_rtn_m_s;
  return `<div class="man-box ok">
    <div class="man-title">Avoidance burn · ${escapeHtml(m.maneuver_name)} <span class="muted small">(precomputed)</span></div>
    ${header(m.dv_rtn_m_s, m.dv_m_s, `<div class="muted small">${burnWhen(m.burn_ms, simTime)}</div>
      <button id="manGo" class="man-go">Go to burn ▸</button>`)}
    <dl class="man-dl">
      <dt>Burn at (UTC)</dt><dd>${utc(m.burn_ms)}</dd>
      <dt>Before TCA</dt><dd>${Math.round(m.lead_min)} min (${m.lead_orbits} orbit${m.lead_orbits === 1 ? '' : 's'})</dd>
      <dt>Forward / back</dt><dd>${component(t, 'forward', 'back')}</dd>
      <dt>Left / right</dt><dd>${component(n, 'left', 'right')}</dd>
      <dt>Up / down</dt><dd>${component(r, 'up', 'down')}</dd>
      <dt>Miss distance</dt><dd>${m.before ? m.before.miss_km.toFixed(3) : '?'} → <b>${m.after.miss_km.toFixed(3)} km</b></dd>
      <dt>Pc</dt><dd>${m.before ? m.before.pc.toExponential(1) : '?'} → <b>${m.after.pc.toExponential(1)}</b></dd>
      <dt>Other objects</dt><dd class="man-safe">✓ none brought close, checked to ${utc(m.checked_until_ms).slice(11, 16)} UTC</dd>
    </dl></div>`;
}

// 3D marker for a precomputed burn: where the satellite fires, with an arrow along the burn direction.
export function createBurnMarker(scene) {
  const group = new THREE.Group();
  const dot = new THREE.Mesh(new THREE.SphereGeometry(0.06, 14, 10), new THREE.MeshBasicMaterial({ color: 0x3ddc97 }));
  const arrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(), 0.9, 0x3ddc97, 0.22, 0.12);
  group.add(dot, arrow);
  group.visible = false;
  scene.add(group);
  const p = new THREE.Vector3(), d = new THREE.Vector3();
  return {
    position: p,
    show(rec, m) {
      const pv = sat.propagate(rec, new Date(m.burn_ms));
      if (!pv?.position) { group.visible = false; return; }
      const { position: r, velocity: v } = pv;
      const rv = [r.x, r.y, r.z], vv = [v.x, v.y, v.z];
      const norm = (a) => { const l = Math.hypot(...a); return a.map((x) => x / l); };
      const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
      const R = norm(rv), N = norm(cross(rv, vv)), T = cross(N, R);
      const [dr, dt, dn] = m.dv_rtn_m_s;
      const dir = [0, 1, 2].map((k) => R[k] * dr + T[k] * dt + N[k] * dn);
      toScene(r.x, r.y, r.z, p);
      toScene(dir[0], dir[1], dir[2], d).normalize();
      dot.position.copy(p);
      arrow.position.copy(p);
      arrow.setDirection(d);
      group.visible = true;
    },
    hide() { group.visible = false; },
  };
}
