// Mutable simulation state shared between modules.

export const state = {
  simTime: Date.now(),      // simulated UTC time (ms)
  speed: 1,                 // simulated seconds per real second (0 = paused)
  colorMode: 'group',       // 'group' | 'altitude'
  latest: null,             // last propagated state from the worker: {time, pos, vel}
  visibleGroups: new Set(), // constellation groups currently shown
};
