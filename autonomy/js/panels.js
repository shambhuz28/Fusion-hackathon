// Panels: network statistics so far, the message feed, the threats list and the detail cards.

import { perigeeApogeeKm, periodMinutes } from '../../app/js/orbit.js';

const sat = window.satellite;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
export const clock = (ms) => new Date(ms).toISOString().slice(11, 19);

export function duration(ms) {
  const s = Math.round(Math.abs(ms) / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${String(m).padStart(2, '0')} min` : m ? `${m} min ${s % 60} s` : `${s} s`;
}
const relative = (ms) => (ms >= 0 ? `in ${duration(ms)}` : `${duration(ms)} ago`);
const pct = (f) => `${Math.round(f * 100)} %`;
const fuelBar = (f) => `<span class="fuelbar"><span style="width:${Math.max(0, Math.min(100, f * 100))}%"></span></span>${pct(f)}`;
const metres = (km) => (km < 10 ? `${Math.round(km * 1000).toLocaleString()} m` : `${km.toFixed(0)} km`);

export function createPanels({ run, sats, satByNorad, onSelectThreat, onJump }) {
  const attrs = new Map(run.satellites.rows.map(([n, f0, f1, s, mass, isp, capacity]) =>
    [n, { fuelStart: f0, fuelEnd: f1, sigma: s, mass, isp, capacity }]));
  const name = (n) => satByNorad.get(n)?.name ?? `NORAD ${n}`;
  const threats = run.threats;
  const byId = new Map(threats.map((t) => [t.id, t]));
  const cfg = run.config;

  // Fuel of a satellite at time ms: its starting level minus every avoidance it has started.
  function fuelAt(norad, ms) {
    let f = attrs.get(norad)?.fuelStart ?? 0;
    for (const t of threats) {
      if (t.status === 'avoided' && t.manoeuvring === norad && ms >= t.plan.burns[0].time_ms) f -= t.fuel_used_fraction;
    }
    return f;
  }

  // Where a threat stands at time ms.
  function stateOf(t, ms) {
    if (t.status === 'warning') return ms <= t.tca_ms ? ['warning', 'cannot avoid'] : ['warning', 'passed (risk was not avoided)'];
    if (t.status === 'monitor') return ms <= t.tca_ms ? ['monitor', `monitoring · TCA ${relative(t.tca_ms - ms)}`] : ['done', 'passed safely'];
    const first = t.plan.burns[0].time_ms, back = t.plan.back_on_orbit_ms;
    if (ms < first) return ['planned', `detour planned · burn ${relative(first - ms)}`];
    if (ms <= back) return ['manoeuvring', ms <= t.tca_ms ? `on detour · TCA ${relative(t.tca_ms - ms)}` : 'returning to orbit'];
    return ['done', 'avoided · back on orbit'];
  }

  // --- statistics: cumulative over the simulated minutes so far
  const pm = run.per_minute;
  const cumulative = [];
  pm.rows.reduce((acc, row) => { const next = acc.map((v, k) => v + row[k]); cumulative.push(next); return next; }, [0, 0, 0, 0, 0]);
  function renderStats(ms, linksNow) {
    const k = Math.max(0, Math.min(cumulative.length - 1, Math.floor((ms - pm.start_ms) / 60000)));
    const [, messages, assessed, discarded] = ms < pm.start_ms ? [0, 0, 0, 0] : cumulative[k];
    const kept = threats.filter((t) => t.detected_ms <= ms).length;
    const last = ms < pm.start_ms ? [0, 0, 0, 0] : pm.rows[k];
    $('stats').innerHTML = [
      ['Links right now', linksNow.toLocaleString()],
      ['Messages received', messages.toLocaleString()],
      ['New data assessed', assessed.toLocaleString()],
      ['Discarded (no risk)', discarded.toLocaleString()],
      ['Threats kept', kept.toLocaleString()],
      ['This minute', `${last[1].toLocaleString()} msgs · ${last[3].toLocaleString()} discarded`],
    ].map(([a, b]) => `<dt>${a}</dt><dd>${b}</dd>`).join('');
  }

  // --- feed of threat events up to the current time
  const feedEvents = run.feed;
  let lastFeedKey = '';
  function feedText(f) {
    const t = byId.get(f.threat);
    const pair = `${esc(name(t.a))} ↔ ${esc(name(t.b))}`;
    if (f.kind === 'detected') {
      const what = `exchanged data ${t.detected_distance_km} km apart: predicted ${metres(t.miss_km)} in ${duration(t.tca_ms - t.detected_ms)}, ` +
        `Pc ${t.pc_onboard.toExponential(1)}`;
      const chosen = t.options?.find((o) => o.norad === t.manoeuvring);
      const other = t.options?.find((o) => o.norad !== t.manoeuvring);
      const share = (o) => (o?.share_of_fuel != null ? `${(o.share_of_fuel * 100).toFixed(1)} %` : 'not possible');
      const action = t.status === 'avoided'
        ? `kept · <b>${esc(name(t.manoeuvring))}</b> will manoeuvre (${share(chosen)} of its fuel vs ${share(other)})`
        : t.status === 'monitor' ? 'kept · monitoring' : '<b>kept · cannot avoid</b>';
      return [t.status === 'warning' ? 'warning' : 'detected', `${pair} ${what} → ${action}`];
    }
    if (f.kind === 'burn') {
      const b = t.plan.burns[f.burn];
      return ['burn', `${esc(name(t.manoeuvring))}: ${esc(b.label.toLowerCase())}, ${b.dv_mps.toFixed(3)} m/s`];
    }
    const passed = t.status === 'avoided'
      ? `passed ${metres(t.plan.miss_after_km)} apart (${metres(t.miss_km)} without the detour)`
      : t.status === 'monitor' ? `passed ${metres(t.miss_km)} apart, as predicted` : `closest approach ${metres(t.miss_km)}: risk not avoided`;
    return [t.status === 'warning' ? 'warning' : 'tca', `${pair} ${passed}`];
  }
  function renderFeed(ms) {
    let end = 0;
    while (end < feedEvents.length && feedEvents[end].t_ms <= ms) end++;
    const shown = feedEvents.slice(Math.max(0, end - 40), end).reverse();
    const key = `${end}`;
    $('feedCount').textContent = `${end.toLocaleString()} events`;
    if (key === lastFeedKey) return;
    lastFeedKey = key;
    const discardLine = `<div class="feed-item discard"><span class="time">every 10 s</span>each satellite broadcasts; receivers ` +
      `that find no future risk discard the data (${run.summary.discarded.toLocaleString()} times in this run)</div>`;
    $('feed').innerHTML = shown.map((f) => {
      const [kind, text] = feedText(f);
      return `<div class="feed-item ${kind}" data-threat="${f.threat}"><span class="time">${clock(f.t_ms)}</span>${text}</div>`;
    }).join('') + discardLine;
  }
  $('feed').onclick = (ev) => {
    const item = ev.target.closest('[data-threat]');
    if (item) onSelectThreat(byId.get(Number(item.dataset.threat)));
  };

  // --- threats known so far
  let lastThreatKey = '';
  function renderThreats(ms, activeId) {
    const known = threats.filter((t) => t.detected_ms <= ms);
    $('threatCount').textContent = `${known.length} of ${threats.length} in this run`;
    const rows = known.slice().reverse().map((t) => {
      const [cls, text] = stateOf(t, ms);
      return { id: t.id, html: `<div class="threat-row${t.id === activeId ? ' active' : ''}" data-threat="${t.id}">
        <span class="dot s-${cls}"></span><span class="names">${esc(name(t.a))} × ${esc(name(t.b))}</span>
        <span class="state">${esc(text)}</span></div>` };
    });
    const key = rows.map((r) => r.html).join('');
    if (key === lastThreatKey) return;
    lastThreatKey = key;
    $('threats').innerHTML = key || '<div class="muted small">No threats yet: every message so far was discarded.</div>';
  }
  $('threats').onclick = (ev) => {
    const row = ev.target.closest('[data-threat]');
    if (row) onSelectThreat(byId.get(Number(row.dataset.threat)));
  };

  // --- detail cards
  const kg = (x) => (x >= 10 ? `${x.toFixed(0)} kg` : x >= 0.1 ? `${x.toFixed(2)} kg` : `${(x * 1000).toFixed(1)} g`);
  const massText = (m) => (m >= 1000 ? `${(m / 1000).toFixed(0)} t` : `${m.toFixed(0)} kg`);

  // One satellite's broadcast data, plus what avoiding would cost it (when the threat was assessed).
  function message(norad, ms, option, chosen) {
    const a = attrs.get(norad);
    const cost = !option ? '' : option.dv_mps != null
      ? `<dt>Detour Δv</dt><dd>${option.dv_mps.toFixed(3)} m/s</dd>
         <dt>Propellant</dt><dd>${kg(option.propellant_kg)} = <b>${(option.share_of_fuel * 100).toFixed(2)} %</b> of its fuel</dd>`
      : `<dt>Detour</dt><dd>not possible</dd>`;
    return `<div${chosen ? ' class="chosen"' : ''}><b>${esc(name(norad))}</b> <span class="muted small">NORAD ${norad}${chosen ? ' · manoeuvres' : ''}</span>
      <dl><dt>Mass · thruster</dt><dd>${massText(a.mass)} · Isp ${a.isp} s</dd>
      ${option ? `<dt>Speed at TCA</dt><dd>${option.speed_km_s.toFixed(3)} km/s</dd>` : ''}
      <dt>Fuel</dt><dd>${fuelBar(fuelAt(norad, ms))} ${kg(fuelAt(norad, ms) * a.capacity)}</dd>
      <dt>Position ±1σ</dt><dd>${Math.round(a.sigma)} / ${Math.round(a.sigma * cfg.sigma_along_factor)} / ${Math.round(a.sigma)} m</dd>
      ${cost}</dl>${option && !option.feasible && option.why ? `<div class="small muted">${esc(option.why)}</div>` : ''}</div>`;
  }

  function threatDetail(t, ms, distanceKm) {
    const [cls, text] = stateOf(t, ms);
    let decision;
    if (t.status === 'avoided') {
      const p = t.plan;
      const other = t.manoeuvring === t.a ? t.b : t.a;
      const steps = p.burns.map((b) => `<li class="${ms >= b.time_ms ? 'done' : ''}"><b>${esc(b.label)}</b> ${b.dv_mps.toFixed(3)} m/s
        ${b === p.burns[0] ? esc(p.direction_name) : ''} · ${clock(b.time_ms)} UTC (${relative(b.time_ms - ms)})</li>`).join('');
      const mine = t.options.find((o) => o.norad === t.manoeuvring), theirs = t.options.find((o) => o.norad === other);
      const why = theirs?.feasible
        ? `the detour costs it ${(mine.share_of_fuel * 100).toFixed(2)} % of its remaining fuel, against
           ${(theirs.share_of_fuel * 100).toFixed(2)} % for ${esc(name(other))} (from each one's mass, thruster, fuel and
           the Δv its own velocity needs)`
        : `${esc(name(other))} cannot: ${esc(theirs?.why ?? 'no plan')}`;
      decision = `<div class="decision ok"><b>✓ Avoided by ${esc(name(t.manoeuvring))}</b>: ${why}. Both satellites
        hold the same data, so they agree without further messages.
        <ol class="steps">${steps}</ol>
        Miss ${metres(t.miss_km)} → <b>${metres(p.miss_after_km)}</b> · total ${p.total_dv_mps.toFixed(3)} m/s ·
        ${kg(mine.propellant_kg)} propellant · back on its original orbit ${duration(p.back_on_orbit_ms - t.tca_ms)} after
        closest approach. Detour checked against the ${p.known_objects_checked.toLocaleString()} satellites it has heard from:
        no new approach under ${p.secondary.threshold_km} km.</div>`;
    } else if (t.status === 'monitor') {
      decision = `<div class="decision watch"><b>Kept and monitored</b>: Pc ${t.pc_onboard.toExponential(1)} is below the
        ${cfg.pc_manoeuvre} manoeuvre threshold and the miss is over ${cfg.miss_keep_km * 1000} m, so no burn is needed.</div>`;
    } else {
      decision = `<div class="decision bad"><b>⚠ Collision risk remains</b>: ${esc(t.reason)}</div>`;
    }
    return `<h2>${esc(name(t.a))} × ${esc(name(t.b))}</h2>
      <div class="muted small">State: <span class="dot s-${cls}" style="display:inline-block"></span> ${esc(text)}</div>
      <div class="msg">${[t.a, t.b].map((n) => message(n, ms, t.options?.find((o) => o.norad === n),
        t.status === 'avoided' && t.manoeuvring === n)).join('')}</div>
      <dl>
        <dt>Data exchanged</dt><dd>${clock(t.detected_ms)} UTC, ${t.detected_distance_km} km apart</dd>
        <dt>Closest approach</dt><dd>${clock(t.tca_ms)} UTC (${relative(t.tca_ms - ms)})</dd>
        <dt>Warning time</dt><dd>${duration(t.tca_ms - t.detected_ms)}</dd>
        <dt>Predicted miss</dt><dd>${metres(t.miss_km)} at ${t.rel_speed_km_s.toFixed(1)} km/s</dd>
        <dt>Pc onboard / ground</dt><dd>${t.pc_onboard.toExponential(1)} / ${t.pc_ground.toExponential(1)}</dd>
        ${distanceKm != null ? `<dt>Distance now</dt><dd><b>${metres(distanceKm)}</b></dd>` : ''}
      </dl>
      ${decision}
      <div class="row">
        <button data-jump="${t.detected_ms - 20000}">Go to data exchange</button>
        ${t.status === 'avoided' ? `<button data-jump="${t.plan.burns[0].time_ms - 60000}">Go to avoidance burn</button>` : ''}
        <button data-jump="${(t.plan?.tca_after_ms ?? t.tca_ms) - 120000}">Go to closest approach</button>
      </div>`;
  }

  function satelliteDetail(s, i, ms, radio, neighbours) {
    if (!radio) {
      return `<h2>${esc(s.name)}</h2><div class="muted small">NORAD ${s.norad} · ${esc(s.group)}</div>
        <div class="decision bad">No radio: this object does not broadcast, so satellites cannot learn its orbit from it.</div>`;
    }
    const rec = sat.json2satrec(s.omm);
    const date = new Date(ms);
    const pv = sat.propagate(rec, date);
    const geo = pv ? sat.eciToGeodetic(pv.position, sat.gstime(date)) : null;
    const [peri, apo] = perigeeApogeeKm(rec);
    const kept = threats.filter((t) => (t.a === s.norad || t.b === s.norad) && t.detected_ms <= ms);
    const a = attrs.get(s.norad);
    return `<h2>${esc(s.name)}</h2><div class="muted small">NORAD ${s.norad} · ${esc(s.group)} · its broadcast message now</div>
      <dl>
        ${geo ? `<dt>Position</dt><dd>${sat.degreesLat(geo.latitude).toFixed(2)}°, ${sat.degreesLong(geo.longitude).toFixed(2)}°,
          ${geo.height.toFixed(1)} km altitude</dd>
        <dt>Velocity</dt><dd>${Math.hypot(pv.velocity.x, pv.velocity.y, pv.velocity.z).toFixed(3)} km/s</dd>` : ''}
        <dt>Orbit</dt><dd>${(rec.inclo * 180 / Math.PI).toFixed(2)}° · ${periodMinutes(rec).toFixed(1)} min ·
          ${peri.toFixed(0)}-${apo.toFixed(0)} km</dd>
        <dt>Mass · thruster</dt><dd>${massText(a.mass)} · Isp ${a.isp} s (assumed)</dd>
        <dt>Fuel</dt><dd>${fuelBar(fuelAt(s.norad, ms))} ${kg(fuelAt(s.norad, ms) * a.capacity)}</dd>
        <dt>Position ±1σ (R/T/N)</dt><dd>${Math.round(a.sigma)} / ${Math.round(a.sigma * cfg.sigma_along_factor)} / ${Math.round(a.sigma)} m</dd>
        <dt>In range now</dt><dd>${neighbours.length ? neighbours.slice(0, 4).map(([j, d]) => `${esc(sats[j].name)} (${d.toFixed(0)} km)`).join(', ') +
          (neighbours.length > 4 ? ` +${neighbours.length - 4}` : '') : 'none'}</dd>
        <dt>Threats kept</dt><dd>${kept.length ? kept.map((t) => `<a href="#" data-threat="${t.id}">${esc(name(t.a === s.norad ? t.b : t.a))}</a>`).join(', ') : 'none'}</dd>
      </dl>`;
  }

  // The card is redrawn several times a second, so act on pointerdown (a click could land on a replaced button).
  $('detailBody').onpointerdown = (ev) => {
    const jump = ev.target.closest('[data-jump]');
    if (jump) onJump(Number(jump.dataset.jump));
    const link = ev.target.closest('[data-threat]');
    if (link) { ev.preventDefault(); onSelectThreat(byId.get(Number(link.dataset.threat))); }
  };

  return {
    stateOf,
    update(ms, linksNow, activeId) { renderStats(ms, linksNow); renderFeed(ms); renderThreats(ms, activeId); },
    threatDetail, satelliteDetail,
  };
}
