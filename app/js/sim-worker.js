// Propagates every satellite with SGP4 for the time the main thread asks for.
// Positions/velocities are TEME (≈ ECI) in km and km/s; NaN marks a failed propagation.
importScripts('https://cdn.jsdelivr.net/npm/satellite.js@6.0.2/dist/satellite.min.js');

let recs = [];

onmessage = (e) => {
  const msg = e.data;
  if (msg.type === 'init') {
    recs = msg.omms.map((omm) => satellite.json2satrec(omm));
    postMessage({ type: 'ready', n: recs.length });
  } else if (msg.type === 'step') {
    const date = new Date(msg.time);
    const pos = new Float32Array(recs.length * 3);
    const vel = new Float32Array(recs.length * 3);
    for (let i = 0; i < recs.length; i++) {
      const pv = satellite.propagate(recs[i], date);   // null when SGP4 fails (e.g. decayed)
      const j = i * 3;
      if (pv?.position) {
        pos[j] = pv.position.x; pos[j + 1] = pv.position.y; pos[j + 2] = pv.position.z;
        vel[j] = pv.velocity.x; vel[j + 1] = pv.velocity.y; vel[j + 2] = pv.velocity.z;
      } else {
        pos[j] = pos[j + 1] = pos[j + 2] = NaN;
      }
    }
    postMessage({ type: 'state', time: msg.time, pos, vel }, [pos.buffer, vel.buffer]);
  }
};
