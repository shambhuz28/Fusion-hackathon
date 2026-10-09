// Satellite colouring: by constellation group or by altitude.

import * as THREE from 'three';
import { ALTITUDE_STOPS_KM, GROUP_COLORS } from './config.js';

export const groupColors = Object.fromEntries(
  Object.entries(GROUP_COLORS).map(([group, hex]) => [group, new THREE.Color(hex)]),
);

// Position (0..1) of an altitude on the colour scale; each pair of stops gets an equal share.
function altitudeT(altKm) {
  const stops = ALTITUDE_STOPS_KM;
  if (altKm <= stops[0]) return 0;
  for (let k = 1; k < stops.length; k++) {
    if (altKm <= stops[k]) return (k - 1 + (altKm - stops[k - 1]) / (stops[k] - stops[k - 1])) / (stops.length - 1);
  }
  return 1;
}

const hue = t => t * 0.78;  // red (low) -> violet (high)

export function altitudeColor(altKm, out) {
  return out.setHSL(hue(altitudeT(altKm)), 0.85, 0.6, THREE.SRGBColorSpace);
}

// CSS gradient matching altitudeColor, for the legend bar.
export function altitudeGradientCss() {
  const stops = Array.from({ length: 11 }, (_, k) => `hsl(${hue(k / 10) * 360} 85% 60%)`);
  return `linear-gradient(90deg, ${stops.join(', ')})`;
}
