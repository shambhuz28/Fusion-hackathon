// The selected satellite: highlight marker, velocity arrow, one-period orbit path, click picking
// and the info card.

import * as THREE from 'three';
import { EARTH_RADIUS_KM, SCENE_UNITS_PER_KM } from './config.js';
import { elementAgeDays, perigeeApogeeKm, periodMinutes, toScene } from './orbit.js';
import { createOrbitPath } from './orbitPath.js';
import { makeSatrec } from './data.js';

const sat = window.satellite;

export function createSelection({ scene, camera, renderer, points, sats, state, onSelect }) {
  const marker = new THREE.Mesh(new THREE.SphereGeometry(0.07, 16, 12), new THREE.MeshBasicMaterial({ color: 0xffffff }));
  const arrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(), 1.4, 0xffd84d, 0.28, 0.14);
  const orbit = createOrbitPath();
  scene.add(marker, arrow, orbit.line);

  const infoEl = document.getElementById('info');
  document.getElementById('close').onclick = () => select(-1);

  let rec = null;          // satellite.js satrec of the selected satellite
  let lastInfo = 0;
  setVisible(false);

  function setVisible(v) {
    marker.visible = arrow.visible = v;
    infoEl.style.display = v ? 'block' : 'none';
    if (!v) orbit.hide();
  }

  // i < 0 clears the selection; focus turns the camera towards the satellite.
  function select(i, focus = false) {
    if (i < 0) {
      rec = null;
      setVisible(false);
      return;
    }
    const s = sats[i];
    rec = makeSatrec(s);
    document.getElementById('infoName').textContent = s.name;
    document.getElementById('infoSub').textContent = `NORAD ${s.norad} · ${s.group}`;
    setVisible(true);
    orbit.show(rec, state.simTime);
    onSelect(s);
    if (focus) focusCamera();
  }

  // Look from between the satellite and its orbit normal, so the whole orbit shows as a ring.
  function focusCamera() {
    const pv = sat.propagate(rec, new Date(state.simTime));
    if (!pv?.position) return;
    const r = toScene(pv.position.x, pv.position.y, pv.position.z, new THREE.Vector3()).normalize();
    const v = toScene(pv.velocity.x, pv.velocity.y, pv.velocity.z, new THREE.Vector3());
    const normal = new THREE.Vector3().crossVectors(r, v).normalize();
    camera.position.copy(r.add(normal.multiplyScalar(0.9)).normalize().multiplyScalar(Math.max(camera.position.length(), 20)));
  }

  // Called every frame: exact SGP4 state for the selected satellite.
  function update() {
    if (!rec) return;
    const date = new Date(state.simTime);
    const pv = sat.propagate(rec, date);
    if (!pv?.position) return;
    const p = toScene(pv.position.x, pv.position.y, pv.position.z, new THREE.Vector3());
    const v = toScene(pv.velocity.x, pv.velocity.y, pv.velocity.z, new THREE.Vector3());
    marker.position.copy(p);
    arrow.position.copy(p);
    arrow.setDirection(v.normalize());
    orbit.update(state.simTime);

    const now = performance.now();
    if (now - lastInfo > 150) {
      lastInfo = now;
      renderInfo(pv, date);
    }
  }

  function renderInfo(pv, date) {
    const geo = sat.eciToGeodetic(pv.position, sat.gstime(date));
    const speed = Math.hypot(pv.velocity.x, pv.velocity.y, pv.velocity.z);
    const [perigee, apogee] = perigeeApogeeKm(rec);
    const rows = [
      ['Altitude', `${geo.height.toFixed(1)} km`],
      ['Speed', `${speed.toFixed(3)} km/s`],
      ['', `${(speed * 3600).toLocaleString(undefined, { maximumFractionDigits: 0 })} km/h`],
      ['Latitude', `${sat.degreesLat(geo.latitude).toFixed(3)}°`],
      ['Longitude', `${sat.degreesLong(geo.longitude).toFixed(3)}°`],
      ['Inclination', `${(rec.inclo * 180 / Math.PI).toFixed(2)}°`],
      ['Period', `${periodMinutes(rec).toFixed(1)} min`],
      ['Perigee / apogee', `${perigee.toFixed(0)} / ${apogee.toFixed(0)} km`],
      ['Eccentricity', rec.ecco.toFixed(5)],
      ['Element age', `${elementAgeDays(rec, state.simTime).toFixed(2)} days`],
    ];
    document.getElementById('infoBody').innerHTML = rows.map(([k, val]) => `<dt>${k}</dt><dd>${val}</dd>`).join('');
  }

  // Click picking: ignores drags and satellites hidden behind the Earth.
  const raycaster = new THREE.Raycaster();
  const earthSphere = new THREE.Sphere(new THREE.Vector3(), EARTH_RADIUS_KM * SCENE_UNITS_PER_KM);
  let downAt = null;
  renderer.domElement.addEventListener('pointerdown', (e) => { downAt = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener('pointerup', (e) => {
    if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 5) return;
    const ndc = new THREE.Vector2((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    raycaster.params.Points.threshold = 0.012 * camera.position.length();
    const earthHit = raycaster.ray.intersectSphere(earthSphere, new THREE.Vector3());
    const maxDist = earthHit ? earthHit.distanceTo(raycaster.ray.origin) : Infinity;
    const hits = raycaster.intersectObject(points)
      .filter(h => h.distance < maxDist && state.visibleGroups.has(sats[h.index].group))
      .sort((a, b) => a.distanceToRay - b.distanceToRay);
    if (hits.length) select(hits[0].index);
  });

  return { select, update };
}
