// Propagates every satellite with SGP4 for the time the main thread asks for.
// Positions/velocities are TEME (≈ ECI) in km and km/s; NaN marks an object that has no valid
// state at that time (outside its validity window, or SGP4 failed, e.g. decayed).
importScripts('https://cdn.jsdelivr.net/npm/satellite.js@6.0.2/dist/satellite.min.js');

let recs = [];
let validFrom = null, validTo = null;

onmessage = (e) => {
  const msg = e.data;
  if (msg.type === 'init') {
    recs = msg.omms.map((omm) => satellite.json2satrec(omm));
    validFrom = msg.validFrom;
    validTo = msg.validTo;
    postMessage({ type: 'ready', n: recs.length });
  } else if (msg.type === 'step') {
    const date = new Date(msg.time);
    const pos = new Float32Array(recs.length * 3);
    const vel = new Float32Array(recs.length * 3);
    for (let i = 0; i < recs.length; i++) {
      const j = i * 3;
      if (msg.time < validFrom[i] || msg.time > validTo[i]) {
        pos[j] = pos[j + 1] = pos[j + 2] = NaN;
        continue;
      }
      let pv = null;
      try { pv = satellite.propagate(recs[i], date); } catch { /* one bad record must not stop the others */ }
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
