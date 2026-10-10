"""Turn the app's screened conjunctions (src/screen_conjunctions.py) into Kelvins-style CDM rows.

The ESA model was trained on Conjunction Data Messages. Our conjunctions come from TLE/OMM
screening, so only the fields that can be derived from the orbits and the screener's assumed
covariance are filled; the rest (tracking statistics, space weather, ...) are left empty (NaN),
which the gradient-boosted model handles natively.

Units and sign conventions follow the Kelvins dataset (checked against train_data.csv):
distances in m, speeds in m/s, relative state = secondary minus primary in the primary's RTN
frame, orbit sizes in km, angles in degrees.

The "primary" (Kelvins target, t_) is the object that can manoeuvre: the satellite when the other
object is debris or a rocket body, otherwise the first object of the pair.
"""

import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "src"))   # after ml/, so ml's own modules are never shadowed
import screen_conjunctions as sc  # noqa: E402  (Pc, covariance model and object loading)
from sgp4.api import jday  # noqa: E402

CONJUNCTIONS_PATH = ROOT / "app" / "data" / "conjunctions.json"


class Catalogue:
    """The simulation's objects with their SGP4 records, looked up by NORAD ID."""

    def __init__(self, objects_path=None):
        self.objs, self.sats = sc.load_objects(objects_path or sc.OBJECTS_PATH)
        self.index = {o["norad_id"]: k for k, o in enumerate(self.objs)}

    def sat(self, norad):
        return self.sats[self.index[norad]]

    def obj(self, norad):
        return self.objs[self.index[norad]]

    def is_debris(self, norad):
        """Debris as the screener's covariance model counts it (group "Debris")."""
        return self.obj(norad)["group"] == "Debris"

    def can_manoeuvre(self, norad):
        """Same rule as can_manoeuvre in src/maneuver.py: debris and rocket bodies cannot."""
        o = self.obj(norad)
        name = o["name"].upper()
        return o["group"] not in ("Debris", "Rocket body") and " DEB" not in name and "R/B" not in name

    def object_type(self, norad):
        """Kelvins c_object_type."""
        o = self.obj(norad)
        if o["group"] == "Rocket body" or "R/B" in o["name"].upper():
            return "ROCKET BODY"
        return "PAYLOAD" if self.can_manoeuvre(norad) else "DEBRIS"


def load_screening(path=CONJUNCTIONS_PATH):
    """The screener's app file as (metadata, list of event dicts), plus the window start (t0, jd0, fr0)."""
    data = json.loads(Path(path).read_text())
    events = [dict(zip(data["fields"], row)) for row in data["rows"]]
    t0 = datetime.fromisoformat(data["window_start_utc"].replace("Z", "+00:00"))
    jd0, fr0 = jday(t0.year, t0.month, t0.day, t0.hour, t0.minute, t0.second)
    return data, events, t0, jd0, fr0


def element_age_days(sat, jd0, fr0, t_sec):
    return (t_sec - sc.epoch_seconds(sat, jd0 + fr0)) / 86400.0


def sigma_rtn_km(age_days, debris):
    """1-sigma RTN position error of the screener's assumed covariance model."""
    s = sc.SIGMA_BASE_KM + sc.SIGMA_GROWTH_KM_PER_DAY * max(age_days, 0.0)
    return s * (sc.DEBRIS_SIGMA_FACTOR if debris else 1.0)


def encounter_plane(r1, v1, r2, v2, cov):
    """Encounter-plane basis (3x2), 2D miss vector and 2D combined covariance."""
    vrel = v2 - v1
    vhat = vrel / np.linalg.norm(vrel)
    miss = r2 - r1
    miss_plane = miss - (miss @ vhat) * vhat
    d = np.linalg.norm(miss_plane)
    x = miss_plane / d if d > 1e-9 else np.cross(vhat, [0.0, 0.0, 1.0])
    x /= np.linalg.norm(x)
    basis = np.column_stack([x, np.cross(vhat, x)])
    return basis, basis.T @ miss, basis.T @ cov @ basis


def primary_secondary(cat, e):
    """(primary, secondary) NORAD IDs: the primary is the satellite that can manoeuvre."""
    a, b = e["norad_1"], e["norad_2"]
    if not cat.can_manoeuvre(a) and cat.can_manoeuvre(b):
        return b, a
    return a, b


def state_at(sat, jd0, fr0, t_sec):
    err, r, v = sc.sgp4_at(sat, jd0, fr0, t_sec)
    return (None, None) if err else (r, v)


def cdm_row(cat, e, jd0, fr0, t0_unix, hbr_km, event_id):
    """One Kelvins-format CDM row for a screened event (None if SGP4 fails)."""
    p, s = primary_secondary(cat, e)
    t_sec = e["tca_ms"] / 1000.0 - t0_unix
    sat_p, sat_s = cat.sat(p), cat.sat(s)
    rp, vp = state_at(sat_p, jd0, fr0, t_sec)
    rs, vs = state_at(sat_s, jd0, fr0, t_sec)
    if rp is None or rs is None:
        return None
    deb_p, deb_s = cat.is_debris(p), cat.is_debris(s)
    age_p = element_age_days(sat_p, jd0, fr0, t_sec)
    age_s = element_age_days(sat_s, jd0, fr0, t_sec)
    cov_p, cov_s = sc.covariance(rp, vp, age_p, deb_p), sc.covariance(rs, vs, age_s, deb_s)
    pc = sc.collision_probability(rp, vp, rs, vs, cov_p, cov_s, hbr_km)

    basis, miss2, c2 = encounter_plane(rp, vp, rs, vs, cov_p + cov_s)
    maha = float(math.sqrt(miss2 @ np.linalg.solve(c2, miss2)))
    # Covariance scaling that maximises Pc (small hard-body approximation): k* = d^2 / 2.
    det = math.sqrt(np.linalg.det(c2))
    k_star = max(maha ** 2 / 2, 1e-12)
    pc_max = min(1.0, hbr_km ** 2 / (2 * k_star * det) * math.exp(-maha ** 2 / (2 * k_star)))

    m = sc.rtn_basis(rp, vp)
    rel_r = m.T @ (rs - rp) * 1000.0
    rel_v = m.T @ (vs - vp) * 1000.0
    speed = float(np.linalg.norm(rel_v))
    sig_p = sigma_rtn_km(age_p, deb_p) * 1000.0
    sig_s = sigma_rtn_km(age_s, deb_s) * 1000.0

    def orbit(prefix, sat):
        return {f"{prefix}_j2k_sma": sat.a * sat.radiusearthkm, f"{prefix}_j2k_ecc": sat.ecco,
                f"{prefix}_j2k_inc": math.degrees(sat.inclo),
                f"{prefix}_h_apo": sat.alta * sat.radiusearthkm, f"{prefix}_h_per": sat.altp * sat.radiusearthkm}

    row = {
        "event_id": event_id,
        "time_to_tca": t_sec / 86400.0,
        "risk": math.log10(max(pc, 1e-30)),
        "max_risk_estimate": math.log10(max(pc_max, 1e-30)),
        "max_risk_scaling": k_star,
        "miss_distance": float(np.linalg.norm(rs - rp)) * 1000.0,
        "relative_speed": speed,
        "relative_position_r": rel_r[0], "relative_position_t": rel_r[1], "relative_position_n": rel_r[2],
        "relative_velocity_r": rel_v[0], "relative_velocity_t": rel_v[1], "relative_velocity_n": rel_v[2],
        "t_sigma_r": sig_p[0], "t_sigma_t": sig_p[1], "t_sigma_n": sig_p[2],
        "c_sigma_r": sig_s[0], "c_sigma_t": sig_s[1], "c_sigma_n": sig_s[2],
        "t_position_covariance_det": float(np.prod(sig_p ** 2)),
        "c_position_covariance_det": float(np.prod(sig_s ** 2)),
        "c_object_type": cat.object_type(s),
        "mahalanobis_distance": maha,
        "geocentric_latitude": math.degrees(math.asin(rp[2] / np.linalg.norm(rp))),
        "azimuth": math.degrees(math.atan2(rel_v[2], -rel_v[1])),
        "elevation": math.degrees(math.asin(np.clip(-rel_v[0] / speed, -1, 1))),
        **orbit("t", sat_p), **orbit("c", sat_s),
    }
    return row


def build_cdms(cat, events, data, jd0, fr0, t0):
    """CDM rows for all events; returns (DataFrame, list of event indices that produced a row)."""
    rows, kept = [], []
    hbr_km = data["hbr_m"] / 1000.0
    t0_unix = t0.timestamp()
    for k, e in enumerate(events):
        row = cdm_row(cat, e, jd0, fr0, t0_unix, hbr_km, event_id=k)
        if row is not None:
            rows.append(row)
            kept.append(k)
    return pd.DataFrame(rows), kept
