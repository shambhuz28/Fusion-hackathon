// ML risk assessment and avoidance manoeuvres (from ml/assess_conjunctions.py): attaches the ML
// collision-risk probability, YES/NO decision and rank to each conjunction, and renders the
// manoeuvre box (direction + magnitude of the burn) shown when a conjunction is analysed.

import * as THREE from 'three';
import { toScene } from './orbit.js';

const sat = window.satellite;
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const key = (tca, n1, n2) => `${tca}:${n1}:${n2}`;
const utc = (ms) => new Date(ms).toISOString().slice(0, 19).replace('T', ' ');

export async function loadRiskAssessment() {
  try {
    const res = await fetch('data/risk_assessment.json');
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

// Adds ml_prob, ml_yes, ml_rank and maneuver to every event that the assessment covers.
export function attachRisk(events, risk) {
  const rows = new Map(risk.rows.map((r) => {
    const o = Object.fromEntries(risk.fields.map((f, k) => [f, r[k]]));
    return [key(o.tca_ms, o.norad_1, o.norad_2), o];
  }));
  let matched = 0;
  for (const e of events) {
    const k = key(e.tca_ms, e.norad_1, e.norad_2);
    const r = rows.get(k);
    if (!r) continue;
    matched++;
    e.ml_prob = r.ml_prob;
    e.ml_yes = r.ml_yes === 1;
    e.ml_rank = r.rank;
    e.maneuver = risk.maneuvers[k] ?? null;
  }
  return matched;
}

// Burn speeds: cm/s below 1 m/s.
export const formatDv = (m_s) => (m_s < 1 ? `${(m_s * 100).toFixed(1)} cm/s` : `${m_s.toFixed(2)} m/s`);

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

function component(value, pos, neg) {
  return value === 0 ? '0' : `${formatDv(Math.abs(value))} ${value > 0 ? pos : neg}`;
}

// HTML for the manoeuvre box of event e; nameOf(norad) gives object names.
export function maneuverHtml(e, nameOf, simTime, meta) {
  if (e.ml_prob == null) return '';
  const pcHigh = (meta?.model?.high_risk_pc ?? 1e-6).toExponential(0);
  if (!e.ml_yes) {
    return `<div class="man-box none"><div class="man-title">No manoeuvre needed</div>
      The ML model predicts this conjunction stays below the high-risk level (Pc ${pcHigh}).</div>`;
  }
  const m = e.maneuver;
  if (!m) {
    return `<div class="man-box none"><div class="man-title">Manoeuvre not computed</div>
      Re-run ml/assess_conjunctions.py without --max-plans.</div>`;
  }
  if (m.status === 'not_needed') {
    return `<div class="man-box none"><div class="man-title">No burn needed</div>${escapeHtml(m.reason)}.</div>`;
  }
  const conflicts = (m.conflicts ?? []).map((c) =>
    `<li>${escapeHtml(c.name)} · ${c.miss_km.toFixed(2)} km · Pc ${c.pc.toExponential(1)} at ${utc(c.tca_ms).slice(11)}</li>`).join('');
  if (m.status === 'warning') {
    return `<div class="man-box warn"><div class="man-title">⚠ COLLISION WARNING</div>
      ${escapeHtml(m.reason)}.${m.dv_m_s ? ` Smallest burn: ${formatDv(m.dv_m_s)} ${escapeHtml(m.direction)} by
      ${escapeHtml(m.maneuver_name)}.` : ''}
      ${conflicts ? `<div class="small" style="margin-top:4px">It would approach:</div><ul class="man-conf">${conflicts}</ul>` : ''}</div>`;
  }
  const [r, t, n] = m.dv_rtn_m_s;
  const toBurn = m.burn_ms - simTime;
  const when = Math.abs(toBurn) < 1000 ? 'now' : toBurn > 0 ? `in ${fmtDur(toBurn)}` : `${fmtDur(-toBurn)} ago`;
  return `<div class="man-box ok">
    <div class="man-title">Avoidance manoeuvre · ${escapeHtml(m.maneuver_name)}</div>
    <div class="man-main">${arrowSvg(m.dv_rtn_m_s)}
      <div><div class="man-dv">${formatDv(m.dv_m_s)}</div><div class="man-dir">${escapeHtml(m.direction)}</div>
      <div class="muted small">burn ${when}</div>
      <button id="manGo" class="man-go">Go to burn ▸</button></div></div>
    <dl class="man-dl">
      <dt>Burn at (UTC)</dt><dd>${utc(m.burn_ms)}</dd>
      <dt>Before TCA</dt><dd>${Math.round(m.lead_min)} min (${m.lead_orbits} orbit${m.lead_orbits === 1 ? '' : 's'})</dd>
      <dt>Forward / back</dt><dd>${component(t, 'forward', 'back')}</dd>
      <dt>Left / right</dt><dd>${component(n, 'left', 'right')}</dd>
      <dt>Up / down</dt><dd>${component(r, 'up', 'down')}</dd>
      <dt>Miss distance</dt><dd>${m.before ? m.before.miss_km.toFixed(3) : '?'} → <b>${m.after.miss_km.toFixed(3)} km</b></dd>
      <dt>Pc</dt><dd>${m.before ? m.before.pc.toExponential(1) : '?'} → <b>${m.after.pc.toExponential(1)}</b></dd>
      <dt>Other objects</dt><dd class="man-safe">✓ none brought close, checked to ${utc(m.checked_until_ms).slice(11, 16)} UTC</dd>
    </dl>
    ${m.rejected_burns ? `<div class="muted small">${m.rejected_burns} smaller burn${m.rejected_burns > 1 ? 's' : ''} rejected: they approached other objects.</div>` : ''}
  </div>`;
}

function fmtDur(ms) {
  let s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const pad = (x) => String(x).padStart(2, '0');
  return `${d ? `${d}d ` : ''}${pad(h)}:${pad(m)}:${pad(s)}`;
}

// 3D marker: where the manoeuvring satellite fires, with an arrow along the burn direction.
export function createBurnMarker(scene) {
  const group = new THREE.Group();
  const dot = new THREE.Mesh(new THREE.SphereGeometry(0.06, 14, 10), new THREE.MeshBasicMaterial({ color: 0x3ddc97 }));
  const arrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(), 0.9, 0x3ddc97, 0.22, 0.12);
  group.add(dot, arrow);
  group.visible = false;
  scene.add(group);
  const p = new THREE.Vector3(), d = new THREE.Vector3();
  return {
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
    position: p,
  };
}
