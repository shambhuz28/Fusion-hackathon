// Conjunction screening results (from src/screen_conjunctions.py): the critical-events panel with
// live countdowns, and an analysis mode that shows both objects, their orbits, the TCA point and
// their live separation. Critical debris-debris events are drawn as red zones, with a warning
// naming the satellites that would fly through the fragments. When the clock leaves the screened
// window, the window starting at the current time is requested from src/server.py (/api/screen)
// and replaces the loaded one.

import * as THREE from 'three';
import { createOrbitPath } from './orbitPath.js';
import { toScene } from './orbit.js';
import { DATASET, dataUrl, makeSatrec } from './data.js';
import { createManeuverView } from './maneuverView.js';
import { SCENE_UNITS_PER_KM } from './config.js';

const sat = window.satellite;
const COLOR_A = 0xffd84d;
const COLOR_B = 0xff4d4d;
const MAX_ROWS = 100;
const LEAD_MS = 10 * 60000;   // analysis starts 10 minutes before TCA
const ZONE_LEAD_MS = 60 * 60000;   // a red zone and its warning appear this long before TCA
const ZONE_LISTED = 8;             // exposed satellites named in the analysis card
const REQUEST_DELAY_MS = 600;  // wait this long outside the window before requesting (skips clock jumps in between)
const $ = (id) => document.getElementById(id);

export async function loadConjunctions() {
  try {
    const res = await fetch(dataUrl('conjunctions.json'));
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

export function createConjunctionView({ data: initialData, scene, camera, sats, state, setSpeed, onAnalyze, clockRange }) {
  const satByNorad = new Map(sats.map((s) => [s.norad, s]));
  let data = null, events = [], windowStartMs = 0, windowEndMs = 0, nDays = 0;
  // Debris-debris red zones of the loaded window (debris_zones in src/screen_conjunctions.py).
  let zoneRadiusKm = 0, zoneEvents = [];
  const zoneGroup = new THREE.Group();
  scene.add(zoneGroup);
  const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;

  // Replace the loaded screening window. Rows are compact arrays; names and groups come from the
  // satellite list.
  function setData(d) {
    data = d;
    events = d.rows
      .map((row) => Object.fromEntries(d.fields.map((f, k) => [f, row[k]])))
      .filter((e) => satByNorad.has(e.norad_1) && satByNorad.has(e.norad_2))
      .sort((a, b) => a.tca_ms - b.tca_ms);
    events.forEach((e, idx) => {
      const a = satByNorad.get(e.norad_1), b = satByNorad.get(e.norad_2);
      Object.assign(e, { idx, name_1: a.name, group_1: a.group, name_2: b.name, group_2: b.group,
        tca_utc: new Date(e.tca_ms).toISOString() });
    });
    windowStartMs = Date.parse(d.window_start_utc);
    windowEndMs = windowStartMs + d.window_hours * 3600000;
    nDays = Math.ceil(d.window_hours / 24);
    setZones(d.debris_zones);

    const c = d.counts ?? {};
    $('conjSummary').textContent =
      `${d.n_objects.toLocaleString()} objects screened for ` +
      `${d.window_hours >= 48 ? `${+(d.window_hours / 24).toFixed(1)} days` : `${d.window_hours} h`} from ` +
      `${d.window_start_utc.slice(0, 16).replace('T', ' ')} UTC · ` +
      `${((c.red ?? 0) + (c.yellow ?? 0) + (c.green ?? 0)).toLocaleString()} approaches < ${d.screen_km} km ` +
      `(${(c.red ?? 0).toLocaleString()} critical, ${(c.yellow ?? 0).toLocaleString()} watch)`;
    $('conjNote').textContent =
      `Pc uses an assumed covariance (TLEs have none) that grows with element age, and a ${d.hbr_m} m ` +
      `combined hard-body radius.` + (d.intra_max_age_days
        ? ` Same-constellation pairs (e.g. Starlink–Starlink) are only screened while both element sets are at most ` +
          `${d.intra_max_age_days} days old: they manoeuvre to keep their spacing, which TLEs cannot predict further ahead.`
        : '');
    lastIds = '';
    renderDays();
    renderList();
    renderCheck();
    renderZoneCheck();
    renderAlert();
  }
  const covered = (t) => data !== null && t >= windowStartMs && t < windowEndMs;

  // Attach the window's red zones to their events, dropping the spheres of the window it replaces.
  function setZones(zones) {
    for (const e of zoneEvents) {
      if (!e.zone.mesh) continue;
      zoneGroup.remove(e.zone.mesh);
      e.zone.mesh.material.dispose();
    }
    zoneRadiusKm = zones?.radius_km ?? 0;
    const eventKey = (e) => `${e.tca_ms}:${e.norad_1}:${e.norad_2}`;
    const eventByKey = new Map(events.map((e) => [eventKey(e), e]));
    zoneEvents = [];
    for (const z of zones?.zones ?? []) {
      const e = eventByKey.get(eventKey(z));
      if (!e) continue;
      e.zone = {
        count: z.n_satellites, windowMin: z.window_min, endMs: z.tca_ms + z.window_min * 60000,
        satellites: z.satellites.map(([norad, km, ms]) => ({ norad, km, ms, name: satByNorad.get(norad)?.name ?? `NORAD ${norad}` })),
      };
      zoneEvents.push(e);
    }
    zoneEvents.sort((a, b) => a.tca_ms - b.tca_ms);
  }

  // ---------- panel ----------
  $('conj').style.display = 'block';

  const filterEl = $('conjFilter'), sortEl = $('conjSort'), hideSameEl = $('conjHideSame'), listEl = $('conjList');
  const rangeEl = $('conjRange');
  for (const el of [filterEl, sortEl, hideSameEl, rangeEl]) el.onchange = () => { lastIds = ''; renderDays(); renderList(); };

  // A focused satellite (selected by the user) overrides the risk filter: all its events are listed.
  let focus = null;
  const matchesFilter = (e) => {
    if (focus) return involves(e, focus.norad);
    if (filterEl.value === 'debris') return satelliteVsDebris(e);
    if (filterEl.value === 'zones') return !!e.zone;
    return filterEl.value === 'red' ? e.risk === 'red' : e.risk !== 'green';
  };

  const passes = (e) => matchesFilter(e) && !(hideSameEl.checked && sameConstellation(e));

  // Events between now (sim time) and the end of the chosen look-ahead range.
  function upcoming() {
    const from = state.simTime - 5000;
    const to = rangeEl.value === 'all' ? Infinity : state.simTime + Number(rangeEl.value) * 3600000;
    let list = events.filter((e) => e.tca_ms >= from && e.tca_ms <= to && passes(e));
    const key = sortEl.value;
    if (key === 'pc') list.sort((a, b) => b.pc - a.pc);
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
    $('conjNext').innerHTML = !covered(state.simTime)
      ? 'This time is outside the screened window'
      : nextRed
        ? `Next critical in <b>${formatDuration(nextRed.tca_ms - state.simTime)}</b>: ${escapeHtml(nextRed.name_1)} × ${escapeHtml(nextRed.name_2)}`
        : 'No critical events left in the screened window';

    if (ids && ids === lastIds) {   // same rows: only refresh the countdowns (an empty list is always redrawn)
      for (const row of listEl.querySelectorAll('.conj-row')) {
        const e = events[Number(row.dataset.idx)];
        if (e) row.querySelector('.when').textContent = `in ${formatDuration(e.tca_ms - state.simTime)}`;
      }
      return;
    }
    lastIds = ids;
    listEl.innerHTML = shown.map((e) => `
      <div class="conj-row${active === e ? ' active' : ''}" data-idx="${e.idx}">
        <span class="badge ${e.risk}"></span>
        <div class="conj-names">${escapeHtml(e.name_1)} × ${escapeHtml(e.name_2)}</div>
        <div class="conj-meta"><span class="when">in ${formatDuration(e.tca_ms - state.simTime)}</span>
          · ${e.miss_km.toFixed(3)} km · Pc ${formatPc(e.pc)}</div>
        ${e.zone ? `<div class="conj-zone">Red zone · ${e.zone.count ? `${plural(e.zone.count, 'satellite')} exposed` : 'no satellites nearby'}</div>` : ''}
      </div>`).join('') || `<div class="muted small">${covered(state.simTime) ? 'No events match.' : 'No screening for this time yet.'}</div>`;
  }
  // ---------- events per day (click a day to fast-forward there) ----------
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
    renderZoneCheck();
  }

  function renderCheck() {
    const el = $('infoConj');
    if (!focus || !data) { el.innerHTML = ''; return; }
    const mine = events.filter((e) => involves(e, focus.norad) && e.tca_ms >= state.simTime);
    const debris = mine.filter((e) => e.debris);
    const others = mine.filter((e) => !e.debris);
    const until = new Date(windowEndMs).toISOString().slice(0, 16).replace('T', ' ');
    const otherLine = others.length
      ? `${others.length} critical/watch conjunction${others.length > 1 ? 's' : ''} with other satellites.`
      : 'No critical/watch conjunctions with other satellites.';
    if (!covered(state.simTime)) {
      el.innerHTML = `<div class="check yellow"><div class="title">This time is not screened yet</div>
        ${status.loading ? 'Screening is running; the check appears when it finishes.' : 'See the conjunction panel.'}</div>`;
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
  // Warn when the selected satellite flies through a coming debris-debris red zone.
  function renderZoneCheck() {
    const el = $('infoZone');
    const hits = focus ? zoneEvents.filter((e) => e.zone.endMs >= state.simTime && e.zone.satellites.some((s) => s.norad === focus.norad)) : [];
    if (!hits.length) { el.innerHTML = ''; return; }
    const e = hits[0], pass = e.zone.satellites.find((s) => s.norad === focus.norad);
    el.innerHTML = `<div class="check red"><div class="title">⚠ Debris–debris red zone on its path</div>
      ${escapeHtml(e.name_1)} × ${escapeHtml(e.name_2)} come within ${e.miss_km.toFixed(3)} km of each other
      ${e.tca_ms > state.simTime ? `in ${formatDuration(e.tca_ms - state.simTime)}` : `${formatDuration(state.simTime - e.tca_ms)} ago`}.
      If they collide, this satellite passes ${pass.km.toFixed(0)} km from the collision point
      ${formatDuration(pass.ms - e.tca_ms)} later and could be hit by fragments.
      ${hits.length > 1 ? `<br>${hits.length - 1} more red zone${hits.length > 2 ? 's' : ''} on its path later.` : ''}</div>`;
  }

  // Top banner: the live or next red zone that has satellites exposed.
  let alertEvent = null;
  function renderAlert() {
    const el = $('zoneAlert');
    alertEvent = zoneEvents.find((e) => e.zone.count && state.simTime >= e.tca_ms - ZONE_LEAD_MS && state.simTime <= e.zone.endMs) ?? null;
    if (!alertEvent) { el.style.display = 'none'; return; }
    const e = alertEvent, z = e.zone, dt = e.tca_ms - state.simTime;
    const named = z.satellites.slice(0, 3).map((s) => `${escapeHtml(s.name)} (${s.km.toFixed(0)} km)`).join(', ');
    el.style.display = 'block';
    const when = Math.abs(dt) < 500 ? 'risk now' : dt > 0 ? `risk in ${formatDuration(dt)}` : `risk: closest approach was ${formatDuration(-dt)} ago`;
    el.innerHTML = `<b>⚠ Debris–debris collision ${when}</b>
      <button id="zoneAlertBtn">Analyse</button><br>
      ${escapeHtml(e.name_1)} × ${escapeHtml(e.name_2)}: miss ${e.miss_km.toFixed(3)} km, Pc ${formatPc(e.pc)}. Neither can manoeuvre.
      If they collide, fragments could harm <b>${plural(z.count, 'satellite')}</b> passing within ${zoneRadiusKm} km of the red zone:
      ${named}${z.count > 3 ? ` and ${z.count - 3} more` : ''}.`;
  }
  $('zoneAlert').addEventListener('pointerdown', (ev) => {
    if (ev.target.closest('#zoneAlertBtn') && alertEvent) analyze(alertEvent);
  });

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

  let active = null, recA = null, recB = null;
  const tcaPoint = new THREE.Vector3();
  const maneuver = createManeuverView({
    scene, state, satByNorad, setSpeed,
    onPlanChange: () => { lastDetail = 0; },
    notBefore: () => windowStartMs,          // plan as if decided when the conjunction was screened
  });
  $('conjManeuver').onclick = () => { if (active) maneuver.request(active); };

  // Red zones: a translucent sphere at the collision point, from an hour before TCA until one orbit after.
  const zoneGeometry = new THREE.SphereGeometry(1, 32, 20);   // scaled to the zone radius per mesh
  function zoneMesh(e) {   // built the first time the zone is shown; null if SGP4 fails
    if (e.zone.mesh !== undefined) return e.zone.mesh;
    const tca = new Date(e.tca_ms);
    const pa = sat.propagate(makeSatrec(satByNorad.get(e.norad_1)), tca)?.position;
    const pb = sat.propagate(makeSatrec(satByNorad.get(e.norad_2)), tca)?.position;
    e.zone.mesh = null;
    if (pa && pb) {
      e.zone.mesh = new THREE.Mesh(zoneGeometry,
        new THREE.MeshBasicMaterial({ color: 0xff2d2d, transparent: true, opacity: 0.2, depthWrite: false }));
      e.zone.mesh.scale.setScalar(zoneRadiusKm * SCENE_UNITS_PER_KM);
      toScene((pa.x + pb.x) / 2, (pa.y + pb.y) / 2, (pa.z + pb.z) / 2, e.zone.mesh.position);
      zoneGroup.add(e.zone.mesh);
    }
    return e.zone.mesh;
  }
  function updateZones(now) {
    for (const e of zoneEvents) {
      const shown = e === active || (state.simTime >= e.tca_ms - ZONE_LEAD_MS && state.simTime <= e.zone.endMs);
      if (!shown && !e.zone.mesh) continue;
      const mesh = zoneMesh(e);
      if (!mesh) continue;
      mesh.visible = shown;
      // steady before closest approach, pulsing while fragments would be coming back through
      mesh.material.opacity = state.simTime >= e.tca_ms ? 0.26 + 0.12 * Math.sin(now / 250) : 0.2;
    }
  }

  function analyze(e, focus = true) {
    if (!maneuver.forEvent(e)) maneuver.clear();
    active = e;
    recA = makeSatrec(satByNorad.get(e.norad_1));
    recB = makeSatrec(satByNorad.get(e.norad_2));
    const tca = new Date(e.tca_ms);
    const pa = sat.propagate(recA, tca)?.position, pb = sat.propagate(recB, tca)?.position;
    if (!pa || !pb) return;
    toScene((pa.x + pb.x) / 2, (pa.y + pb.y) / 2, (pa.z + pb.z) / 2, tcaPoint);
    tcaMarker.position.copy(tcaPoint);
    tcaMarker.visible = !e.zone;   // a red zone marks the same point
    markerA.scale.setScalar(e.zone ? 0.35 : 1);   // keep the two objects from hiding their zone
    markerB.scale.setScalar(e.zone ? 0.35 : 1);

    orbitA.show(recA, e.tca_ms);
    orbitB.show(recB, e.tca_ms);
    group.visible = true;
    state.simTime = e.tca_ms - LEAD_MS;
    setSpeed(10);
    if (focus) camera.position.copy(tcaPoint.clone().normalize().multiplyScalar(tcaPoint.length() + 4.5));

    $('conjDetail').style.display = 'block';
    $('conjPair').innerHTML = [[e.name_1, e.norad_1, e.group_1, 'a'], [e.name_2, e.norad_2, e.group_2, 'b']]
      .map(([name, id, g, cls]) => `<div class="pair"><span class="dot ${cls}"></span><b>${escapeHtml(name)}</b>
        <span class="muted small">NORAD ${id} · ${g}</span></div>`).join('');
    $('conjZone').innerHTML = e.zone ? zoneHtml(e) : '';
    lastIds = '';
    onAnalyze();
  }

  function zoneHtml(e) {
    const z = e.zone;
    const rows = z.satellites.slice(0, ZONE_LISTED).map((s) =>
      `<tr><td>${escapeHtml(s.name)}</td><td>${s.km.toFixed(0)} km</td><td>TCA + ${formatDuration(s.ms - e.tca_ms)}</td></tr>`).join('');
    return `<div class="check red"><div class="title">⚠ Red zone: debris × debris</div>
      Neither object can manoeuvre. If they collide, the fragments return through this point every orbit.
      ${z.count
        ? `In the ${z.windowMin.toFixed(0)} min after TCA, <b>${plural(z.count, 'active satellite')}</b>
           ${z.count === 1 ? 'passes' : 'pass'} within ${zoneRadiusKm} km of it and could be harmed:
           <table class="zone-sats">${rows}</table>${z.count > ZONE_LISTED ? `<div class="muted small">and ${z.count - ZONE_LISTED} more</div>` : ''}`
        : `No active satellite passes within ${zoneRadiusKm} km of it in the ${z.windowMin.toFixed(0)} min after TCA.`}</div>`;
  }

  function close() {
    maneuver.clear();
    active = null;
    group.visible = false;
    orbitA.hide();
    orbitB.hide();
    $('conjDetail').style.display = 'none';
    lastIds = '';
  }
  $('conjClose').onclick = close;
  $('conjLead').onclick = () => { if (active) { state.simTime = active.tca_ms - LEAD_MS; setSpeed(10); } };
  $('conjTca').onclick = () => {
    if (active) { state.simTime = maneuver.summary()?.tca_after_ms ?? active.tca_ms; setSpeed(0); }
  };

  // ---------- per-frame update ----------
  // ---------- on-demand screening (src/server.py) ----------
  const status = { loading: false, apiMissing: false, failedKey: null, outSince: null };
  const statusEl = $('conjStatus');
  const spanEl = $('conjSpan');
  const fmtUtc = (ms) => new Date(ms).toISOString().slice(0, 16).replace('T', ' ');
  function showStatus(html, kind = '') {
    statusEl.className = kind;
    statusEl.innerHTML = html;
  }

  async function requestScreening(timeMs) {
    const startMs = Math.floor(timeMs / 3600000) * 3600000;
    const hours = Number(spanEl.value);
    const key = `${startMs}:${hours}`;
    if (status.loading || key === status.failedKey) return;
    status.loading = true;
    showStatus(`<span class="spinner"></span>Screening ${hours >= 48 ? `${hours / 24} days` : `${hours} h`} from ` +
      `${fmtUtc(startMs)} UTC… (up to ${Math.round(hours / 24 * 45 + 20)} s; dates screened before load instantly)`, 'loading');
    try {
      const res = await fetch(`api/screen?start=${new Date(startMs).toISOString()}&hours=${hours}` +
        (DATASET ? `&dataset=${encodeURIComponent(DATASET)}` : ''));
      if (res.status === 404 || res.status === 501) {
        status.apiMissing = true;
        showStatus('This time is outside the screened window. Start the app with <code>./run.sh</code> ' +
          '(or <code>python3 src/server.py</code>) to screen any date on demand.', 'warn');
      } else if (!res.ok) {
        status.failedKey = key;
        showStatus(escapeHtml((await res.json().catch(() => ({}))).error ?? `Screening failed (HTTP ${res.status}).`), 'warn');
      } else {
        setData(await res.json());
        showStatus('');
      }
    } catch {
      status.failedKey = key;
      showStatus('The simulation server is not reachable; restart it with <code>./run.sh --serve-only</code>.', 'warn');
    } finally {
      status.loading = false;
    }
  }
  $('conjScreenHere').onclick = () => { status.failedKey = null; requestScreening(state.simTime); };
  spanEl.onchange = () => { status.failedKey = null; };

  // Request the window for the current time once the clock has stayed outside the loaded one.
  function ensureCoverage(now) {
    const t = state.simTime;
    const inRange = !clockRange || (t >= clockRange[0] && t <= clockRange[1]);
    if (covered(t) || !inRange || status.apiMissing) {
      status.outSince = null;
      return;
    }
    status.outSince ??= now;
    if (now - status.outSince > REQUEST_DELAY_MS) requestScreening(t);
  }

  let lastList = 0, lastDetail = 0;
  function update(prevSimTime) {
    const now = performance.now();
    if (now - lastList > 400) {
      lastList = now; renderList(); renderCheck(); renderZoneCheck(); renderAlert(); ensureCoverage(now);
    }
    updateZones(now);
    if (!active) return;

    // Pause exactly at TCA when the clock runs through it (the manoeuvred TCA while a plan is flown).
    const tcaMs = maneuver.summary()?.tca_after_ms ?? active.tca_ms;
    if (prevSimTime < tcaMs && state.simTime >= tcaMs && state.speed > 0 && $('conjAutoPause').checked) {
      state.simTime = tcaMs;
      setSpeed(0);
    }

    const date = new Date(state.simTime);
    let pa = sat.propagate(recA, date)?.position, pb = sat.propagate(recB, date)?.position;
    if (!pa || !pb) return;
    // While a suggested manoeuvre is flown, the manoeuvring satellite follows its new path.
    const shift = (p, o) => (o ? { x: p.x + o[0], y: p.y + o[1], z: p.z + o[2] } : p);
    pa = shift(pa, maneuver.offset(active.norad_1, state.simTime));
    pb = shift(pb, maneuver.offset(active.norad_2, state.simTime));
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
    const plan = maneuver.summary();
    if (plan) {
      rows.splice(3, 0, ['With manoeuvre', `<b>${(plan.miss_after_km * 1000).toFixed(0)} m</b> miss · Pc ${formatPc(plan.pc_after)}`]);
    } else if (maneuver.warning()) {
      rows.splice(1, 0, ['Avoidance', '<span class="badge-text red">no safe manoeuvre</span>']);
    }
    $('conjDetailBody').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
  }

  // Analyse the screened pass of two objects closest to nearMs (used by the replay's "Go to collision").
  function analyzePair(n1, n2, nearMs) {
    const passes = events.filter((e) => (e.norad_1 === n1 && e.norad_2 === n2) || (e.norad_1 === n2 && e.norad_2 === n1));
    if (!passes.length) return false;
    analyze(passes.reduce((a, b) => (Math.abs(b.tca_ms - nearMs) < Math.abs(a.tca_ms - nearMs) ? b : a)));
    return true;
  }

  if (initialData) setData(initialData);
  else showStatus('No screening loaded yet.', 'warn');
  return { update, close, setFocus, analyzePair };
}
