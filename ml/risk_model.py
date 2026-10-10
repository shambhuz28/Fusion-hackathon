"""Model pieces stored in the saved bundle (kept in a module so joblib can load them anywhere)."""

import numpy as np
from sklearn.linear_model import LogisticRegression

import features as F


class Persistence:
    """Baseline: the event ends high risk if the latest CDM already says so. Score = latest log10 Pc."""

    def __init__(self, features):
        self.col = features.index(F.RISK)

    def fit(self, X, y, sample_weight=None):
        return self

    def decision_score(self, X):
        return np.asarray(X)[:, self.col]


class PlattCalibrator:
    """Maps a model's raw score to a probability with a 1-D logistic fit on out-of-fold scores."""

    def __init__(self, logit_input):
        self.logit_input = logit_input

    def _x(self, score):
        s = np.asarray(score, float)
        if self.logit_input:
            s = np.clip(s, 1e-6, 1 - 1e-6)
            s = np.log(s / (1 - s))
        return s.reshape(-1, 1)

    def fit(self, score, y):
        self.lr = LogisticRegression(C=1e4).fit(self._x(score), y)
        return self

    def __call__(self, score):
        return self.lr.predict_proba(self._x(score))[:, 1]


def raw_score(model, X):
    """Ranking score of a fitted candidate (the decision threshold applies to this)."""
    if isinstance(model, Persistence):
        return model.decision_score(X)
    return model.predict_proba(X)[:, 1]


def predicted_final_log_pc(current_log_pc, is_yes):
    """Kelvins-style final-risk estimate: latest Pc for YES events, capped just under 1e-6 for NO."""
    current = np.clip(current_log_pc, F.RISK_FLOOR, None)
    return np.where(is_yes, np.maximum(current, F.HIGH_RISK_LOG10),
                    np.minimum(current, F.HIGH_RISK_LOG10 - 0.001))
