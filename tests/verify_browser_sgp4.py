"""Checks that the browser's SGP4 (satellite.js json2satrec, as loaded by app/index.html) gives the same
positions and velocities as the Python screener for every object in app/data/leo_objects.json.

Needs Node.js and network access to the CDN.

Usage:
    python tests/verify_browser_sgp4.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import _venv  # noqa: E402,F401  (re-runs with .venv/bin/python if packages are missing)

import json
import re
import subprocess
import tempfile

from sgp4.api import jday

import screen_conjunctions as sc  # noqa: E402

NODE_SCRIPT = r"""
const fs = require('fs');
const [satUrl, objectsPath, refPath] = process.argv.slice(2);
(async () => {
  const module = { exports: {} };
  new Function('module', 'exports', await (await fetch(satUrl)).text())(module, module.exports);
  const satellite = module.exports;
  const data = JSON.parse(fs.readFileSync(objectsPath));
  const ref = JSON.parse(fs.readFileSync(refPath));
  let worstR = 0, worstV = 0, n = 0, failures = 0;
  data.objects.forEach(([, values], i) => {
    const omm = Object.fromEntries(data.omm_fields.map((f, k) => [f, values[k]]));
    const rec = satellite.json2satrec(omm);
    for (const [ms, states] of ref) {
      const r = states[i];
      const pv = satellite.propagate(rec, new Date(ms));
      if (!r || !pv) { if (!r !== !pv) failures++; continue; }
      const dr = Math.hypot(pv.position.x - r[0], pv.position.y - r[1], pv.position.z - r[2]);
      const dv = Math.hypot(pv.velocity.x - r[3], pv.velocity.y - r[4], pv.velocity.z - r[5]);
      worstR = Math.max(worstR, dr); worstV = Math.max(worstV, dv); n++;
    }
  });
  console.log(JSON.stringify({ n, worstR, worstV, failures }));
})();
"""


def main():
    html = (ROOT / "app" / "index.html").read_text()
    sat_url = re.search(r"https://cdn\.jsdelivr\.net/npm/satellite\.js@[^\"']+", html).group(0)
    objs, sats = sc.load_objects()

    # Whole-millisecond times (the browser clock resolution) spread over the 7-day window.
    times_ms = [1791561637123 + k * 86400000 + k * 3_600_000 for k in range(8)]
    ref = []
    for ms in times_ms:
        jd, fr = jday(1970, 1, 1, 0, 0, 0)
        fr += ms / 86400000.0
        states = []
        for s in sats:
            e, r, v = s.sgp4(jd, fr)
            states.append(None if e else [*r, *v])
        ref.append([ms, states])

    with tempfile.TemporaryDirectory() as tmp:
        ref_path = Path(tmp) / "ref.json"
        ref_path.write_text(json.dumps(ref))
        script = Path(tmp) / "check.js"
        script.write_text(NODE_SCRIPT)
        out = subprocess.run(["node", str(script), sat_url, str(ROOT / "app" / "data" / "leo_objects.json"), str(ref_path)],
                             capture_output=True, text=True, check=True).stdout
    res = json.loads(out)
    ok = res["worstR"] < 0.002 and res["worstV"] < 2e-6 and res["failures"] == 0
    print(f"[{'PASS' if ok else 'FAIL'}] browser SGP4 ({sat_url.split('/npm/')[1].split('/')[0]}) vs Python screener: "
          f"{res['n']} states ({len(sats)} objects x {len(times_ms)} times over 7 days), "
          f"max position diff {res['worstR'] * 1000:.3f} m, max velocity diff {res['worstV'] * 1e6:.3f} mm/s, "
          f"error mismatches {res['failures']}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
