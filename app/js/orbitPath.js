// A one-period orbit path for a satrec, rebuilt as simulation time moves on.

import * as THREE from 'three';
import { periodMinutes, toScene } from './orbit.js';

const sat = window.satellite;
const STEPS = 240;

export function createOrbitPath(color = 0xffffff, opacity = 0.75) {
  const line = new THREE.Line(new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
  line.visible = false;
  let rec = null;
  let center = 0;

  // Path over one period centred on timeMs (inertial frame).
  function build(timeMs) {
    const T = periodMinutes(rec) * 60000, pts = [];
    center = timeMs;
    for (let k = 0; k <= STEPS; k++) {
      const pv = sat.propagate(rec, new Date(timeMs - T / 2 + (T * k) / STEPS));
      if (pv.position) pts.push(toScene(pv.position.x, pv.position.y, pv.position.z, new THREE.Vector3()));
    }
    line.geometry.dispose();
    line.geometry = new THREE.BufferGeometry().setFromPoints(pts);
  }

  return {
    line,
    show(newRec, timeMs) { rec = newRec; line.visible = true; build(timeMs); },
    hide() { rec = null; line.visible = false; },
    // Rebuild once the simulation has moved a quarter period away from the path's centre.
    update(timeMs) {
      if (rec && Math.abs(timeMs - center) > periodMinutes(rec) * 15000) build(timeMs);
    },
  };
}
