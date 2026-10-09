// Loads the LEO satellite list exported by src/export_sim_data.py.

import { DATA_URL, GROUP_COLORS } from './config.js';

export async function loadSatellites() {
  const data = await (await fetch(DATA_URL)).json();
  const sats = data.satellites.map(([norad, name, group, line1, line2]) => ({ norad, name, group, line1, line2 }));
  const groupNames = Object.keys(GROUP_COLORS).filter(g => sats.some(s => s.group === g));
  return { sats, groupNames, generatedUtc: data.generated_utc };
}
