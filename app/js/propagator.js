// Runs SGP4 for every satellite in a Web Worker (sim-worker.js), in a request/response loop:
// each result immediately triggers the next request for the current simulation time.

export function startPropagator(sats, state, { onReady, onState }) {
  const worker = new Worker(new URL('./sim-worker.js', import.meta.url));
  const requestStep = () => worker.postMessage({ type: 'step', time: state.simTime });

  worker.onmessage = (e) => {
    const msg = e.data;
    if (msg.type === 'ready') {
      onReady();
      requestStep();
    } else if (msg.type === 'state') {
      state.latest = msg;
      onState(msg);
      requestStep();
    }
  };
  worker.postMessage({
    type: 'init',
    omms: sats.map(s => s.omm),
    validFrom: Float64Array.from(sats, s => s.validFrom),
    validTo: Float64Array.from(sats, s => s.validTo),
  });
}
