"""Score conjunction situations: ML collision-risk probability and a YES/NO decision per event.

Input is a CDM table in the Kelvins format (one row per Conjunction Data Message, several per
event). For every event the most recent CDM issued at least `--as-of-days` before TCA is the
"current situation"; later CDMs are ignored, as if it were that moment now.

Printed per event:
    Pc now        collision probability in the latest CDM
    Risk prob     ML probability that the event ends high risk (final Pc >= 1e-6)
    Pred. Pc      predicted final collision probability
    Decision      YES = high-risk conjunction, plan a collision avoidance maneuver

The default input is the official ESA test set (CDMs >= 2 days before TCA), whose events were
never used in training; with --evaluate the true final Pc from test_data_private.csv is shown.

Usage:
    python ml/predict.py                          # every ESA test event
    python ml/predict.py --evaluate --only-yes
    python ml/predict.py --top 25
    python ml/predict.py --input my_cdms.csv --output predictions.csv

From other code (run with ml/ on sys.path):
    from predict import predict_conjunctions
    table = predict_conjunctions(cdm_dataframe)
"""

import argparse
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

import features as F
from risk_model import predicted_final_log_pc, raw_score

ML_DIR = Path(__file__).resolve().parent
MODEL_PATH = ML_DIR / "models" / "collision_risk_model.joblib"
DEFAULT_INPUT = ML_DIR / "data" / "raw" / "test_data.csv"
DEFAULT_LABELS = ML_DIR / "data" / "raw" / "test_data_private.csv"


def load_model(path=MODEL_PATH):
    if not Path(path).exists():
        raise FileNotFoundError(f"{path} not found: run  python ml/train.py  first")
    return joblib.load(path)


def predict_conjunctions(cdms, bundle=None, as_of_days=F.DECISION_HORIZON_DAYS):
    """Return one row per event, riskiest first: current state, ML risk probability,
    predicted final Pc and the YES/NO decision."""
    bundle = bundle or load_model()
    cdms = cdms.sort_values([F.EVENT, F.TTCA], ascending=[True, False], kind="mergesort")
    snaps = F.build_snapshots(cdms, min_ttca=as_of_days, labelled=False, latest_only=True)
    X = snaps.reindex(columns=bundle["features"]).to_numpy(float)

    score = raw_score(bundle["model"], X)
    is_yes = score >= bundle["threshold"]
    out = pd.DataFrame({
        F.EVENT: snaps[F.EVENT].to_numpy(),
        "n_cdms": snaps["n_cdms"].to_numpy(),
        "time_to_tca_days": snaps[F.TTCA].to_numpy(),
        "miss_distance_m": snaps["miss_distance"].to_numpy(),
        "relative_speed_m_s": snaps["relative_speed"].to_numpy(),
        "current_pc": 10.0 ** snaps[F.RISK].to_numpy(),
        "risk_probability": bundle["calibrator"](score),
        "predicted_final_pc": 10.0 ** predicted_final_log_pc(snaps[F.RISK].to_numpy(), is_yes),
        "decision": np.where(is_yes, "YES", "NO"),
    })
    order = np.lexsort((-out["current_pc"].to_numpy(), -score))
    return out.iloc[order].reset_index(drop=True)


def fmt_pc(pc):
    return "<1e-10" if pc < 1e-10 else f"{pc:.2e}"


def print_table(table, evaluate):
    header = (f"{'Event':>6} {'CDMs':>4} {'T-TCA[d]':>8} {'Miss[m]':>8} {'Speed[m/s]':>10} "
              f"{'Pc now':>9} {'Risk prob':>9} {'Pred. Pc':>9} {'Decision':>8}")
    if evaluate:
        header += f" {'True Pc':>9} {'Result':>6}"
    print(header)
    print("-" * len(header))
    high = 10 ** F.HIGH_RISK_LOG10
    for r in table.itertuples(index=False):
        line = (f"{r.event_id:>6} {r.n_cdms:>4} {r.time_to_tca_days:>8.2f} {r.miss_distance_m:>8.0f} "
                f"{r.relative_speed_m_s:>10.0f} {fmt_pc(r.current_pc):>9} {r.risk_probability:>9.3f} "
                f"{fmt_pc(r.predicted_final_pc):>9} {r.decision:>8}")
        if evaluate:
            actual, said = r.true_final_pc >= high, r.decision == "YES"
            result = {(True, True): "TP", (False, False): "TN", (False, True): "FP", (True, False): "MISS"}
            line += f" {fmt_pc(r.true_final_pc):>9} {result[(actual, said)]:>6}"
        print(line)


def print_summary(table, bundle, evaluate):
    n_yes = int((table["decision"] == "YES").sum())
    print(f"\n{len(table)} conjunction events scored: {n_yes} YES (high risk), {len(table) - n_yes} NO")
    print(f"Model: {bundle['model_name']}; YES when the event is predicted to end with Pc >= "
          f"{10 ** bundle['high_risk_log10']:.0e}")
    if evaluate:
        actual = table["true_final_pc"] >= 10 ** F.HIGH_RISK_LOG10
        said = table["decision"] == "YES"
        tp, fp, fn = int((actual & said).sum()), int((~actual & said).sum()), int((actual & ~said).sum())
        tn = len(table) - tp - fp - fn
        prec, rec = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
        f2 = 5 * prec * rec / max(4 * prec + rec, 1e-12)
        print(f"Against the true final Pc: {tp} TP, {fp} FP, {fn} missed, {tn} TN  |  precision {prec:.3f}, "
              f"recall {rec:.3f}, F2 {f2:.3f}, accuracy {(tp + tn) / len(table):.3f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="CDM CSV (Kelvins format)")
    parser.add_argument("--model", type=Path, default=MODEL_PATH)
    parser.add_argument("--as-of-days", type=float, default=F.DECISION_HORIZON_DAYS,
                        help="ignore CDMs issued closer to TCA than this (default 2)")
    parser.add_argument("--evaluate", action="store_true",
                        help="also print the true final Pc (from --labels, or the input's own last CDM)")
    parser.add_argument("--labels", type=Path,
                        help="CSV of final CDMs (default for the ESA test set: test_data_private.csv)")
    parser.add_argument("--only-yes", action="store_true", help="print only YES events")
    parser.add_argument("--top", type=int, help="print only the N riskiest events")
    parser.add_argument("--output", type=Path, help="also write all predictions to this CSV")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"{args.input} not found (run  python ml/download_data.py  for the ESA data)", file=sys.stderr)
        return 1
    bundle = load_model(args.model)
    cdms = F.load_cdms(args.input)
    table = predict_conjunctions(cdms, bundle, args.as_of_days)
    if table.empty:
        print(f"No event has a CDM issued >= {args.as_of_days:g} days before TCA", file=sys.stderr)
        return 1
    if args.evaluate:
        labels_path = args.labels or (DEFAULT_LABELS if args.input.resolve() == DEFAULT_INPUT else None)
        final = F.load_labels(labels_path) if labels_path else F.final_risk(cdms)[0]
        table["true_final_pc"] = 10.0 ** table[F.EVENT].map(final).to_numpy()

    shown = table[table["decision"] == "YES"] if args.only_yes else table
    if args.top:
        shown = shown.head(args.top)
    print_table(shown, args.evaluate)
    print_summary(table, bundle, args.evaluate)
    if args.output:
        table.to_csv(args.output, index=False)
        print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
