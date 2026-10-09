// Loads the LEO objects exported by src/export_sim_data.py (full-precision OMM elements).

import { DATA_URL, GROUP_COLORS } from './config.js';

export async function loadSatellites() {
  const data = await (await fetch(DATA_URL)).json();
  const sats = data.objects.map(([group, values]) => {
    const omm = Object.fromEntries(data.omm_fields.map((f, k) => [f, values[k]]));
    return { norad: omm.NORAD_CAT_ID, name: omm.OBJECT_NAME, group, omm };
  });
  const groupNames = Object.keys(GROUP_COLORS).filter(g => sats.some(s => s.group === g));
  return { sats, groupNames, generatedUtc: data.generated_utc };
}

// SGP4 record for one object; the Python screener initialises the same OMM values.
export const makeSatrec = (s) => window.satellite.json2satrec(s.omm);
