// Entry point of the crosslink simulation: loads the satellites (shared with the main app) and the network run
// (autonomy/engine.py), and plays it: radio links, satellite states, feed, threats and avoidance detours.

import * as THREE from 'three';
import { createScene } from '../../app/js/scene.js';
import { createSatelliteLayer } from '../../app/js/satellites.js';
import { startPropagator } from '../../app/js/propagator.js';
import { toScene } from '../../app/js/orbit.js';
import { notify } from '../../app/js/notice.js';
import { createNetwork, planOffset, threatStates } from './network.js';
import { createFocus } from './focus.js';
import { createPanels } from './panels.js';

const $ = (id) => document.getElementById(id);
const SPEEDS = [[0, 'Pause'], [1, '1×'], [10, '10×'], [60, '60×'], [300, '300×'], [1000, '1000×']];
const EXAGGERATION = 300;

async function loadJson(url, missing) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(missing);
  return res.json();
}

const [objects, run] = await Promise.all([
  loadJson('../app/data/leo_objects.json', 'app/data/leo_objects.json is missing: run ./run.sh in the project first.'),
  loadJson('data/run.json', 'autonomy/data/run.json is missing: run python3 autonomy/engine.py first.'),
]).catch((err) => { $('loading').textContent = err.message; throw err; });

const sats = objects.objects.map(([group, values, validFrom, validTo]) => {
  const omm = Object.fromEntries(objects.omm_fields.map((f, k) => [f, values[k]]));
  return { norad: omm.NORAD_CAT_ID, name: omm.OBJECT_NAME, group, omm, validFrom, validTo };
});
const satByNorad = new Map(sats.map((s) => [s.norad, s]));
const indexOf = new Map(sats.map((s, i) => [s.norad, i]));
const radioSet = new Set(run.satellites.rows.map((r) => r[0]));
const radio = Uint8Array.from(sats, (s) => (radioSet.has(s.norad) ? 1 : 0));
const threats = run.threats;

const lastEvent = Math.max(run.end_ms, ...run.feed.map((f) => f.t_ms));
const range = [run.start_ms, lastEvent + 10 * 60000];
const state = { simTime: run.start_ms, speed: 10, latest: null, colorMode: 'group',
  visibleGroups: new Set(sats.map((s) => s.group)) };

$('subtitle').textContent = `${run.summary.radio_satellites.toLocaleString()} satellites with radio + ` +
  `${run.summary.silent_objects.toLocaleString()} silent objects · ${((run.end_ms - run.start_ms) / 3600000).toFixed(0)} h from ` +
  `${new Date(run.start_ms).toISOString().slice(0, 16).replace('T', ' ')} UTC`;
$('rangeText').textContent = `${run.config.link_km} km (a ${run.config.link_km * 2} km bubble)`;
$('horizonText').textContent = `${run.config.horizon_h} h`;
const c = run.summary.coverage;
$('coverage').innerHTML = `<b>Coverage check:</b> of ${c.critical_pairs} critical encounters between radio satellites in this
  window (from the ground screening), the network detected <b>${c.detected_in_time}</b> at least 10 min ahead.
  ${c.too_late} pairs first came into range only during the approach itself (seconds before, at 7-15 km/s),
  ${c.never_in_range} never came within range, and ${c.discarded_onboard} were judged safe with the precise
  onboard data. Avoidance works when two satellites have met on an earlier orbit.`;

// --- scene
const { renderer, scene, camera, controls, earth } = createScene($('scene'));
const layer = createSatelliteLayer(sats);
scene.add(layer.points);
layer.updateColors(state);
const network = createNetwork({ scene, sats, radio, linkKm: run.config.link_km, layer });
const focus = createFocus({ scene, camera, satByNorad });

let selected = null;   // { type: 'threat', t } | { type: 'sat', i }
const panels = createPanels({
  run, sats, satByNorad,
  onSelectThreat: (t) => selectThreat(t),
  onJump: (ms) => { jumpTo(ms); setSpeed(10); },
});

function jumpTo(ms) {
  state.simTime = Math.min(range[1], Math.max(range[0], ms));
}

function selectThreat(t) {
  selected = { type: 'threat', t };
  focus.show(t, $('exaggerate').checked ? EXAGGERATION : 1);
  if (state.simTime < t.detected_ms - 60000 || state.simTime > t.tca_ms + 30 * 60000) jumpTo(t.detected_ms - 20000);
  $('detail').style.display = 'block';
  $('exaggerateLabel').style.display = 'block';
  lastDetail = 0;
}

function selectSatellite(i) {
  selected = { type: 'sat', i };
  focus.hide();
  $('detail').style.display = 'block';
  $('exaggerateLabel').style.display = 'none';
  lastDetail = 0;
}

$('detailClose').onclick = () => { selected = null; focus.hide(); $('detail').style.display = 'none'; };
$('exaggerate').onchange = (e) => focus.setScale(e.target.checked ? EXAGGERATION : 1);
$('showSilent').onchange = (e) => {
  for (const g of ['Debris', 'Rocket body']) e.target.checked ? state.visibleGroups.add(g) : state.visibleGroups.delete(g);
};

// --- time controls
const speedsEl = $('speeds');
for (const [value, label] of SPEEDS) {
  const b = document.createElement('button');
  b.textContent = label;
  b.dataset.speed = value;
  b.onclick = () => setSpeed(value);
  speedsEl.appendChild(b);
}
function setSpeed(v) {
  state.speed = v;
  for (const b of speedsEl.children) b.classList.toggle('active', Number(b.dataset.speed) === v);
}
setSpeed(state.speed);
$('toStart').onclick = () => jumpTo(range[0]);
$('nextThreat').onclick = () => {
  // Threats are in detection order: the one after the selected threat, else the first one after the clock.
  const current = selected?.type === 'threat' ? selected.t : null;
  const next = (current ? threats.find((t) => t.id > current.id) : threats.find((t) => t.detected_ms > state.simTime))
    ?? threats[0];
  if (!next) { notify('No threats in this run.'); return; }
  selectThreat(next);
  jumpTo(next.detected_ms - 20000);
  setSpeed(10);
};

// --- click a satellite: nearest visible dot on screen, not hidden behind the Earth
const raycaster = new THREE.Raycaster();
const earthSphere = new THREE.Sphere(new THREE.Vector3(), 6.378137);
let downAt = null;
renderer.domElement.addEventListener('pointerdown', (e) => { downAt = [e.clientX, e.clientY]; });
renderer.domElement.addEventListener('pointerup', (e) => {
  if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 5) return;
  const pos = layer.points.geometry.attributes.position.array;
  const v = new THREE.Vector3();
  let best = -1, bestD = 12;
  for (let i = 0; i < sats.length; i++) {
    v.set(pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2]);
    if (v.lengthSq() < 1) continue;   // hidden
    const p = v.clone().project(camera);
    if (p.z > 1) continue;
    const d = Math.hypot((p.x + 1) / 2 * innerWidth - e.clientX, (1 - p.y) / 2 * innerHeight - e.clientY);
    if (d < bestD) {
      raycaster.set(camera.position, v.clone().sub(camera.position).normalize());
      const hit = raycaster.ray.intersectSphere(earthSphere, new THREE.Vector3());
      if (hit && hit.distanceTo(camera.position) < v.distanceTo(camera.position)) continue;
      best = i; bestD = d;
    }
  }
  if (best >= 0) selectSatellite(best);
});

// --- propagation (the main app's SGP4 worker) and the loop
startPropagator(sats, state, { onReady: () => $('loading').remove(), onState: () => {} });

const s3 = new THREE.Vector3();
let lastFrame = performance.now(), lastNet = 0, lastPanels = 0, lastDetail = 0, states = new Map(), distanceKm = null;

// Satellites on a detour: move their dots by the planned offset (km-scale, visible when zoomed in).
function applyDetours(pos) {
  for (const t of threats) {
    if (t.status !== 'avoided' || state.simTime < t.plan.burns[0].time_ms || state.simTime > t.plan.back_on_orbit_ms) continue;
    const o = planOffset(t.plan, state.simTime);
    const i = indexOf.get(t.manoeuvring);
    if (!o || i === undefined || (pos[i * 3] === 0 && pos[i * 3 + 1] === 0)) continue;
    toScene(o[0], o[1], o[2], s3);
    pos[i * 3] += s3.x; pos[i * 3 + 1] += s3.y; pos[i * 3 + 2] += s3.z;
  }
}

function frame(now) {
  requestAnimationFrame(frame);
  const prev = state.simTime;
  // The first frame's timestamp can precede the script's start: never step backwards.
  state.simTime += Math.max(0, Math.min(now - lastFrame, 250)) * state.speed;
  lastFrame = now;
  if (state.simTime > range[1]) {
    jumpTo(state.simTime);
    if (state.speed) { setSpeed(0); notify('End of the simulated window.'); }
  } else if (state.simTime < range[0]) {
    jumpTo(state.simTime);
  }
  // Pause at the closest approach of the threat being watched (on the detour: its shifted closest approach).
  const t = selected?.type === 'threat' ? selected.t : null;
  const tca = t ? (t.plan?.tca_after_ms ?? t.tca_ms) : 0;
  if (t && prev < tca && state.simTime >= tca && state.speed > 0) { state.simTime = tca; setSpeed(0); }

  try {
    $('clock').textContent = new Date(state.simTime).toISOString().slice(0, 19).replace('T', ' ') + ' UTC';
    earth.rotation.y = window.satellite.gstime(new Date(state.simTime));
    layer.updatePositions(state);
    applyDetours(layer.points.geometry.attributes.position.array);
    if (now - lastNet > 250 && state.latest) {
      lastNet = now;
      states = threatStates(threats, state.simTime);
      network.update(state, states);
    }
    distanceKm = focus.update(state.simTime, now);
    if (now - lastPanels > 300) {
      lastPanels = now;
      panels.update(state.simTime, network.pairs().length, t?.id);
    }
    if (selected && now - lastDetail > 500) {
      lastDetail = now;
      $('detailBody').innerHTML = selected.type === 'threat'
        ? panels.threatDetail(selected.t, state.simTime, distanceKm)
        : panels.satelliteDetail(sats[selected.i], selected.i, state.simTime, radio[selected.i], network.neighboursOf(selected.i));
    }
  } catch (err) {
    console.error(err);
  }
  controls.update();
  renderer.render(scene, camera);
}
requestAnimationFrame(frame);
