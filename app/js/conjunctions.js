// Conjunction screening results (from src/screen_conjunctions.py): the critical-events panel with
// live countdowns, and an analysis mode that shows both objects, their orbits, the TCA point and
// their live separation.

import * as THREE from 'three';
import { createOrbitPath } from './orbitPath.js';
import { toScene } from './orbit.js';
import { makeSatrec } from './data.js';
import { attachRisk, createBurnMarker, maneuverHtml } from './riskAssessment.js';

const sat = window.satellite;
const COLOR_A = 0xffd84d;
const COLOR_B = 0xff4d4d;
const MAX_ROWS = 100;
const LEAD_MS = 10 * 60000;   // analysis starts 10 minutes before TCA
const $ = (id) => document.getElementById(id);

export async function loadConjunctions() {
  try {
    const res = await fetch('data/conjunctions.json');
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

export function formatDuration(ms) {
  const sign = ms < 0 ? '−' : '';
  let s = Math.floor(Math.abs(ms) / 1000);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const pad = (x) => String(x).padStart(2, '0');
  return `${sign}${d ? `${d}d ` : ''}${pad(h)}:${pad(m)}:${pad(s)}`;
}

const formatPc = (pc) => (pc === 0 ? '0' : pc.toExponential(1));
const escapeHtml = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const sameConstellation = (e) => e.group_1 === e.group_2 && e.group_1 !== 'Other' && e.group_1 !== 'Debris';
const satelliteVsDebris = (e) => e.debris && !(e.group_1 === 'Debris' && e.group_2 === 'Debris');
const involves = (e, norad) => e.norad_1 === norad || e.norad_2 === norad;
const RISK_ORDER = { red: 0, yellow: 1, green: 2 };
const DAY_MS = 86400000;
const shortDate = (ms) => new Date(ms).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });

export function createConjunctionView({ data, risk, scene, camera, sats, state, setSpeed, onAnalyze }) {
  // Rows are compact arrays; names and groups come from the satellite list.
  const satByNorad = new Map(sats.map((s) => [s.norad, s]));
  const events = data.rows
    .map((row) => Object.fromEntries(data.fields.map((f, k) => [f, row[k]])))
    .filter((e) => satByNorad.has(e.norad_1) && satByNorad.has(e.norad_2))
    .sort((a, b) => a.tca_ms - b.tca_ms);
  events.forEach((e, idx) => {
    const a = satByNorad.get(e.norad_1), b = satByNorad.get(e.norad_2);
    Object.assign(e, { idx, name_1: a.name, group_1: a.group, name_2: b.name, group_2: b.group,
      tca_utc: new Date(e.tca_ms).toISOString() });
  });
  // ML collision-risk probability, decision and avoidance manoeuvres (ml/assess_conjunctions.py).
  const hasRisk = Boolean(risk) && attachRisk(events, risk) > 0;
  const windowStartMs = Date.parse(data.window_start_utc);
  const windowEndMs = windowStartMs + data.window_hours * 3600000;

  // ---------- panel ----------
  $('conj').style.display = 'block';
  const c = data.counts ?? {};
  $('conjSummary').textContent =
    `${data.n_objects.toLocaleString()} objects screened for ` +
    `${data.window_hours >= 48 ? `${data.window_hours / 24} days` : `${data.window_hours} h`} from ` +
    `${data.window_start_utc.slice(0, 16).replace('T', ' ')} UTC · ` +
    `${((c.red ?? 0) + (c.yellow ?? 0) + (c.green ?? 0)).toLocaleString()} approaches < ${data.screen_km} km ` +
    `(${(c.red ?? 0).toLocaleString()} critical, ${(c.yellow ?? 0).toLocaleString()} watch)`;
  $('conjNote').textContent = (risk ? `ML: ${risk.model.name} trained on ESA's Kelvins CDM data; YES = predicted to end ` +
    `with Pc ≥ ${risk.model.high_risk_pc.toExponential(0)}. Burns bring Pc below ${risk.maneuver.pc_target.toExponential(0)} ` +
    `and are checked against every object. ` : '') +
    `Pc uses an assumed covariance (TLEs have none) that grows with element age, and a ${data.hbr_m} m ` +
    `combined hard-body radius.` + (data.intra_cutoff_hours
      ? ` Same-constellation pairs (e.g. Starlink–Starlink) are shown for the first ${data.intra_cutoff_hours} h ` +
        `only: they manoeuvre to keep their spacing, which TLEs cannot predict further ahead.`
      : '');

  const filterEl = $('conjFilter'), sortEl = $('conjSort'), hideSameEl = $('conjHideSame'), listEl = $('conjList');
  const rangeEl = $('conjRange');
  if (hasRisk) {
    filterEl.value = 'ml';
    sortEl.value = 'ml';
  } else {
    for (const el of [filterEl, sortEl]) el.querySelector('option[value="ml"]')?.remove();
  }
  for (const el of [filterEl, sortEl, hideSameEl, rangeEl]) el.onchange = () => { lastIds = ''; renderDays(); renderList(); };

  // A focused satellite (selected by the user) overrides the risk filter: all its events are listed.
  let focus = null;
  const matchesFilter = (e) => {
    if (focus) return involves(e, focus.norad);
    if (filterEl.value === 'debris') return satelliteVsDebris(e);
    if (filterEl.value === 'ml') return e.ml_yes === true;
    return filterEl.value === 'red' ? e.risk === 'red' : e.risk !== 'green';
  };

  const passes = (e) => matchesFilter(e) && !(hideSameEl.checked && sameConstellation(e));

  // Events between now (sim time) and the end of the chosen look-ahead range.
  function upcoming() {
    const from = state.simTime - 5000;
    const to = rangeEl.value === 'all' ? Infinity : state.simTime + Number(rangeEl.value) * 3600000;
    let list = events.filter((e) => e.tca_ms >= from && e.tca_ms <= to && passes(e));
    const key = sortEl.value;
    if (key === 'ml') list.sort((a, b) => (a.ml_rank ?? Infinity) - (b.ml_rank ?? Infinity));
    else if (key === 'pc') list.sort((a, b) => b.pc - a.pc);
    else if (key === 'miss') list.sort((a, b) => a.miss_km - b.miss_km);
    else list.sort((a, b) => a.tca_ms - b.tca_ms);
    return list;
  }

  let lastIds = '';
  function renderList() {
    const list = upcoming();
    const shown = list.slice(0, MAX_ROWS);
    const ids = shown.map((e) => e.idx).join(',');
    $('conjCount').textContent = `${list.length.toLocaleString()} upcoming`;
    highlightDay();

    const nextRed = events.find((e) => e.risk === 'red' && e.tca_ms >= state.simTime && !(hideSameEl.checked && sameConstellation(e)));
    $('conjNext').innerHTML = nextRed
      ? `Next critical in <b>${formatDuration(nextRed.tca_ms - state.simTime)}</b>: ${escapeHtml(nextRed.name_1)} × ${escapeHtml(nextRed.name_2)}`
      : 'No critical events left in the screening window';

    if (ids === lastIds) {   // same rows: only refresh the countdowns
      for (const row of listEl.children) {
        const e = events[Number(row.dataset.idx)];
        row.querySelector('.when').textContent = `in ${formatDuration(e.tca_ms - state.simTime)}`;
      }
      return;
    }
    lastIds = ids;
    listEl.innerHTML = shown.map((e, pos) => `
      <div class="conj-row${active === e ? ' active' : ''}" data-idx="${e.idx}">
        <span class="badge ${e.risk}"></span>
        <div class="conj-names">${hasRisk ? `<span class="conj-rank">${pos + 1}</span>` : ''}${escapeHtml(e.name_1)} × ${escapeHtml(e.name_2)}</div>
        <div class="conj-meta"><span class="when">in ${formatDuration(e.tca_ms - state.simTime)}</span>
          · ${e.miss_km.toFixed(3)} km · Pc ${formatPc(e.pc)}${mlTag(e)}</div>
      </div>`).join('') || '<div class="muted small">No events match.</div>';
  }
  // ML probability, decision and manoeuvre status for a list row.
  function mlTag(e) {
    if (e.ml_prob == null) return '';
    const m = e.maneuver;
    const plan = !e.ml_yes ? '' : m?.status === 'ok' ? ` · burn ${formatDvShort(m.dv_m_s)}`
      : m?.status === 'warning' ? ' · <span class="ml-warn">⚠ warning</span>' : '';
    return `<br><span class="ml-tag ${e.ml_yes ? 'yes' : 'no'}">ML ${e.ml_yes ? 'YES' : 'NO'} ${e.ml_prob.toFixed(3)}</span>${plan}`;
  }
  const formatDvShort = (v) => (v < 1 ? `${(v * 100).toFixed(1)} cm/s` : `${v.toFixed(2)} m/s`);

  // ---------- events per day (click a day to fast-forward there) ----------
  const nDays = Math.ceil(data.window_hours / 24);
  const daysEl = $('conjDays');
  // Each bar covers 24 h from the screening start, so label it with its exact range.
  const stamp = (ms) => `${shortDate(ms)} ${new Date(ms).toISOString().slice(11, 16)}`;
  const dayRange = (d) => `${stamp(windowStartMs + d * DAY_MS)} – ${stamp(Math.min(windowEndMs, windowStartMs + (d + 1) * DAY_MS))}`;
  function renderDays() {
    const counts = new Array(nDays).fill(0);
    for (const e of events) {
      const d = Math.floor((e.tca_ms - windowStartMs) / DAY_MS);
      if (d >= 0 && d < nDays && passes(e)) counts[d]++;
    }
    const max = Math.max(1, ...counts);
    daysEl.innerHTML = counts.map((n, d) => `
      <button class="day" data-day="${d}" title="${n.toLocaleString()} events · ${dayRange(d)} UTC">
        <span class="count">${n.toLocaleString()}</span>
        <span class="bar" style="height:${Math.max(2, Math.round((n / max) * 40))}px"></span>
        <span class="label">${shortDate(windowStartMs + d * DAY_MS)}</span>
      </button>`).join('');
    highlightDay();
  }
  function highlightDay() {
    const current = Math.floor((state.simTime - windowStartMs) / DAY_MS);
    for (const b of daysEl.children) b.classList.toggle('current', Number(b.dataset.day) === current);
  }
  daysEl.onclick = (ev) => {
    const b = ev.target.closest('.day');
    if (!b) return;
    state.simTime = windowStartMs + Number(b.dataset.day) * DAY_MS;  // start of that day
    rangeEl.value = '24';
    lastIds = '';
    renderList();
  };

  // Restrict the list to one satellite and show its debris check in the satellite card.
  function setFocus(s) {
    focus = s;
    renderDays();
    $('conjFocus').innerHTML = s
      ? `<span>Conjunctions of <b>${escapeHtml(s.name)}</b></span><button id="conjFocusClear" aria-label="Show all">×</button>`
      : '';
    if (s) $('conjFocusClear').onclick = () => setFocus(null);
    lastIds = '';
    renderList();
    renderCheck();
  }

  function renderCheck() {
    const el = $('infoConj');
    if (!focus) { el.innerHTML = ''; return; }
    const mine = events.filter((e) => involves(e, focus.norad) && e.tca_ms >= state.simTime);
    const debris = mine.filter((e) => e.debris);
    const others = mine.filter((e) => !e.debris);
    const until = new Date(windowEndMs).toISOString().slice(0, 16).replace('T', ' ');
    const otherLine = others.length
      ? `${others.length} critical/watch conjunction${others.length > 1 ? 's' : ''} with other satellites.`
      : 'No critical/watch conjunctions with other satellites.';
    if (state.simTime > windowEndMs) {
      el.innerHTML = `<div class="check yellow"><div class="title">Outside the screening window</div>
        Re-run src/screen_conjunctions.py to check times after ${until} UTC.</div>`;
      return;
    }
    if (!debris.length) {
      el.innerHTML = `<div class="check ok"><div class="title">✓ No debris within ${data.screen_km} km</div>
        until ${until} UTC. ${otherLine}</div>`;
      return;
    }
    const worstRisk = debris.reduce((r, e) => (RISK_ORDER[e.risk] < RISK_ORDER[r] ? e.risk : r), 'green');
    const maxPc = Math.max(...debris.map((e) => e.pc));
    const closest = debris.reduce((a, b) => (b.miss_km < a.miss_km ? b : a));
    const next = debris.reduce((a, b) => (b.tca_ms < a.tca_ms ? b : a));
    el.innerHTML = `<div class="check ${worstRisk}">
      <div class="title">⚠ ${debris.length} debris approach${debris.length > 1 ? 'es' : ''} &lt; ${data.screen_km} km</div>
      Next in ${formatDuration(next.tca_ms - state.simTime)} ·
      closest ${closest.miss_km.toFixed(3)} km · highest Pc ${formatPc(maxPc)}<br>${otherLine}
      <br><button id="checkNext">Analyse next debris approach</button></div>`;
    checkNextEvent = next;
  }
  // The card is redrawn several times a second, so act on pointerdown rather than click.
  let checkNextEvent = null;
  $('infoConj').addEventListener('pointerdown', (ev) => {
    if (ev.target.closest('#checkNext') && checkNextEvent) analyze(checkNextEvent);
  });

  listEl.onclick = (ev) => {
    const row = ev.target.closest('.conj-row');
    if (row) analyze(events[Number(row.dataset.idx)]);
  };

  // ---------- 3D analysis objects ----------
  const group = new THREE.Group();
  const markerA = new THREE.Mesh(new THREE.SphereGeometry(0.08, 16, 12), new THREE.MeshBasicMaterial({ color: COLOR_A }));
  const markerB = new THREE.Mesh(new THREE.SphereGeometry(0.08, 16, 12), new THREE.MeshBasicMaterial({ color: COLOR_B }));
  const tcaMarker = new THREE.Mesh(new THREE.SphereGeometry(0.12, 20, 16),
    new THREE.MeshBasicMaterial({ color: 0xff3b30, transparent: true, opacity: 0.45, depthWrite: false }));
  const sepGeometry = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
  const sepLine = new THREE.Line(sepGeometry, new THREE.LineBasicMaterial({ color: 0xffffff }));
  const orbitA = createOrbitPath(COLOR_A, 0.85);
  const orbitB = createOrbitPath(COLOR_B, 0.85);
  group.add(markerA, markerB, tcaMarker, sepLine);
  group.visible = false;
  scene.add(group, orbitA.line, orbitB.line);
  const burnMarker = createBurnMarker(scene);

  let active = null, recA = null, recB = null, lastManeuver = '';
  const tcaPoint = new THREE.Vector3();

  function analyze(e, focus = true) {
    active = e;
    recA = makeSatrec(satByNorad.get(e.norad_1));
    recB = makeSatrec(satByNorad.get(e.norad_2));
    const tca = new Date(e.tca_ms);
    const pa = sat.propagate(recA, tca)?.position, pb = sat.propagate(recB, tca)?.position;
    if (!pa || !pb) return;
    toScene((pa.x + pb.x) / 2, (pa.y + pb.y) / 2, (pa.z + pb.z) / 2, tcaPoint);
    tcaMarker.position.copy(tcaPoint);

    orbitA.show(recA, e.tca_ms);
    orbitB.show(recB, e.tca_ms);
    group.visible = true;
    const m = e.maneuver;
    if (m?.status === 'ok') burnMarker.show(m.maneuver_norad === e.norad_1 ? recA : recB, m);
    else burnMarker.hide();
    lastManeuver = '';
    state.simTime = e.tca_ms - LEAD_MS;
    setSpeed(10);
    if (focus) camera.position.copy(tcaPoint.clone().normalize().multiplyScalar(tcaPoint.length() + 4.5));

    $('conjDetail').style.display = 'block';
    $('conjPair').innerHTML = [[e.name_1, e.norad_1, e.group_1, 'a'], [e.name_2, e.norad_2, e.group_2, 'b']]
      .map(([name, id, g, cls]) => `<div class="pair"><span class="dot ${cls}"></span><b>${escapeHtml(name)}</b>
        <span class="muted small">NORAD ${id} · ${g}</span></div>`).join('');
    lastIds = '';
    onAnalyze();
  }

  function close() {
    active = null;
    group.visible = false;
    burnMarker.hide();
    $('conjManeuver').innerHTML = '';
    orbitA.hide();
    orbitB.hide();
    $('conjDetail').style.display = 'none';
    lastIds = '';
  }
  $('conjClose').onclick = close;
  // The manoeuvre box is redrawn every second, so act on pointerdown rather than click.
  $('conjManeuver').addEventListener('pointerdown', (ev) => {
    const m = active?.maneuver;
    if (!ev.target.closest('#manGo') || m?.status !== 'ok') return;
    state.simTime = m.burn_ms - 60000;   // a minute before the burn, then run at 10x
    setSpeed(10);
    const p = burnMarker.position;
    camera.position.copy(p.clone().normalize().multiplyScalar(p.length() + 4.5));
  });
  $('conjLead').onclick = () => { if (active) { state.simTime = active.tca_ms - LEAD_MS; setSpeed(10); } };
  $('conjTca').onclick = () => { if (active) { state.simTime = active.tca_ms; setSpeed(0); } };

  // ---------- per-frame update ----------
  let lastList = 0, lastDetail = 0;
  function update(prevSimTime) {
    const now = performance.now();
    if (now - lastList > 400) { lastList = now; renderList(); renderCheck(); }
    if (!active) return;

    // Pause exactly at TCA when the clock runs through it.
    if (prevSimTime < active.tca_ms && state.simTime >= active.tca_ms && state.speed > 0 && $('conjAutoPause').checked) {
      state.simTime = active.tca_ms;
      setSpeed(0);
    }

    const date = new Date(state.simTime);
    const pa = sat.propagate(recA, date)?.position, pb = sat.propagate(recB, date)?.position;
    if (!pa || !pb) return;
    toScene(pa.x, pa.y, pa.z, markerA.position);
    toScene(pb.x, pb.y, pb.z, markerB.position);
    sepGeometry.attributes.position.setXYZ(0, markerA.position.x, markerA.position.y, markerA.position.z);
    sepGeometry.attributes.position.setXYZ(1, markerB.position.x, markerB.position.y, markerB.position.z);
    sepGeometry.attributes.position.needsUpdate = true;
    orbitA.update(state.simTime);
    orbitB.update(state.simTime);
    tcaMarker.scale.setScalar(1 + 0.35 * Math.sin(now / 200));

    if (now - lastDetail < 150) return;
    lastDetail = now;
    const dist = Math.hypot(pa.x - pb.x, pa.y - pb.y, pa.z - pb.z);
    const e = active;
    const toTca = e.tca_ms - state.simTime;
    const rows = [
      ['Risk', `<span class="badge-text ${e.risk}">${{ red: 'Critical', yellow: 'Watch', green: 'Low' }[e.risk]}</span>`],
      ['TCA (UTC)', e.tca_utc.replace('T', ' ').replace('Z', '')],
      ['Time to TCA', Math.abs(toTca) < 500 ? '<b>at TCA</b>' : toTca > 0 ? formatDuration(toTca) : `passed ${formatDuration(-toTca)} ago`],
      ['Current distance', `<b>${dist < 10 ? dist.toFixed(3) : dist.toFixed(0)} km</b>`],
      ['Miss distance', `${e.miss_km.toFixed(3)} km`],
      ['Radial / along / cross', `${e.miss_radial_km.toFixed(2)} / ${e.miss_along_km.toFixed(2)} / ${e.miss_cross_km.toFixed(2)} km`],
      ['Relative speed', `${e.rel_speed_km_s.toFixed(2)} km/s`],
      ['Altitude', `${e.altitude_km.toFixed(0)} km`],
      ['Collision probability', formatPc(e.pc)],
    ];
    if (e.ml_prob != null) {
      rows.push(['ML risk probability', `<b>${e.ml_prob.toFixed(3)}</b> (rank ${e.ml_rank.toLocaleString()} of ${events.length.toLocaleString()})`],
        ['ML decision', `<span class="badge-text ${e.ml_yes ? 'red' : 'green'}">${e.ml_yes ? 'YES · high risk' : 'NO'}</span>`]);
    }
    $('conjDetailBody').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
    // The manoeuvre box only changes with its countdown, once a second.
    const html = maneuverHtml(e, (n) => satByNorad.get(n)?.name ?? n, Math.floor(state.simTime / 1000) * 1000, risk);
    if (html !== lastManeuver) { $('conjManeuver').innerHTML = html; lastManeuver = html; }
  }

  renderDays();
  renderList();
  return { update, close, setFocus };
}
