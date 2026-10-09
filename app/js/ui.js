// Control panel: clock, speed buttons, colour mode, LEO toggle, group legend, stats and search.

import { GROUP_COLORS, SPEEDS } from './config.js';
import { altitudeGradientCss } from './colors.js';
import { altitudeKm } from './orbit.js';

const $ = (id) => document.getElementById(id);

export function createPanel({ sats, groupNames, generatedUtc, state, onColorModeChange, onLeoToggle, onSearchSelect }) {
  const nDebris = sats.filter(s => s.group === 'Debris').length;
  $('subtitle').textContent = `${(sats.length - nDebris).toLocaleString()} LEO satellites + ${nDebris.toLocaleString()} debris · SGP4 · ` +
    `elements from ${generatedUtc.slice(0, 16).replace('T', ' ')} UTC`;

  // speed buttons
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
  $('now').onclick = () => { state.simTime = Date.now(); };
  $('plusHour').onclick = () => { state.simTime += 3600000; };
  $('plusDay').onclick = () => { state.simTime += 86400000; };
  $('plusWeek').onclick = () => { state.simTime += 7 * 86400000; };
  // datetime-local has no time zone; the value is read as UTC.
  $('goDate').value = new Date(state.simTime).toISOString().slice(0, 16);
  $('goDateBtn').onclick = () => {
    const ms = Date.parse($('goDate').value + ':00Z');
    if (!Number.isNaN(ms)) state.simTime = ms;
  };

  // colour mode + LEO boundary toggle
  $('gradient').style.background = altitudeGradientCss();
  $('colorMode').onchange = (e) => {
    state.colorMode = e.target.value;
    const alt = state.colorMode === 'altitude';
    $('gradient').style.display = alt ? 'block' : 'none';
    $('gradient-labels').style.display = alt ? 'flex' : 'none';
    for (const s of document.querySelectorAll('#legend .swatch')) s.style.visibility = alt ? 'hidden' : 'visible';
    onColorModeChange();
  };
  $('leoToggle').onchange = (e) => onLeoToggle(e.target.checked);

  // group legend with show/hide checkboxes
  const legend = $('legend');
  const checkboxes = {};
  for (const g of groupNames) {
    const count = sats.filter(s => s.group === g).length;
    const label = document.createElement('label');
    label.className = 'legend-item';
    label.innerHTML = `<input type="checkbox" checked><span class="swatch" style="background:${GROUP_COLORS[g]}"></span>` +
      `<span>${g}</span><span class="count">${count.toLocaleString()}</span>`;
    checkboxes[g] = label.querySelector('input');
    checkboxes[g].onchange = (e) => {
      e.target.checked ? state.visibleGroups.add(g) : state.visibleGroups.delete(g);
      updateStats();
    };
    legend.appendChild(label);
  }
  function showGroup(g) {
    state.visibleGroups.add(g);
    checkboxes[g].checked = true;
  }

  // stats for the visible groups
  let lastStats = 0;
  function updateStats(throttle = false) {
    const now = performance.now();
    if (!state.latest || (throttle && now - lastStats < 500)) return;
    lastStats = now;
    let n = 0, altSum = 0, vSum = 0;
    const p = state.latest.pos, v = state.latest.vel;
    for (let i = 0; i < sats.length; i++) {
      const j = i * 3;
      if (!state.visibleGroups.has(sats[i].group) || Number.isNaN(p[j])) continue;
      n++;
      altSum += altitudeKm(p[j], p[j + 1], p[j + 2]);
      vSum += Math.hypot(v[j], v[j + 1], v[j + 2]);
    }
    $('stats').innerHTML = `<dt>Objects</dt><dd>${n.toLocaleString()}</dd>` +
      `<dt>Mean altitude</dt><dd>${n ? (altSum / n).toFixed(0) : '–'} km</dd>` +
      `<dt>Mean speed</dt><dd>${n ? (vSum / n).toFixed(2) : '–'} km/s</dd>`;
  }

  // search by name or NORAD ID
  const searchEl = $('search'), suggestEl = $('suggestions');
  function matches(q) {
    q = q.trim().toUpperCase();
    if (!q) return [];
    const out = [];
    for (let i = 0; i < sats.length && out.length < 8; i++) {
      if (String(sats[i].norad) === q || sats[i].name.toUpperCase().includes(q)) out.push(i);
    }
    return out;
  }
  const choose = (i) => { suggestEl.innerHTML = ''; onSearchSelect(i); };
  searchEl.oninput = () => {
    suggestEl.innerHTML = '';
    for (const i of matches(searchEl.value)) {
      const d = document.createElement('div');
      d.textContent = `${sats[i].name} · ${sats[i].norad}`;
      d.onclick = () => choose(i);
      suggestEl.appendChild(d);
    }
  };
  searchEl.onkeydown = (e) => {
    if (e.key !== 'Enter') return;
    const m = matches(searchEl.value);
    if (m.length) choose(m[0]);
  };

  const clockEl = $('clock');
  function updateClock() {
    clockEl.textContent = new Date(state.simTime).toISOString().slice(0, 19).replace('T', ' ') + ' UTC';
  }

  return { updateStats, updateClock, showGroup, setSpeed };
}
