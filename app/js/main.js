// Entry point: loads the data, wires the modules together and runs the render loop.

import { loadSatellites } from './data.js';
import { state } from './state.js';
import { createScene } from './scene.js';
import { createLeoBoundary } from './leoBoundary.js';
import { createSatelliteLayer } from './satellites.js';
import { startPropagator } from './propagator.js';
import { createSelection } from './selection.js';
import { createPanel } from './ui.js';

const sat = window.satellite;

const { sats, groupNames, generatedUtc } = await loadSatellites();
groupNames.forEach(g => state.visibleGroups.add(g));

const { renderer, scene, camera, controls, earth } = createScene(document.getElementById('scene'));
const leoBoundary = createLeoBoundary();
const layer = createSatelliteLayer(sats);
scene.add(leoBoundary, layer.points);

const panel = createPanel({
  sats, groupNames, generatedUtc, state,
  onColorModeChange: () => layer.updateColors(state),
  onLeoToggle: (visible) => { leoBoundary.visible = visible; },
  onSearchSelect: (i) => selection.select(i, true),
});

const selection = createSelection({
  scene, camera, renderer, points: layer.points, sats, state,
  onSelect: (s) => panel.showGroup(s.group),
});

let colored = false;
startPropagator(sats, state, {
  onReady: () => document.getElementById('loading').remove(),
  onState: () => {
    // Group colours are set once; altitude colours follow every new SGP4 state.
    if (!colored || state.colorMode === 'altitude') {
      layer.updateColors(state);
      colored = true;
    }
    panel.updateStats(true);
  },
});

let lastFrame = performance.now();
function frame(now) {
  state.simTime += (now - lastFrame) * state.speed;
  lastFrame = now;

  panel.updateClock();
  earth.rotation.y = sat.gstime(new Date(state.simTime));  // keeps ground locations under the ECI positions
  layer.updatePositions(state);
  selection.update();
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
