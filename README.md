<p align="center">
  <img src="docs/images/banner.png" alt="OrbitGuard AI – predict satellite collisions, rank the risk, plan the dodge" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Orbits-SGP4-2E86DE" alt="SGP4">
  <img src="https://img.shields.io/badge/AI-XGBoost-14A3A3" alt="XGBoost">
  <img src="https://img.shields.io/badge/3D-Three.js-000000?logo=threedotjs&logoColor=white" alt="Three.js">
  <img src="https://img.shields.io/badge/Data-CelesTrak%20%7C%20ESA%20Kelvins-E8590C" alt="Data">
  <img src="https://img.shields.io/badge/Fusion-2026%20Hackathon-1D3B6F" alt="Fusion 2026">
</p>

<p align="center">
  <b>OrbitGuard AI</b> watches every satellite and debris piece in Low Earth Orbit, warns about close approaches up to 7 days ahead,<br>
  uses AI to rank the dangerous ones first, and suggests the <b>smallest engine burn</b> that dodges them safely.
</p>

<p align="center">
  <a href="#-the-problem">Problem</a> •
  <a href="#-our-solution">Solution</a> •
  <a href="#-project-output">Output</a> •
  <a href="#-how-it-works--full-workflow">Workflow</a> •
  <a href="#-results">Results</a> •
  <a href="#-technologies">Tech</a> •
  <a href="#-datasets">Datasets</a> •
  <a href="#-run-it-yourself">Run it</a>
</p>

---

## The problem

> **Problem statement (Fusion 2026):** Low Earth Orbit is increasingly congested. Warnings of near-collisions between satellites and debris are growing faster than ground teams can check each one by hand.
> **Expected solution:** using public TLE orbit data, build a system that **predicts upcoming close approaches**, **ranks them by collision probability**, and **recommends an avoidance manoeuvre** (timing + Δv direction) for at least one event.

<table>
  <tr>
    <td width="50%"><img src="docs/images/debris_leo.jpg" alt="Objects in Low Earth Orbit (NASA)"></td>
    <td width="50%"><img src="docs/images/iss.jpg" alt="International Space Station (NASA)"></td>
  </tr>
  <tr>
    <td align="center"><sub>Thousands of tracked objects crowd Low Earth Orbit. <i>Image: NASA Orbital Debris Program Office</i></sub></td>
    <td align="center"><sub>Even the ISS regularly has to manoeuvre to dodge debris. <i>Image: NASA</i></sub></td>
  </tr>
</table>

**Why it matters, in our own numbers** (one 7-day screening of live data, 10–16 Oct 2026):

| Objects tracked | Close approaches (< 5 km) in 7 days | Critical events |
|:---:|:---:|:---:|
| **18,550** | **118,318** | **1,661** |
| satellites + debris in LEO | far too many to check by hand | need a decision **now** |

---

## Our solution

One automatic pipeline, from **free public orbit data** to a **safe, checked avoidance burn**:

| Step | What it does |
|---|---|
| **1. Track** | Downloads the latest orbits of every LEO satellite and 4 debris clouds from CelesTrak |
| **2. Predict** | Uses orbit physics (SGP4) to find **every close approach in the next 7 days**, with the exact time, miss distance and collision probability |
| **3. Rank** | An **AI model (XGBoost)** trained on real ESA collision warnings ranks events so the most dangerous come first |
| **4. Avoid** | Plans the **smallest burn** (when, which direction, how strong) that makes the event safe, plus a burn to return to the original orbit |
| **5. Verify** | Re-checks the new path against **all 18,550 objects**, so the dodge doesn't create a new near-miss |
| **See it** | Everything is shown live on an **interactive 3D globe** in the browser |

It answers the two questions an operator has:

- **Which close approach matters most?** → an AI-ranked list, most urgent first
- **Which burn makes it safe?** → burn time + direction + size, already safety-checked

---

## Project output

### 1. Live 3D globe, debris alert and AI-ranked list
<img src="docs/images/pic1.png" alt="OrbitGuard AI 3D globe with debris alert and ML ranked list">

- **Center:** the Earth with all **18,550** tracked objects moving in real time. The yellow and red lines are the orbits of the two objects being analysed.
- **Top banner:** a **debris-vs-debris** warning. Two dead objects may collide, and the app names the active satellites that would fly through the fragments.
- **Right:** **events per day** for the next 7 days, plus the conjunction list **ranked by the AI** (`ML YES · 0.208`).
- **Bottom right:** the **red zone** card, listing which satellites pass within 100 km of the predicted crash point, and when.

### 2. Analysing a close approach and planning the dodge
<img src="docs/images/pic2.png" alt="Conjunction analysis with suggested avoidance manoeuvre">

- **KUIPER-00252** is predicted to pass **305 m** from a FENGYUN 1C debris piece. The AI ranks it **6th of 38,548** (YES, high risk).
- **Suggested manoeuvre:** a **0.398 m/s** retrograde burn **2 h 31 min before** the encounter, then a return burn 44 min after.
- **Result:** miss distance **305 m → 4,463 m**, collision probability **1.5e-4 → 9.9e-7**.
- **No new risk:** the whole detour was checked against 1,515 nearby objects.

### 3. The avoidance path in 3D
<img src="docs/images/pic3.png" alt="Avoidance and return burns drawn on the orbit">

- The **green dashed line** is the temporary detour, with markers for **① the avoidance burn** and **② the return burn**.
- The card shows the full plan: burns, Δv, miss distance before/after, and **propellant used (184 g** for a 500 kg satellite).

<details>
<summary><b>More: full app screenshot</b></summary>
<br>
<img src="docs/images/app_overview.jpg" alt="Full app overview">

Left: time controls (pause up to 6 h per second, +1 h / day / week, date picker), colour by constellation, search. Right: 7-day screening summary, AI filter and events-per-day chart.
</details>

---

## How it works – full workflow

```mermaid
flowchart LR
    subgraph OFF["Offline – train the AI"]
        K["ESA Kelvins<br/>real collision warnings"] --> F["117 features"] --> T["Train & compare<br/>5 models"] --> M["XGBoost<br/>risk model"]
    end
    subgraph DAY["Every day – automatic pipeline"]
        C["CelesTrak<br/>live orbits + debris"] --> S["SGP4 physics<br/>7 days, every 20 s"]
        S --> D["Close-approach search<br/>KD-tree + exact closest point"]
        D --> P["Collision probability<br/>red / yellow / green"]
        P --> R["AI risk ranking"]
        R --> B["Burn planner<br/>smallest safe Δv"]
        B --> V["Safety check<br/>vs all objects"]
        P --> Z["Red zones<br/>debris vs debris"]
    end
    M --> R
    V --> UI["3D web app<br/>(Three.js + satellite.js)"]
    Z --> UI
    H["Space-Track history"] --> HR["Replay past<br/>real collisions"] --> UI
```

### Step by step

**1. Track – get the orbits**

`src/fetch_positions.py` downloads orbital elements (OMM/TLE) for all active satellites and 4 debris clouds (COSMOS 2251, Fengyun-1C, Iridium 33, Cosmos 1408) from **CelesTrak**, and keeps only objects in LEO (below 2,000 km).

**2. Predict – find every close approach** (`src/screen_conjunctions.py`)
- **SGP4** computes every object's position every **20 seconds** for **7 days**.
- A **KD-tree** (fast neighbour search) only compares objects that are near each other, so we never test all 170 million pairs.
- Each candidate is refined to the exact **time of closest approach (TCA)** and **miss distance**.
- A **collision probability (Pc)** is computed (Foster/Chan 2D method). Risk colours: **critical** = Pc ≥ 1e-4 or miss < 200 m, **watch** = Pc ≥ 1e-5 or miss < 1 km.

**3. Rank – AI decides what matters first** (`ml/`)
- Trained on **ESA's Kelvins Collision Avoidance Challenge** dataset: real Conjunction Data Messages from ESA operations (9,264 training events).
- We compared **Logistic Regression, Random Forest, HistGradientBoosting, LightGBM and XGBoost**. XGBoost was chosen by grouped cross-validation.
- Every live event gets an **AI risk probability**, a **YES/NO** decision and a **rank**.

**4. Avoid – plan the smallest dodge** (`src/maneuver.py`, `ml/avoidance.py`)
- Uses **Clohessy–Wiltshire (Hill) equations**: how a small push changes a satellite's position relative to its normal path.
- Searches burn times from 0.1 to 3 orbits before TCA and many directions, then picks the **cheapest burn** that gives **miss ≥ 1 km and Pc ≤ 1e-6**.
- Adds one or two **return burns** after the encounter, so the satellite goes back to its original orbit.
- Propellant is estimated with the rocket equation (assumed 500 kg satellite, Isp 220 s).

<p align="center"><img src="docs/images/burn_diagram.png" width="560" alt="Avoidance burn diagram"></p>

**5. Verify – make sure the dodge is safe**

The whole detour is re-flown and checked against **every object** in its altitude band. If it would create a new approach (< 2 km or Pc ≥ 1e-6), it is rejected and the next-best burn is tried. If no safe burn exists, the event is marked **no safe burn – COLLISION WARNING**.

**6. Red zones – when nobody can move**

Two pieces of debris can't manoeuvre. If they might collide, OrbitGuard draws a **100 km "red zone"** around the predicted crash point and lists every active satellite that flies through it during the next orbit.

<p align="center"><img src="docs/images/red_zone_diagram.png" width="560" alt="Red zone diagram"></p>

**7. Always up to date**

`src/server.py` checks every 15 minutes and rebuilds the data each new UTC day (fresh orbits → 7-day screening → AI ranking). You can also pick **any date** and the server screens that window on demand.

**8. Proof on history** (`src/history.py`)

We rebuilt the sky using **only data published 24 h before** real past collisions, to test whether OrbitGuard would have warned in time (see results below).

---

## Satellite communication – satellites that dodge on their own

A second simulation (`autonomy/`): instead of a ground team screening the whole catalogue, **satellites talk to each other by radio** and decide among themselves who moves. Open it with the **"Satellite communication"** button in the 3D app.

```mermaid
flowchart LR
    A["Broadcast every 10 s<br/>position, velocity, orbit,<br/>uncertainty, fuel"] --> B["Received by satellites<br/>within 50 km"]
    B --> C{"Collision risk<br/>in the next 24 h?"}
    C -- no --> D["Discard the data"]
    C -- yes --> E["Keep it as a threat<br/>and monitor"]
    E --> F{"Can they collide?"}
    F -- yes --> G["Satellite whose detour costs the<br/>smallest share of its fuel moves"]
    G --> H["Temporary detour,<br/>back on the original orbit"]
```

| Step | What happens |
|---|---|
| **1. Broadcast** | Every 10 s each satellite sends its position, velocity, orbit elements, position uncertainty and fuel level |
| **2. Range** | Only satellites within **50 km** receive it (links drawn live in 3D) |
| **3. Keep or discard** | The receiver predicts the next **24 h** on board: no risk → data discarded, risk → kept as a threat |
| **4. Who moves?** | Both satellites hold the same data and apply the same rule, so they agree without more messages. Each one's Δv (from its velocity relative to the encounter), mass and thruster give the propellant it needs; the one for which the detour costs the **smallest share of its remaining fuel** manoeuvres (keeping a 5 % reserve) |
| **5. Detour** | A temporary burn before the encounter, safety-checked, then back on the original orbit |
| **6. Debris** | Debris and rocket bodies have no radio, so they stay with the ground screening |

**Results of a 6 h run on the real sky** (10 Oct 2026, 00:00–06:00 UTC):

| Satellites with radio | Messages exchanged | Discarded on board | Threats found | Avoided by the satellites |
|:---:|:---:|:---:|:---:|:---:|
| **15,874** (+ 2,676 silent objects) | **3,581,156** | **99.98 %** | **91** | **46** (33 monitored, 12 warnings) |

**Real example:** STARLINK-5320 and 2026-229A were heading for a **166 m** pass, warned **2.4 h** ahead. The detour would cost Starlink (electric thruster) **0.25 %** of its fuel against **1.79 %** for the other satellite, so Starlink moves: a **1.5 m/s** detour, miss distance **166 m → 1.77 km**, collision chance **1 in 670 → 1 in 1,000,000**.

**Coverage check:** of 241 critical encounters between radio satellites, the network warned 56 at least 10 minutes ahead; 162 pairs first came within 50 km only during the approach itself (at 7–15 km/s). Satellite-to-satellite avoidance works when two satellites have met on an earlier orbit, and complements the ground screening rather than replacing it.

```bash
python autonomy/engine.py      # simulate the network (6 h window, ~1 min)
autonomy/run.sh                # or: simulate if needed and serve it at http://127.0.0.1:8770/autonomy/
```

<sub>Fuel, mass, thruster and position uncertainty are not in public TLE data, so they are simulated per satellite (e.g. Starlink 300–800 kg with electric thrusters, cubesats 5 kg with cold gas). Details: `autonomy/README.md`.</sub>

---

## Results

### Live 7-day screening (10–16 Oct 2026)

| What | Number |
|---|---:|
| Objects screened | **18,550** |
| Close approaches under 5 km | **118,318** |
| Critical / Watch | **1,661** / **24,634** |
| AI-flagged **YES** (high risk) | **5,600** of 38,548 events |
| Ready **burn plans** | **3,326** (median burn **23 cm/s**) |
| Debris-vs-debris **red zones** | **24** (18 with satellites inside, worst: **194** satellites) |

### Would it have predicted real collisions? Yes.

Using only data from **24 hours before** each event:

| Real collision | Predicted time (UTC) | Miss | Flagged as | Rank |
|---|---|---:|:---:|---|
| **Iridium 33 × Cosmos 2251** (10 Feb 2009), the first satellite-satellite crash | 16:55:59.8 (real: 16:55:59.7–.99) | 584 m | critical | 32 of 5,171 |
| **Cerise × Ariane 1 debris** (24 Jul 1996) | 09:48:02 (date matches) | 1,054 m | watch | 192 of 3,120 |
| **Yunhai 1-02 × Zenit-2 debris** (18 Mar 2021) | 07:41:19 (real: 07:41) | 216 m | watch | 746 of 17,488 |

For Iridium–Cosmos, our prediction matches CelesTrak SOCRATES' own prediction from that day (584 m, 16:55:59.806).

### AI model (ESA held-out test set)

| Model | High-risk events caught (recall) | ROC-AUC |
|---|:---:|:---:|
| Logistic Regression | 51 % | 0.93 |
| HistGradientBoosting | 48 % | 0.96 |
| Random Forest | 67 % | 0.96 |
| LightGBM | 77 % | 0.96 |
| **XGBoost (chosen)** | **77 %** | **0.96** |

### Accuracy checks (`tests/`)

| Check | Result |
|---|---|
| Missed events vs a brute-force search every 1 s (3 windows, 3,462 events) | **0 missed, 0 extra** |
| Closest-approach time & distance vs 1 ms sampling | within **0.15 m**, 1 ms |
| Browser (satellite.js) vs Python SGP4, 140,204 states over 7 days | within **0.31 m** |
| Burns re-checked with fuller physics (two-body + J2), 200 burns | **98 %** stay below Pc 1e-6 |

---

## Technologies

| Area | Tools |
|---|---|
| **Language** | Python 3, JavaScript (ES modules) |
| **Orbit physics** | [`sgp4`](https://pypi.org/project/sgp4/) (Python), [`satellite.js`](https://github.com/shashwatak/satellite-js) 6 in a Web Worker (browser) |
| **Number crunching** | NumPy, SciPy (`cKDTree` neighbour search), pandas, multiprocessing |
| **AI / ML** | scikit-learn, **XGBoost**, LightGBM, joblib |
| **Manoeuvre planning** | Clohessy–Wiltshire relative motion, rocket equation, own planner |
| **3D visualisation** | **Three.js** 0.160 (globe, orbits, markers), HTML/CSS |
| **Server** | Python standard-library HTTP server (`src/server.py`): on-demand screening, manoeuvre API, daily auto-update |
| **Monitoring** | Logfire (optional) for live traces of each pipeline run |

---

## Datasets

| Dataset | Used for | Link |
|---|---|---|
| **CelesTrak GP elements** (OMM/TLE): active satellites + COSMOS 2251, Fengyun-1C, Iridium 33, Cosmos 1408 debris | Live orbits of all 18,550 objects | [celestrak.org/NORAD/elements](https://celestrak.org/NORAD/elements/) |
| **ESA Kelvins Collision Avoidance Challenge** (real CDMs, 2015–2019, CC BY 4.0) | Training & testing the AI risk model | [zenodo.org/records/4463683](https://zenodo.org/records/4463683) |
| **Space-Track.org `gp_history`** (free account) | Rebuilding the sky before past collisions | [space-track.org](https://www.space-track.org/) |

---

## Run it yourself

**Requirements:** Linux/macOS (or Git Bash/WSL on Windows), Python 3.11+, a modern browser (Chrome/Edge/Firefox). Node.js only for one optional test.

```bash
git clone https://github.com/shambhuz28/Fusion-hackathon.git
cd Fusion-hackathon

# Full pipeline: download orbits -> positions -> 7-day screening (~4 min) -> start the server
./run.sh

# Or just start the app with the data already in the repo
./run.sh --serve-only
```

Then open **http://127.0.0.1:8765** in your browser.

> **Quickest look (no setup):** `python -m http.server 8765 -d app` and open http://127.0.0.1:8765.
> The globe, conjunction list, AI ranking and precomputed burns all work. Only on-demand screening and live manoeuvre planning need `src/server.py`.

<details>
<summary><b>AI model and tests</b></summary>

```bash
# AI model (own environment)
python -m venv ml/.venv && ml/.venv/Scripts/pip install -r ml/requirements.txt   # Windows path; use ml/.venv/bin on Linux/macOS
ml/.venv/Scripts/python ml/train.py                 # train and compare the models
ml/.venv/Scripts/python ml/predict.py --evaluate    # score on ESA's held-out test events
ml/.venv/Scripts/python ml/assess_conjunctions.py   # rank today's conjunctions + precompute burns

# Accuracy checks
.venv/bin/python tests/verify_conjunctions.py       # screening vs brute force
.venv/bin/python tests/verify_browser_sgp4.py       # browser SGP4 vs Python (needs Node.js)
.venv/bin/python tests/verify_maneuver.py           # burns re-flown with two-body integration

# Daily refresh by hand
python src/daily_update.py --force
```
</details>

---

## Project structure

```
Fusion-hackathon/
├── app/                    3D web app (Three.js + satellite.js)
│   ├── index.html
│   ├── js/                 globe, satellites, conjunction panel, manoeuvre view, SGP4 web worker
│   └── data/               leo_objects.json, conjunctions.json, risk_assessment.json
├── src/                    Python pipeline
│   ├── fetch_positions.py      download CelesTrak orbits, compute positions
│   ├── export_sim_data.py      prepare data for the app
│   ├── screen_conjunctions.py  7-day close-approach search + Pc + red zones
│   ├── maneuver.py             avoidance + return burn planner
│   ├── server.py               web server, on-demand screening, daily update
│   ├── daily_update.py         rebuild everything for today
│   └── history.py              replay past real collisions
├── ml/                     AI risk model (train, features, predict, avoidance)
├── tests/                  accuracy checks
├── docs/                   images + full technical notes (docs/TECHNICAL.md)
└── run.sh                  one command to run everything
```

---

## Honest limitations

- **Public data is approximate.** TLE/OMM positions are good to about 1 km and drift by 1–3 km per day, so we refresh every day.
- **No real uncertainty in public data.** The collision probability uses an **assumed** uncertainty model, so the AI score is best read as a **ranking**.
- **Burns are larger than real ones** (typically 1–10 cm/s in practice) because of that assumed, km-sized uncertainty.
- **Satellite mass and fuel are unknown**, so we assume 500 kg and Isp 220 s, and cap burns at 1 m/s.
- A **red zone is a simple warning area**, not a full fragment-cloud model.

## Future scope

- Use real uncertainty data (CDMs) for exact collision probability
- Model the full debris cloud after a collision
- Low-thrust (electric) manoeuvres and real mission rules
- Automatic alerts to operators (email / API)

---

## Team

Built for the **Fusion 2026 Hackathon** by:

| Member | GitHub |
|---|---|
| Shambhu Gaikwad | [@shambhuz28](https://github.com/shambhuz28) |
| Amey Mohite | [@Amey2006](https://github.com/Amey2006) |
| Suyash Patil | [@Suyashh19](https://github.com/Suyashh19) |
| Atharv Taware | [@ATHARV-TAWARE](https://github.com/ATHARV-TAWARE) |

## References & credits

- CelesTrak, *GP data formats*: https://celestrak.org/NORAD/documentation/gp-data-formats.php
- Vallado, Crawford, Hujsak & Kelso, *Revisiting Spacetrack Report #3* (AIAA 2006-6753): SGP4
- Uriot et al., *Spacecraft collision avoidance challenge* (2020): https://arxiv.org/abs/2008.03069
- ESA Kelvins CA dataset: https://doi.org/10.5281/zenodo.4463683
- Foster & Estes (1992), NASA JSC-25898: probability of collision
- Clohessy & Wiltshire (1960), *Terminal guidance system for satellite rendezvous*
- Space images: NASA (public domain), via Wikimedia Commons: *Debris-LEO1280*, *International Space Station after undocking of STS-132*, *The Earth seen from Apollo 17* (banner)

**Full technical details** (every option, algorithm and verification): [docs/TECHNICAL.md](docs/TECHNICAL.md)
