// Orbit maths and frame conversion helpers (no rendering, no DOM).

import { EARTH_RADIUS_KM, MU_KM3_S2, SCENE_UNITS_PER_KM } from './config.js';

const S = SCENE_UNITS_PER_KM;

// ECI/TEME (z up, km) -> scene (y up). Greenwich lies on +x when the Earth's rotation.y = GMST.
export const toScene = (x, y, z, out) => out.set(x * S, z * S, -y * S);

// satrec.no is the SGP4 mean motion in rad/min.
export const periodMinutes = (rec) => 2 * Math.PI / rec.no;

export function perigeeApogeeKm(rec) {
  const n = rec.no / 60;                        // rad/s
  const a = Math.cbrt(MU_KM3_S2 / (n * n));     // Kepler's third law
  return [a * (1 - rec.ecco) - EARTH_RADIUS_KM, a * (1 + rec.ecco) - EARTH_RADIUS_KM];
}

// Days between the TLE epoch and `timeMs`.
export function elementAgeDays(rec, timeMs) {
  const year = rec.epochyr < 57 ? 2000 + rec.epochyr : 1900 + rec.epochyr;
  const epochMs = Date.UTC(year, 0, 1) + (rec.epochdays - 1) * 86400000;
  return (timeMs - epochMs) / 86400000;
}

// WGS84 height above the ellipsoid (as in the positions CSVs and the info card). A rotation about
// the z axis does not change it, so ECI/TEME coordinates can be used directly.
const WGS84_A = 6378.137, WGS84_E2 = (1 / 298.257223563) * (2 - 1 / 298.257223563);
export function altitudeKm(x, y, z) {
  const p = Math.hypot(x, y);
  let lat = Math.atan2(z, p * (1 - WGS84_E2)), n = WGS84_A, h = 0;
  for (let k = 0; k < 4; k++) {
    n = WGS84_A / Math.sqrt(1 - WGS84_E2 * Math.sin(lat) ** 2);
    h = p / Math.cos(lat) - n;
    lat = Math.atan2(z, p * (1 - WGS84_E2 * n / (n + h)));
  }
  return h;
}
