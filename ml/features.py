"""Feature engineering for conjunction events made of CDM (Conjunction Data Message) time series.

A conjunction event is a sequence of CDMs for one pair of objects, issued as the time of
closest approach (TCA) nears. Each CDM carries the current estimate of the collision
probability as `risk` = log10(Pc). The quantity we want is the *final* risk, i.e. the risk
in the last CDM before TCA, predicted from the CDMs available now.

A "snapshot" is the state of an event as seen at one CDM: that CDM's fields plus summary
features of the CDMs received before it (no look-ahead).
"""

import numpy as np
import pandas as pd

EVENT = "event_id"
TTCA = "time_to_tca"            # days until time of closest approach
RISK = "risk"                   # log10 of the collision probability in that CDM

HIGH_RISK_LOG10 = -6.0          # Kelvins definition of a high-risk event: final Pc >= 1e-6
RISK_FLOOR = -10.0              # targets below this (down to the -30 "no risk" value) are clipped
DECISION_HORIZON_DAYS = 2.0     # snapshots must be at least this far before TCA (time to plan a maneuver)
LABEL_MAX_TTCA_DAYS = 1.0       # an event's last CDM must be this close to TCA to count as its final risk

OBJECT_TYPES = ["PAYLOAD", "ROCKET BODY", "DEBRIS", "UNKNOWN", "TBA"]

TARGETS = ["final_risk", "high_risk"]
# Identifiers, not physics: kept out of the model so it generalises to other satellites.
EXCLUDED = {EVENT, "mission_id", *TARGETS}


def load_cdms(path):
    """Read a CDM table and sort each event's CDMs in time order (time_to_tca decreasing)."""
    df = pd.read_csv(path)
    return df.sort_values([EVENT, TTCA], ascending=[True, False], kind="mergesort").reset_index(drop=True)


def final_risk(cdms):
    """log10 Pc of the last CDM of every event, plus how close to TCA that CDM was."""
    last = cdms.groupby(EVENT, sort=False).tail(1).set_index(EVENT)
    return last[RISK].rename("final_risk"), last[TTCA].rename("final_ttca")


def add_history_features(cdms):
    """Append features summarising each event's CDMs up to and including the current one."""
    df = cdms.copy()
    g = df.groupby(EVENT, sort=False)

    df["c_object_type"] = pd.Categorical(df["c_object_type"], categories=OBJECT_TYPES).codes.astype(float)
    df.loc[df["c_object_type"] < 0, "c_object_type"] = np.nan

    df["n_cdms"] = g.cumcount() + 1
    df["risk_first"] = g[RISK].transform("first")
    df["risk_max_so_far"] = g[RISK].cummax()
    df["risk_min_so_far"] = g[RISK].cummin()
    df["risk_mean_so_far"] = g[RISK].cumsum() / df["n_cdms"]

    prev = g[[RISK, TTCA, "miss_distance", "mahalanobis_distance"]].shift(1)
    df["risk_prev"] = prev[RISK]
    df["risk_delta"] = df[RISK] - prev[RISK]
    df["dt_since_prev"] = prev[TTCA] - df[TTCA]
    df["risk_slope"] = df["risk_delta"] / df["dt_since_prev"].where(df["dt_since_prev"] > 1e-6)
    df["miss_distance_delta"] = df["miss_distance"] - prev["miss_distance"]
    df["mahalanobis_delta"] = df["mahalanobis_distance"] - prev["mahalanobis_distance"]
    df["risk_change_total"] = df[RISK] - df["risk_first"]

    df["log_miss_distance"] = np.log10(df["miss_distance"].clip(lower=1.0))
    for side in ("t", "c"):
        sig = [f"{side}_sigma_{axis}" for axis in ("r", "t", "n")]
        if all(col in df for col in sig):
            df[f"{side}_log_sigma_pos"] = np.log10(np.sqrt((df[sig] ** 2).sum(axis=1, min_count=3)).clip(lower=1e-3))
    if {"t_log_sigma_pos", "c_log_sigma_pos"} <= set(df):
        sigma_comb = np.sqrt(10 ** (2 * df["t_log_sigma_pos"]) + 10 ** (2 * df["c_log_sigma_pos"]))
        df["miss_over_sigma"] = df["miss_distance"] / sigma_comb
    # Covariance determinants reach 1e46, beyond float32 range; their log is what matters.
    for col in ("t_position_covariance_det", "c_position_covariance_det"):
        if col in df:
            df[col] = np.log10(df[col].where(df[col] > 0))
    num = df.select_dtypes("number").columns
    df[num] = df[num].replace([np.inf, -np.inf], np.nan)
    return df


def feature_columns(df):
    """Numeric model inputs: everything except identifiers."""
    return [c for c in df.columns
            if c not in EXCLUDED and pd.api.types.is_numeric_dtype(df[c])]


def build_snapshots(cdms, min_ttca=DECISION_HORIZON_DAYS, labelled=True, latest_only=False, labels=None):
    """Turn a CDM table into model rows.

    Each row is one event as seen at one CDM issued at least `min_ttca` days before TCA.
    With `latest_only`, each event keeps only its most recent such snapshot (the "current
    situation").

    With `labelled`, the targets `final_risk` (log10 Pc, clipped at RISK_FLOOR) and
    `high_risk` are attached. The final risk comes from `labels` (event_id -> log10 Pc of
    the last CDM) when the CDMs near TCA are held separately, as in the Kelvins test set;
    otherwise it is the event's own last CDM, which is then dropped from the inputs, and
    events whose last CDM is more than LABEL_MAX_TTCA_DAYS before TCA are skipped.
    """
    df = add_history_features(cdms)
    if labelled:
        if labels is None:
            labels, last_ttca = final_risk(cdms)
            df = df[df[EVENT].isin(last_ttca.index[last_ttca <= LABEL_MAX_TTCA_DAYS])]
            df = df[df.groupby(EVENT, sort=False).cumcount(ascending=False) > 0]
        df = df[df[EVENT].isin(labels.index)]
    df = df[df[TTCA] >= min_ttca]
    if latest_only:
        df = df.groupby(EVENT, sort=False).tail(1)
    df = df.reset_index(drop=True).copy()
    if labelled:
        df["final_risk"] = df[EVENT].map(labels).clip(lower=RISK_FLOOR)
        df["high_risk"] = (df["final_risk"] >= HIGH_RISK_LOG10).astype(int)
    return df


def load_labels(path):
    """event_id -> final log10 Pc from a file of final CDMs (Kelvins `test_data_private.csv`)."""
    df = load_cdms(path)
    col = "true_risk" if "true_risk" in df else RISK
    return df.groupby(EVENT, sort=False)[col].last()
