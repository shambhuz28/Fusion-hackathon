// Entry point: loads the data, wires the modules together and runs the render loop.

import { loadSatellites } from './data.js';
import { state } from './state.js';
import { createScene } from './scene.js';
import { createLeoBoundary } from './leoBoundary.js';
import { createSatelliteLayer } from './satellites.js';
import { startPropagator } from './propagator.js';
import { createSelection } from './selection.js';
import { createPanel } from './ui.js';
import { createConjunctionView, loadConjunctions } from './conjunctions.js';

const sat = window.satellite;

const [{ sats, groupNames, generatedUtc }, conjData] = await Promise.all([loadSatellites(), loadConjunctions()]);
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
  onSelect: (s) => {
    panel.showGroup(s.group);
    conjunctions?.close();  // the satellite card and the conjunction analysis share the bottom-right corner
    conjunctions?.setFocus(s);  // list this satellite's conjunctions and show its debris check
  },
});

// Conjunction panel (only when src/screen_conjunctions.py has produced data/conjunctions.json).
const conjunctions = conjData && createConjunctionView({
  data: conjData, scene, camera, sats, state,
  setSpeed: panel.setSpeed,
  onAnalyze: () => selection.select(-1),
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
  const prevSimTime = state.simTime;
  state.simTime += (now - lastFrame) * state.speed;
  lastFrame = now;

  panel.updateClock();
  earth.rotation.y = sat.gstime(new Date(state.simTime));  // keeps ground locations under the ECI positions
  layer.updatePositions(state);
  selection.update();
  conjunctions?.update(prevSimTime);
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
