// Three.js setup: renderer, camera, controls, the textured Earth, atmosphere glow and stars.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EARTH_RADIUS_KM, EARTH_TEXTURE_URL, SCENE_UNITS_PER_KM } from './config.js';

const EARTH_R = EARTH_RADIUS_KM * SCENE_UNITS_PER_KM;

export function createScene(container) {
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(innerWidth, innerHeight);
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, innerWidth / innerHeight, 0.01, 2000);
  camera.position.set(0, 9, 24);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.minDistance = EARTH_R * 1.05;
  controls.maxDistance = 90;

  const earth = createEarth();
  scene.add(earth, createAtmosphere(), createStars());

  addEventListener('resize', () => {
    camera.aspect = innerWidth / innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(innerWidth, innerHeight);
  });

  return { renderer, scene, camera, controls, earth };
}

// Plain blue sphere until the texture arrives. Rotate it by GMST (rotation.y) to match ECI positions.
function createEarth() {
  const earth = new THREE.Mesh(
    new THREE.SphereGeometry(EARTH_R, 128, 64),
    new THREE.MeshBasicMaterial({ color: 0x1d3b66 }),
  );
  new THREE.TextureLoader().load(EARTH_TEXTURE_URL, (tex) => {
    tex.colorSpace = THREE.SRGBColorSpace;
    earth.material.map = tex;
    earth.material.color.set(0xffffff);
    earth.material.needsUpdate = true;
  });
  return earth;
}

function createAtmosphere() {
  return new THREE.Mesh(
    new THREE.SphereGeometry(EARTH_R * 1.02, 64, 32),
    new THREE.MeshBasicMaterial({ color: 0x4ea8ff, transparent: true, opacity: 0.08, side: THREE.BackSide }),
  );
}

function createStars(count = 3000) {
  const p = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const v = new THREE.Vector3().randomDirection().multiplyScalar(600 + Math.random() * 300);
    p.set([v.x, v.y, v.z], i * 3);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(p, 3));
  return new THREE.Points(g, new THREE.PointsMaterial({ color: 0x8090a8, size: 1.2, sizeAttenuation: false }));
}
