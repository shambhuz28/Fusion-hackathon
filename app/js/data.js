// Loads the LEO objects exported by src/export_sim_data.py (full-precision OMM elements).
// ?dataset=history/<event> loads a historical replay built by src/history.py instead of the live data.

import { GROUP_COLORS } from './config.js';

const requested = new URLSearchParams(location.search).get('dataset') ?? '';
export const DATASET = /^(history\/[a-z0-9-]+)?$/.test(requested) ? requested : '';
export const dataUrl = (file) => `data/${DATASET ? `${DATASET}/` : ''}${file}`;

export async function loadSatellites() {
  const res = await fetch(dataUrl('leo_objects.json'));
  if (!res.ok) throw new Error(`Dataset "${DATASET || 'live'}" not found (HTTP ${res.status}).`);
  const data = await res.json();
  // validFrom/validTo (Unix ms): the object is only shown and screened inside this window
  // (+-validity_days around its element epoch, never before its launch or after its decay).
  const sats = data.objects.map(([group, values, validFrom, validTo]) => {
    const omm = Object.fromEntries(data.omm_fields.map((f, k) => [f, values[k]]));
    return { norad: omm.NORAD_CAT_ID, name: omm.OBJECT_NAME, group, omm, validFrom, validTo };
  });
  const groupNames = Object.keys(GROUP_COLORS).filter(g => sats.some(s => s.group === g));
  return { sats, groupNames, generatedUtc: data.generated_utc, clockRange: data.clock_range_ms,
    validityDays: data.validity_days, event: data.event ?? null };
}

// The replay's prediction report (src/history.py), if any.
export async function loadReport() {
  if (!DATASET) return null;
  try {
    const res = await fetch(dataUrl('report.json'));
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

// SGP4 record for one object; the Python screener initialises the same OMM values.
export const makeSatrec = (s) => window.satellite.json2satrec(s.omm);
