// Data-source selector (live data or a historical replay) and, for a replay, the event banner with
// the prediction report from src/history.py and a button that jumps to the collision.

import { DATASET } from './data.js';

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const fmt = (iso) => iso.replace('T', ' ').replace('Z', '').slice(0, 23);

export async function initDatasetSelector() {
  const sel = $('datasetSel');
  let list = [{ dataset: DATASET, label: DATASET ? DATASET.replace('history/', 'Replay: ') : 'Live (CelesTrak, current)' }];
  try {
    const res = await fetch('api/datasets');
    if (res.ok) list = await res.json();
  } catch { /* static server: only the current dataset */ }
  sel.innerHTML = list.map((d) => `<option value="${escapeHtml(d.dataset)}"${d.dataset === DATASET ? ' selected' : ''}>` +
    `${escapeHtml(d.dataset ? `Replay: ${d.label}` : d.label)}</option>`).join('');
  sel.onchange = () => {
    const url = new URL(location.href);
    if (sel.value) url.searchParams.set('dataset', sel.value); else url.searchParams.delete('dataset');
    location.href = url.toString();
  };
}

// Banner for a replay: what happened, what the screening predicted with each warning time.
export function showEventBanner(event, report, { goToCollision }) {
  if (!event) return;
  const rows = (report?.by_lead ?? []).map((r) => r.predictable
    ? `<tr><td>${r.lead_hours} h</td><td>${fmt(r.tca_utc).slice(11, 21)}</td><td>${Math.round(r.miss_km * 1000)} m</td>` +
      `<td><span class="badge-text ${r.risk}" title="${r.risk}">${r.pc.toExponential(1)}</span></td></tr>`
    : `<tr><td>${r.lead_hours} h</td><td colspan="3" class="wrap">not predictable: object not catalogued yet</td></tr>`).join('');
  const s = report?.screening;
  const verdict = !s ? '' : s.flagged
    ? `Screening with data from before ${fmt(report.cutoff_utc).slice(0, 16)} UTC flagged it: rank ${s.rank_by_pc} of ` +
      `${s.events_screened.toLocaleString()} approaches by Pc.`
    : 'Screening did not flag this pair (no approach under 5 km).';
  $('eventBanner').innerHTML = `
    <div class="title">Replay: ${escapeHtml(event.name)}</div>
    <div>Collision reported ${escapeHtml(fmt(event.start))}${event.end !== event.start ? ` – ${escapeHtml(fmt(event.end).slice(11))}` : ''} UTC.
      Only element sets published before ${escapeHtml(fmt(event.cutoff_utc).slice(0, 16))} UTC are used
      (${event.lead_hours} h warning).</div>
    ${rows ? `<table><tr><th>Warning</th><th>TCA (UTC)</th><th>Miss</th><th>Pc (risk)</th></tr>${rows}</table>` : ''}
    <div>${verdict}</div>
    <div class="row"><button id="goCollision">Go to collision</button>
      <a class="small muted" href="${escapeHtml(event.source)}" target="_blank" rel="noopener">source</a></div>`;
  $('goCollision').onclick = goToCollision;
}
