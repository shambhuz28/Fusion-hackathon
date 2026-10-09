// All satellites drawn as one THREE.Points cloud; positions and colours are updated in place.

import * as THREE from 'three';
import { altitudeColor, groupColors } from './colors.js';
import { altitudeKm, toScene } from './orbit.js';

export function createSatelliteLayer(sats) {
  const n = sats.length;
  const positions = new Float32Array(n * 3);
  const colors = new Float32Array(n * 3);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3).setUsage(THREE.DynamicDrawUsage));
  geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));

  const points = new THREE.Points(geometry, new THREE.PointsMaterial({
    size: 0.095, map: createDotTexture(), vertexColors: true, transparent: true, alphaTest: 0.35,
  }));
  points.frustumCulled = false;

  const v3 = new THREE.Vector3();
  const tmpColor = new THREE.Color();

  // Extrapolate from the worker's last SGP4 state with its velocity (error < ~2 km at 1000x).
  function updatePositions(state) {
    const { latest, simTime, visibleGroups } = state;
    if (!latest) return;
    const k = (simTime - latest.time) / 1000, p = latest.pos, v = latest.vel;
    for (let i = 0; i < n; i++) {
      const j = i * 3;
      if (Number.isNaN(p[j]) || !visibleGroups.has(sats[i].group)) {
        positions[j] = positions[j + 1] = positions[j + 2] = 0;  // hidden inside the Earth
        continue;
      }
      toScene(p[j] + v[j] * k, p[j + 1] + v[j + 1] * k, p[j + 2] + v[j + 2] * k, v3);
      positions[j] = v3.x; positions[j + 1] = v3.y; positions[j + 2] = v3.z;
    }
    geometry.attributes.position.needsUpdate = true;
  }

  function updateColors(state) {
    const { latest, colorMode } = state;
    for (let i = 0; i < n; i++) {
      if (colorMode === 'group' || !latest) {
        tmpColor.copy(groupColors[sats[i].group]);
      } else {
        const j = i * 3, p = latest.pos;
        altitudeColor(altitudeKm(p[j], p[j + 1], p[j + 2]), tmpColor);
      }
      colors.set([tmpColor.r, tmpColor.g, tmpColor.b], i * 3);
    }
    geometry.attributes.color.needsUpdate = true;
  }

  return { points, updatePositions, updateColors };
}

function createDotTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const ctx = c.getContext('2d');
  const grd = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  grd.addColorStop(0, 'rgba(255,255,255,1)');
  grd.addColorStop(0.5, 'rgba(255,255,255,0.9)');
  grd.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = grd;
  ctx.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}
