"""Train and select the collision-risk model on the ESA Kelvins CDM dataset.

Question answered for every conjunction event, from the CDMs available at least 2 days
before TCA: will the event end as high risk, i.e. final collision probability Pc >= 1e-6?

Candidates (the "latest Pc" persistence baseline, logistic regression, random forest,
HistGradientBoosting, LightGBM, XGBoost) are compared with 5-fold cross-validation grouped
by event on the training set. The decision threshold of each is tuned for F2 (recall counts
4x precision, as missing a real high-risk event is the costly error). The winner is refitted
on all training events, its scores are calibrated into probabilities (Platt scaling on the
out-of-fold scores), it is scored once on the official held-out test set, and saved to
ml/models/collision_risk_model.joblib.

Usage:
    python ml/train.py
    python ml/train.py --folds 3 --quick      # faster comparison (fewer trees)
"""

import argparse
import json
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from lightgbm import LGBMClassifier
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, brier_score_loss, fbeta_score,
                             precision_score, recall_score, roc_auc_score)
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

import features as F
from risk_model import Persistence, PlattCalibrator, predicted_final_log_pc, raw_score

ML_DIR = Path(__file__).resolve().parent
RAW_DIR = ML_DIR / "data" / "raw"
MODEL_DIR = ML_DIR / "models"
MODEL_PATH = MODEL_DIR / "collision_risk_model.joblib"
REPORT_PATH = MODEL_DIR / "model_report.json"
SEED = 42
BASELINE = "Persistence (latest Pc)"

warnings.filterwarnings("ignore", message="X does not have valid feature names")


def candidates(n_trees, pos_weight):
    """Model factories. Trees are kept shallow: there are only ~100 high-risk training events."""
    return {
        BASELINE: None,
        "Logistic Regression": lambda: make_pipeline(
            SimpleImputer(strategy="median"), StandardScaler(),
            LogisticRegression(C=0.1, class_weight="balanced", max_iter=3000)),
        "Random Forest": lambda: make_pipeline(
            SimpleImputer(strategy="median"),
            RandomForestClassifier(n_estimators=n_trees, min_samples_leaf=10, max_features="sqrt",
                                   class_weight="balanced_subsample", n_jobs=-1, random_state=SEED)),
        "HistGradientBoosting": lambda: HistGradientBoostingClassifier(
            max_iter=n_trees, learning_rate=0.03, max_leaf_nodes=7, min_samples_leaf=50,
            l2_regularization=5.0, class_weight={0: 1.0, 1: pos_weight}, random_state=SEED),
        "LightGBM": lambda: LGBMClassifier(
            n_estimators=n_trees, learning_rate=0.02, num_leaves=7, min_child_samples=50,
            subsample=0.8, subsample_freq=1, colsample_bytree=0.7, reg_lambda=5.0,
            scale_pos_weight=pos_weight, random_state=SEED, verbose=-1),
        "XGBoost": lambda: XGBClassifier(
            n_estimators=n_trees, learning_rate=0.02, max_depth=3, min_child_weight=5,
            subsample=0.8, colsample_bytree=0.7, reg_lambda=5.0, scale_pos_weight=pos_weight,
            tree_method="hist", random_state=SEED, n_jobs=-1),
    }


def fit(factory, feats, train):
    """Fit a candidate on training snapshots, weighting so every event counts once."""
    if factory is None:
        return Persistence(feats)
    X = train[feats].to_numpy(float)
    y = train["high_risk"].to_numpy()
    w = 1.0 / train.groupby(F.EVENT)[F.EVENT].transform("size").to_numpy(float)
    model = factory()
    step = model.steps[-1][0] if hasattr(model, "steps") else None
    model.fit(X, y, **({f"{step}__sample_weight": w} if step else {"sample_weight": w}))
    return model


def best_threshold(y, score, beta=2.0):
    """Threshold on `score` that maximises F-beta."""
    grid = np.unique(np.quantile(score, np.linspace(0.80, 0.999, 400)))
    f = [fbeta_score(y, score >= t, beta=beta, zero_division=0) for t in grid]
    return float(grid[int(np.argmax(f))])


def metrics(y, score, threshold):
    decision = score >= threshold
    return {
        "F2": fbeta_score(y, decision, beta=2, zero_division=0),
        "precision": precision_score(y, decision, zero_division=0),
        "recall": recall_score(y, decision, zero_division=0),
        "PR_AUC": average_precision_score(y, score),
        "ROC_AUC": roc_auc_score(y, score),
        "flagged": int(decision.sum()),
    }


def mse_high_risk(final_log_pc, pred_log_pc):
    """Kelvins regression metric: MSE over the truly high-risk events."""
    hr = final_log_pc >= F.HIGH_RISK_LOG10
    return float(np.mean((pred_log_pc[hr] - final_log_pc[hr]) ** 2))


def print_table(title, rows, cols):
    print(f"\n{title}")
    width = max(len(name) for name in rows) + 2
    print(" " * width + "".join(f"{c:>11}" for c in cols))
    for name, m in rows.items():
        print(f"{name:<{width}}" + "".join(f"{m[c]:>11.4f}" if isinstance(m[c], float) else f"{m[c]:>11}"
                                           for c in cols))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--quick", action="store_true", help="fewer trees, for a fast check")
    args = parser.parse_args()
    n_trees = 150 if args.quick else 400

    paths = {name: RAW_DIR / f"{name}.csv" for name in ("train_data", "test_data", "test_data_private")}
    if not all(p.exists() for p in paths.values()):
        print("Dataset missing: run  python ml/download_data.py  first", file=sys.stderr)
        return 1

    print("Loading CDMs ...")
    train_cdms = F.load_cdms(paths["train_data"])
    # Train on the event as seen at every CDM before its last one (time_to_tca is a feature);
    # evaluate on the latest CDM issued >= 2 days before TCA, the Kelvins decision setting.
    snaps = F.build_snapshots(train_cdms, min_ttca=0.0)
    evalrows = snaps[snaps[F.TTCA] >= F.DECISION_HORIZON_DAYS].groupby(F.EVENT, sort=False).tail(1)
    test = F.build_snapshots(F.load_cdms(paths["test_data"]), latest_only=True,
                             labels=F.load_labels(paths["test_data_private"]))
    feats = F.feature_columns(snaps)

    n_events = snaps[F.EVENT].nunique()
    n_pos = int(snaps.groupby(F.EVENT)["high_risk"].first().sum())
    print(f"Train: {len(train_cdms)} CDMs, {n_events} labelled events ({n_pos} high risk), "
          f"{len(snaps)} snapshots; {len(evalrows)} events ({int(evalrows['high_risk'].sum())} high risk) "
          f"have a CDM >= {F.DECISION_HORIZON_DAYS:g} days before TCA for validation")
    print(f"Test:  {len(test)} events ({int(test['high_risk'].sum())} high risk)")
    print(f"Features: {len(feats)}")

    models = candidates(n_trees, pos_weight=(n_events - n_pos) / max(n_pos, 1))

    print(f"\n== {args.folds}-fold cross-validation, grouped by event ==")
    events = evalrows[F.EVENT].to_numpy()
    y_cv = evalrows["high_risk"].to_numpy()
    oof = {name: np.zeros(len(evalrows)) for name in models}
    for k, (_, va_idx) in enumerate(GroupKFold(n_splits=args.folds).split(evalrows, groups=events)):
        train = snaps[~snaps[F.EVENT].isin(set(events[va_idx]))]
        X_val = evalrows.iloc[va_idx][feats].to_numpy(float)
        for name, factory in models.items():
            t0 = time.time()
            oof[name][va_idx] = raw_score(fit(factory, feats, train), X_val)
            print(f"  fold {k + 1}/{args.folds}  {name:<24} {time.time() - t0:5.1f}s", flush=True)

    thresholds = {name: best_threshold(y_cv, s) for name, s in oof.items()}
    cv = {name: metrics(y_cv, s, thresholds[name]) for name, s in oof.items()}
    cols = ["F2", "precision", "recall", "PR_AUC", "ROC_AUC", "flagged"]
    print_table("Cross-validation (threshold tuned for F2 on out-of-fold scores)", cv, cols)

    best = max(cv, key=lambda n: (round(cv[n]["F2"], 3), cv[n]["PR_AUC"]))
    print(f"\nSelected: {best}  (CV F2 {cv[best]['F2']:.4f}, PR-AUC {cv[best]['PR_AUC']:.4f})")

    print("\n== Held-out Kelvins test set (never used for selection) ==")
    X_test, y_test = test[feats].to_numpy(float), test["high_risk"].to_numpy()
    fitted, test_scores = {}, {}
    for name, factory in models.items():
        fitted[name] = fit(factory, feats, snaps)
        test_scores[name] = raw_score(fitted[name], X_test)
    test_metrics = {name: metrics(y_test, s, thresholds[name]) for name, s in test_scores.items()}
    print_table("Test set (CV-tuned thresholds)", test_metrics, cols)

    calibrator = PlattCalibrator(logit_input=best != BASELINE).fit(oof[best], y_cv)
    p_test = calibrator(test_scores[best])
    final_test = test["final_risk"].to_numpy()
    pc_rows = {
        "Latest Pc (persistence)": np.clip(test[F.RISK].to_numpy(), F.RISK_FLOOR, None),
        f"Gated by {best}": predicted_final_log_pc(test[F.RISK].to_numpy(),
                                                   test_scores[best] >= thresholds[best]),
    }
    print("\nFinal-Pc estimate on test, Kelvins metrics (MSE on high-risk events, lower is better):")
    for name, pred in pc_rows.items():
        f2 = fbeta_score(y_test, pred >= F.HIGH_RISK_LOG10, beta=2)
        mse = mse_high_risk(final_test, np.clip(pred, F.HIGH_RISK_LOG10 - 0.001, None))
        print(f"  {name:<34} MSE_HR {mse:.4f}   F2 {f2:.4f}   score MSE_HR/F2 {mse / max(f2, 1e-9):.4f}")
    print(f"Calibrated P(high risk) on test: Brier {brier_score_loss(y_test, p_test):.4f}, "
          f"mean {p_test.mean():.4f} vs observed rate {y_test.mean():.4f}")

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    bundle = {
        "model": fitted[best],
        "model_name": best,
        "threshold": thresholds[best],
        "calibrator": calibrator,
        "features": feats,
        "high_risk_log10": F.HIGH_RISK_LOG10,
        "decision_horizon_days": F.DECISION_HORIZON_DAYS,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    joblib.dump(bundle, MODEL_PATH, compress=3)
    report = {k: v for k, v in bundle.items() if k not in ("model", "calibrator", "features")}
    report.update(n_features=len(feats), cv=cv, test=test_metrics, cv_thresholds=thresholds,
                  n_train_events=n_events, n_train_high_risk=n_pos, n_test_events=len(test),
                  n_test_high_risk=int(y_test.sum()))
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=float))
    print(f"\nSaved {MODEL_PATH} ({MODEL_PATH.stat().st_size / 1e6:.1f} MB) and {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
