// Entry point: loads the data, wires the modules together and runs the render loop.

import { loadReport, loadSatellites } from './data.js';
import { initDatasetSelector, showEventBanner } from './replay.js';
import { state } from './state.js';
import { createScene } from './scene.js';
import { createLeoBoundary } from './leoBoundary.js';
import { createSatelliteLayer } from './satellites.js';
import { startPropagator } from './propagator.js';
import { createSelection } from './selection.js';
import { createPanel } from './ui.js';
import { createConjunctionView, loadConjunctions } from './conjunctions.js';
import { notify } from './notice.js';

const sat = window.satellite;

const [{ sats, groupNames, generatedUtc, clockRange, event }, conjData, report] =
  await Promise.all([loadSatellites(), loadConjunctions(), loadReport()]);
groupNames.forEach(g => state.visibleGroups.add(g));
// A replay starts an hour before the collision; live data starts now. Either way inside the valid range.
if (event) state.simTime = Date.parse(event.start) - 3600000;
state.simTime = Math.min(clockRange[1], Math.max(clockRange[0], state.simTime));
initDatasetSelector();

const { renderer, scene, camera, controls, earth } = createScene(document.getElementById('scene'));
const leoBoundary = createLeoBoundary();
const layer = createSatelliteLayer(sats);
scene.add(leoBoundary, layer.points);

const panel = createPanel({
  sats, groupNames, generatedUtc, clockRange, event, state,
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

// Conjunction panel: the screening from run.sh, replaced on demand when the clock leaves its window.
const conjunctions = createConjunctionView({
  data: conjData, scene, camera, sats, state, clockRange,
  setSpeed: panel.setSpeed,
  onAnalyze: () => selection.select(-1),
});

showEventBanner(event, report, {
  goToCollision: () => {
    const t = Date.parse(event.start);
    if (!conjunctions.analyzePair(event.norad[0], event.norad[1], t)) {
      panel.jumpTo(t - 600000);
      notify('This pair was not in the screening results (no approach under 5 km); showing the reported time.');
    }
  },
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

// Each step runs isolated: an error in one module is reported once and never stops the animation.
const reported = new Set();
function safe(name, fn) {
  try {
    fn();
  } catch (err) {
    if (!reported.has(name)) {
      reported.add(name);
      console.error(`${name} failed:`, err);
      notify(`Part of the display (${name}) hit an error and was skipped; see the browser console.`);
    }
  }
}

// Keep the clock inside the validity range of the loaded elements (pause at the edges).
function clampClock() {
  const [lo, hi] = clockRange;
  if (state.simTime >= lo && state.simTime <= hi) return;
  const atEnd = state.simTime > hi;
  state.simTime = atEnd ? hi : lo;
  if (state.speed !== 0) {
    panel.setSpeed(0);
    notify(`Paused: the loaded orbital elements are only valid until ${new Date(atEnd ? hi : lo).toISOString()
      .slice(0, 16).replace('T', ' ')} UTC. Run ./run.sh again later to load newer data.`, 10000);
  }
}

let lastFrame = performance.now();
function frame(now) {
  requestAnimationFrame(frame);   // schedule first, so the loop survives any error below
  const prevSimTime = state.simTime;
  state.simTime += Math.min(now - lastFrame, 250) * state.speed;   // cap the step after a stalled tab
  lastFrame = now;
  clampClock();

  safe('clock', () => panel.updateClock());
  safe('earth', () => { earth.rotation.y = sat.gstime(new Date(state.simTime)); });  // ground under ECI positions
  safe('satellites', () => layer.updatePositions(state));
  safe('selection', () => selection.update());
  safe('conjunctions', () => conjunctions.update(prevSimTime));
  safe('render', () => {
    controls.update();
    renderer.render(scene, camera);
  });
}
requestAnimationFrame(frame);
