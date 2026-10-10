// 3D view of one threat: both satellites, their orbits, the closest-approach point, the line between them, and
// the temporary detour (green dashed) that the manoeuvring satellite flies before rejoining its orbit.

import * as THREE from 'three';
import { toScene } from '../../app/js/orbit.js';
import { createOrbitPath } from '../../app/js/orbitPath.js';
import { planOffset } from './network.js';

const sat = window.satellite;
const COLOR_A = 0xffd84d, COLOR_B = 0xff4d4d, PATH = 0x3ddc97;

export function createFocus({ scene, camera, satByNorad }) {
  const group = new THREE.Group();
  const marker = (c) => new THREE.Mesh(new THREE.SphereGeometry(0.03, 16, 12), new THREE.MeshBasicMaterial({ color: c }));
  const markerA = marker(COLOR_A), markerB = marker(COLOR_B);
  const tcaMark = new THREE.Mesh(new THREE.SphereGeometry(0.06, 20, 16),
    new THREE.MeshBasicMaterial({ color: 0xff3b30, transparent: true, opacity: 0.4, depthWrite: false }));
  const sepGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
  const sep = new THREE.Line(sepGeo, new THREE.LineBasicMaterial({ color: 0xffffff }));
  const path = new THREE.Line(new THREE.BufferGeometry(),
    new THREE.LineDashedMaterial({ color: PATH, dashSize: 0.04, gapSize: 0.02 }));
  const orbitA = createOrbitPath(COLOR_A, 0.7), orbitB = createOrbitPath(COLOR_B, 0.7);
  group.add(markerA, markerB, tcaMark, sep, path);
  group.visible = false;
  scene.add(group, orbitA.line, orbitB.line);

  let threat = null, recA = null, recB = null, scale = 1;

  const shifted = (p, o, f) => (o ? { x: p.x + o[0] * f, y: p.y + o[1] * f, z: p.z + o[2] * f } : p);

  function drawPath() {
    path.visible = threat?.status === 'avoided';
    if (!path.visible) return;
    const plan = threat.plan;
    const rec = threat.manoeuvring === threat.a ? recA : recB;
    const pts = [];
    plan.path.offset_km.forEach((o, k) => {
      const pos = sat.propagate(rec, new Date(plan.path.t0_ms + k * plan.path.step_ms))?.position;
      if (pos) pts.push(toScene(pos.x + o[0] * scale, pos.y + o[1] * scale, pos.z + o[2] * scale, new THREE.Vector3()));
    });
    path.geometry.dispose();
    path.geometry = new THREE.BufferGeometry().setFromPoints(pts);
    path.computeLineDistances();
  }

  function show(t, exaggeration) {
    threat = t;
    scale = exaggeration;
    recA = sat.json2satrec(satByNorad.get(t.a).omm);
    recB = sat.json2satrec(satByNorad.get(t.b).omm);
    const when = new Date(t.tca_ms);
    const pa = sat.propagate(recA, when)?.position, pb = sat.propagate(recB, when)?.position;
    if (pa && pb) {
      toScene((pa.x + pb.x) / 2, (pa.y + pb.y) / 2, (pa.z + pb.z) / 2, tcaMark.position);
      camera.position.copy(tcaMark.position.clone().normalize().multiplyScalar(tcaMark.position.length() + 2.2));
    }
    orbitA.show(recA, t.tca_ms);
    orbitB.show(recB, t.tca_ms);
    group.visible = true;
    drawPath();
  }

  function hide() {
    threat = null;
    group.visible = false;
    orbitA.hide();
    orbitB.hide();
  }

  // Per frame: positions at simTime (the manoeuvring one on its detour); returns the true distance in km.
  function update(simTime, now) {
    if (!threat) return null;
    const date = new Date(simTime);
    let pa = sat.propagate(recA, date)?.position, pb = sat.propagate(recB, date)?.position;
    if (!pa || !pb) return null;
    const off = threat.status === 'avoided' ? planOffset(threat.plan, simTime) : null;
    const offA = threat.manoeuvring === threat.a ? off : null, offB = threat.manoeuvring === threat.b ? off : null;
    const trueA = shifted(pa, offA, 1), trueB = shifted(pb, offB, 1);
    pa = shifted(pa, offA, scale); pb = shifted(pb, offB, scale);
    toScene(pa.x, pa.y, pa.z, markerA.position);
    toScene(pb.x, pb.y, pb.z, markerB.position);
    sepGeo.attributes.position.setXYZ(0, markerA.position.x, markerA.position.y, markerA.position.z);
    sepGeo.attributes.position.setXYZ(1, markerB.position.x, markerB.position.y, markerB.position.z);
    sepGeo.attributes.position.needsUpdate = true;
    orbitA.update(simTime);
    orbitB.update(simTime);
    tcaMark.scale.setScalar(1 + 0.35 * Math.sin(now / 200));
    return Math.hypot(trueA.x - trueB.x, trueA.y - trueB.y, trueA.z - trueB.z);
  }

  return {
    show, hide, update,
    setScale(f) { scale = f; drawPath(); },
    current: () => threat,
  };
}
