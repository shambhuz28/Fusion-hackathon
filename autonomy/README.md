# Crosslink simulation: satellites avoid collisions by talking to each other

A second, separate simulation. Instead of a ground screening of the whole catalogue, every satellite with a radio
shares its orbit data with the satellites around it, and the satellites decide on their own.

```bash
autonomy/run.sh            # first time: simulate the network (~1 min for 6 h), then serve the page
autonomy/run.sh --rerun    # simulate again (e.g. after ./run.sh refreshed the satellite data)
HOURS=12 autonomy/run.sh --rerun
```

Open the URL it prints (default `http://127.0.0.1:8770/autonomy/`). It uses the main project's data, so run
`./run.sh` once first.

## What happens

1. **Broadcast.** Every 10 s each satellite broadcasts: position (and altitude), velocity, orbital elements (its
   predicted path), fuel level and position uncertainty.
2. **Range.** Only satellites within **50 km** (a 100 km bubble) receive it; links are drawn in cyan.
3. **Check.** A receiver that gets new data (not heard from that satellite in the last hour) predicts the sender's
   closest approach to itself over the next **24 h**, and computes the collision probability with the two
   broadcast uncertainties.
4. **Discard or keep.** No risk: the data is **discarded** (over 99.99 % of messages). Pc >= 1e-5 or a predicted
   miss under 200 m: it is **kept** as a threat and monitored.
5. **Avoid.** When the satellites can collide (Pc >= 1e-4 or miss under 200 m), both hold the same data and apply
   the same rule, so they agree without further messages on who flies a temporary detour (it rejoins its original
   orbit about 10 minutes after the encounter, `src/maneuver.py`; the safety check only uses satellites it has
   itself heard from). The choice uses **fuel level, velocity and mass**:
   - each satellite's own detour is planned: its velocity and direction relative to the encounter set the
     delta-v it needs;
   - its mass and thruster turn that into propellant, `m * (1 - exp(-dv / (Isp * g0)))`;
   - the satellite for which the detour costs the **smallest share of its remaining fuel** manoeuvres (ties: lower
     NORAD ID). One that would drop below its 5 % reserve cannot; if neither can, the threat stays a **warning**.

   In the 6 h run this picks a different satellite than "more fuel wins" in 9 of 46 avoidances, e.g. an 800 kg
   Starlink with electric thrusters (0.63 % of its fuel) instead of a 45 kg cold-gas satellite with more fuel
   (11.7 %). All LEO satellites move at ~7.6 km/s, so velocity matters through its direction (the delta-v needed),
   not its size.
6. **Silent objects.** Debris and rocket bodies have no radio: the network cannot see them.

## Files

| File | Role |
| --- | --- |
| `engine.py` | Simulates the network over a window and writes `data/run.json` |
| `index.html`, `css/style.css` | The page (reuses the main app's styles) |
| `js/main.js` | Loads data, time controls, picking, render loop |
| `js/network.js` | Radio links within range (spatial hash) and satellite colours by state |
| `js/focus.js` | 3D view of one threat: both satellites, orbits, closest approach, the detour |
| `js/panels.js` | Network statistics, message feed, threats list, detail cards |
| `serve.py`, `run.sh` | Serve the project root so the page can use `../app/js` and `../app/data` |

The 3D scene, SGP4 worker, point cloud and orbit paths are imported from `../app/js`.

## Results of a 6 h run (10 Oct 2026 00:00-06:00 UTC)

| | |
| --- | --- |
| Satellites with radio / silent objects | 15,874 / 2,676 |
| Messages received | 3,581,156 |
| New data assessed / discarded | 1,327,130 / 1,326,832 |
| Threats kept | 91: 46 avoided, 33 monitored, 12 warnings |

**Coverage check against the ground screening:** of 241 critical encounters between two radio satellites inside the
window, the network detected 56 at least 10 minutes ahead. 162 pairs first came within 50 km only during the
approach itself (seconds before closest approach at 7-15 km/s), 3 never came within range, and 20 were judged safe
with the more precise onboard data. Crosslink avoidance works when two satellites met on an earlier orbit; a
first meeting at high speed is seen too late with a 100 km bubble. Longer range (`--link-km`) or relaying data
across several hops would widen it.

## Assumptions

- Fuel levels (8 % of satellites start below the 5 % reserve) and position uncertainties (GNSS-class, 30-150 m
  radial, 2.5x along-track) are simulated per satellite, seeded by NORAD ID: TLEs carry neither.
- Mass and thruster are assumed per type (TLEs carry neither): Starlink 300 kg (v1) / 800 kg (v2 mini), OneWeb
  150 kg, Kuiper 600 kg, all electric (Isp 1500 s); Qianfan 300 kg, Guowang 500 kg, Iridium 860 kg (Isp 220 s);
  Planet/Spire cubesats 5 kg (cold gas, Isp 70 s); ISS 420 t, Tiangong 100 t; others a seeded 5-1,500 kg (cold gas
  under 50 kg, else Isp 220 s). Tanks hold 5 % of the satellite's mass. Burns are impulsive, although electric
  thrusters would really spread them over minutes.
- The receiver's 24 h prediction is the same SGP4 closest-approach computation as `src/screen_conjunctions.py` on
  the elements the sender broadcasts, so the engine looks it up from that verified screening
  (`app/data/conjunctions.json`) instead of recomputing it for each of ~10^6 contacts. The risk decision is
  recomputed with the broadcast uncertainties.
- The detour planner sizes the burn with the ground tool's (TLE-level) uncertainty, so it is conservative.
