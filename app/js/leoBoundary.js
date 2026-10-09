// LEO boundary: a translucent shell at 2,000 km altitude with a faint lat/long grid and a label.

import * as THREE from 'three';
import { EARTH_RADIUS_KM, LEO_MAX_ALT_KM, SCENE_UNITS_PER_KM } from './config.js';

export function createLeoBoundary() {
  const r = (EARTH_RADIUS_KM + LEO_MAX_ALT_KM) * SCENE_UNITS_PER_KM;
  const group = new THREE.Group();

  group.add(new THREE.Mesh(
    new THREE.SphereGeometry(r, 96, 48),
    new THREE.MeshBasicMaterial({ color: 0x4ea8ff, transparent: true, opacity: 0.045, depthWrite: false, side: THREE.DoubleSide }),
  ));

  const grid = [];
  for (const latDeg of [-60, -30, 30, 60]) {
    const lat = latDeg * Math.PI / 180;
    grid.push(...circle(a => new THREE.Vector3(r * Math.cos(lat) * Math.cos(a), r * Math.sin(lat), r * Math.cos(lat) * Math.sin(a))));
  }
  for (let lonDeg = 0; lonDeg < 180; lonDeg += 30) {
    const lon = lonDeg * Math.PI / 180;
    grid.push(...circle(a => new THREE.Vector3(r * Math.cos(a) * Math.cos(lon), r * Math.sin(a), r * Math.cos(a) * Math.sin(lon))));
  }
  group.add(lines(grid, 0.16));
  group.add(lines(circle(a => new THREE.Vector3(r * Math.cos(a), 0, r * Math.sin(a))), 0.55));  // equator

  const label = createLabel(`LEO boundary · ${LEO_MAX_ALT_KM.toLocaleString()} km`);
  label.position.set(0, r + 0.6, 0);
  group.add(label);
  return group;
}

// Segment endpoint pairs for a closed circle; pointAt(angle) returns a point on it.
function circle(pointAt, segments = 128) {
  const pts = [];
  for (let k = 0; k < segments; k++) pts.push(pointAt(2 * Math.PI * k / segments), pointAt(2 * Math.PI * (k + 1) / segments));
  return pts;
}

function lines(points, opacity) {
  return new THREE.LineSegments(
    new THREE.BufferGeometry().setFromPoints(points),
    new THREE.LineBasicMaterial({ color: 0x6fb8ff, transparent: true, opacity, depthWrite: false }),
  );
}

function createLabel(text) {
  const c = document.createElement('canvas');
  c.width = 512; c.height = 64;
  const ctx = c.getContext('2d');
  ctx.font = '600 30px system-ui, sans-serif';
  ctx.fillStyle = 'rgba(160, 205, 255, 0.95)';
  ctx.textAlign = 'center';
  ctx.fillText(text, 256, 42);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
  sprite.scale.set(6, 0.75, 1);
  return sprite;
}
