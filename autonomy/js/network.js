// Radio links between satellites within range, and each satellite's colour by its state at the current time.

import * as THREE from 'three';
import { toScene } from '../../app/js/orbit.js';

const MAX_LINKS = 40000;
const COLORS = {
  radio: new THREE.Color('#4e7fd8'), link: new THREE.Color('#5ce1e6'), monitor: new THREE.Color('#ffd84d'),
  manoeuvre: new THREE.Color('#3ddc97'), warning: new THREE.Color('#ff4d4d'), silent: new THREE.Color('#5b6475'),
};

// Interpolated TEME offset (km) of a manoeuvring satellite from its SGP4 position (plan.path from the engine).
export function planOffset(plan, ms) {
  const { t0_ms: t0, step_ms: step, offset_km: off } = plan.path;
  const k = (ms - t0) / step;
  if (k <= 0 || k >= off.length - 1) return null;
  const i = Math.floor(k), f = k - i, a = off[i], b = off[i + 1];
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
}

// Status of each involved satellite at time ms, from the threat timeline.
export function threatStates(threats, ms) {
  const out = new Map();   // norad -> 'monitor' | 'manoeuvre' | 'warning'
  const rank = { monitor: 1, warning: 2, manoeuvre: 3 };
  const set = (n, s) => { if ((rank[s] ?? 0) > (rank[out.get(n)] ?? 0)) out.set(n, s); };
  for (const t of threats) {
    if (ms < t.detected_ms) continue;
    if (t.status === 'avoided') {
      const first = t.plan.burns[0].time_ms, back = t.plan.back_on_orbit_ms;
      if (ms >= first && ms <= back) set(t.manoeuvring, 'manoeuvre');
      else if (ms < first) { set(t.a, 'monitor'); set(t.b, 'monitor'); }
    } else if (ms <= t.tca_ms) {
      const s = t.status === 'warning' ? 'warning' : 'monitor';
      set(t.a, s); set(t.b, s);
    }
  }
  return out;
}

export function createNetwork({ scene, sats, radio, linkKm, layer }) {
  const positions = new Float32Array(MAX_LINKS * 6);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3).setUsage(THREE.DynamicDrawUsage));
  const lines = new THREE.LineSegments(geometry,
    new THREE.LineBasicMaterial({ color: COLORS.link, transparent: true, opacity: 0.65, depthWrite: false }));
  lines.frustumCulled = false;
  scene.add(lines);

  const n = sats.length;
  const km = new Float64Array(n * 3);          // current TEME positions (km), NaN when not shown
  const inContact = new Uint8Array(n);
  let pairs = [];                              // [i, j, distance km] of satellites in range now
  const v3 = new THREE.Vector3(), w3 = new THREE.Vector3();

  // Neighbours within linkKm among radio satellites: spatial hash with cells of linkKm.
  function findPairs(state) {
    const { latest, simTime } = state;
    const k = Math.max(-30, Math.min(30, (simTime - latest.time) / 1000));
    const p = latest.pos, v = latest.vel;
    const cells = new Map();
    const key = (x, y, z) => `${x},${y},${z}`;
    for (let i = 0; i < n; i++) {
      const j = i * 3;
      if (!radio[i] || Number.isNaN(p[j])) { km[j] = NaN; continue; }
      km[j] = p[j] + v[j] * k; km[j + 1] = p[j + 1] + v[j + 1] * k; km[j + 2] = p[j + 2] + v[j + 2] * k;
      const c = key(Math.floor(km[j] / linkKm), Math.floor(km[j + 1] / linkKm), Math.floor(km[j + 2] / linkKm));
      (cells.get(c) ?? cells.set(c, []).get(c)).push(i);
    }
    pairs = [];
    inContact.fill(0);
    const r2 = linkKm * linkKm;
    for (const [c, members] of cells) {
      const [cx, cy, cz] = c.split(',').map(Number);
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) for (let dz = -1; dz <= 1; dz++) {
        const other = cells.get(key(cx + dx, cy + dy, cz + dz));
        if (!other) continue;
        for (const i of members) for (const j of other) {
          if (j <= i) continue;
          const a = i * 3, b = j * 3;
          const d2 = (km[a] - km[b]) ** 2 + (km[a + 1] - km[b + 1]) ** 2 + (km[a + 2] - km[b + 2]) ** 2;
          if (d2 <= r2) { pairs.push([i, j, Math.sqrt(d2)]); inContact[i] = inContact[j] = 1; }
        }
      }
    }
    const shown = Math.min(pairs.length, MAX_LINKS);
    for (let q = 0; q < shown; q++) {
      const [i, j] = pairs[q];
      toScene(km[i * 3], km[i * 3 + 1], km[i * 3 + 2], v3);
      toScene(km[j * 3], km[j * 3 + 1], km[j * 3 + 2], w3);
      positions.set([v3.x, v3.y, v3.z, w3.x, w3.y, w3.z], q * 6);
    }
    geometry.setDrawRange(0, shown * 2);
    geometry.attributes.position.needsUpdate = true;
  }

  // Colour every point by its state (layer.points is the shared point cloud from ../app/js/satellites.js).
  function recolor(states) {
    const colors = layer.points.geometry.attributes.color.array;
    for (let i = 0; i < n; i++) {
      const s = states.get(sats[i].norad);
      const c = !radio[i] ? COLORS.silent
        : s === 'manoeuvre' ? COLORS.manoeuvre : s === 'warning' ? COLORS.warning
          : s === 'monitor' ? COLORS.monitor : inContact[i] ? COLORS.link : COLORS.radio;
      colors[i * 3] = c.r; colors[i * 3 + 1] = c.g; colors[i * 3 + 2] = c.b;
    }
    layer.points.geometry.attributes.color.needsUpdate = true;
  }

  return {
    update(state, states) {
      if (!state.latest) return;
      findPairs(state);
      recolor(states);
    },
    pairs: () => pairs,
    neighboursOf: (i) => pairs.filter(([a, b]) => a === i || b === i).map(([a, b, d]) => [a === i ? b : a, d]),
    position: (i) => (Number.isNaN(km[i * 3]) ? null : [km[i * 3], km[i * 3 + 1], km[i * 3 + 2]]),
  };
}
