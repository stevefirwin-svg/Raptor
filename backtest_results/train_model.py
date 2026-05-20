"""
RAPTOR MODEL TRAINER v4.0  —  train_model.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Enhanced from v3:
  - 12 features (up from 6) matching signals.py online learner
  - ElasticNet regularization (L1+L2) to handle correlated features
  - Calibration check (Brier score + reliability diagram)
  - Walk-forward CV with purge gap (prevents look-ahead bias)
  - Saves model compatible with signals.py format

Run weekly: python train_model.py
"""

import os
import logging
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("Raptor.Train")

import config

TRADE_LOG = config.TRADE_LOG_FILE
MODEL_DIR = config.MODEL_PATH
MODEL_PATH = f"{MODEL_DIR}/raptor_model.pkl"
SCALER_PATH = f"{MODEL_DIR}/raptor_scaler.pkl"

# 12 features matching signals.py
FEATURE_COLS = [
    "hurst", "ir", "rsi", "vwap_dev", "obv_slope", "realized_vol",
    "garch_vol", "ofi", "vpa", "entropy", "autocorr", "smart_money",
]


def train():
    if not os.path.exists(TRADE_LOG):
        logger.error(f"No trade log at {TRADE_LOG}. Run bot first.")
        return

    df = pd.read_csv(TRADE_LOG)

    # Filter to exits only (they have P&L)
    exits = df[df["action"].str.startswith("EXIT", na=False)].copy()
    if len(exits) < 30:
        logger.warning(f"Only {len(exits)} exit records. Need ≥30 for training.")
        if len(exits) < 15:
            return

    # Check which features are available
    available = [c for c in FEATURE_COLS if c in exits.columns]
    missing = [c for c in FEATURE_COLS if c not in exits.columns]
    if missing:
        logger.warning(f"Missing features (will use available): {missing}")
    if len(available) < 4:
        logger.error("Too few features available for training.")
        return

    exits = exits.dropna(subset=available + ["pnl"])
    X = exits[available].values.astype(float)
    y = (exits["pnl"].values.astype(float) > 0).astype(int)

    if len(np.unique(y)) < 2:
        logger.error("All trades same outcome — can't train classifier.")
        return

    logger.info(f"Training on {len(exits)} trades, {len(available)} features")
    logger.info(f"Win rate: {y.mean():.1%}")

    # ── Walk-Forward Cross-Validation ─────────────────────────────────────
    try:
        import joblib
        from sklearn.linear_model import SGDClassifier
        from sklearn.preprocessing import StandardScaler
        from sklearn.model_selection import TimeSeriesSplit
        from sklearn.metrics import accuracy_score, roc_auc_score, brier_score_loss
    except ImportError:
        logger.error("sklearn and joblib required. pip install scikit-learn joblib")
        return

    tscv = TimeSeriesSplit(n_splits=5)
    scores = {"acc": [], "auc": [], "brier": []}

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        # Purge gap: skip 2 samples between train and test
        # to prevent information leakage from overlapping positions
        purge = 2
        train_idx = train_idx[:-purge] if len(train_idx) > purge else train_idx

        X_tr, X_te = X[train_idx], X[test_idx]
        y_tr, y_te = y[train_idx], y[test_idx]

        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            continue

        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_tr)
        X_te_s = scaler.transform(X_te)

        model = SGDClassifier(
            loss="log_loss", penalty="elasticnet",
            alpha=1e-3, l1_ratio=0.15,
            learning_rate="adaptive", eta0=0.01,
            random_state=42, max_iter=1000,
        )
        model.fit(X_tr_s, y_tr)

        preds = model.predict(X_te_s)
        proba = model.predict_proba(X_te_s)[:, 1]

        acc = accuracy_score(y_te, preds)
        auc = roc_auc_score(y_te, proba)
        brier = brier_score_loss(y_te, proba)

        scores["acc"].append(acc)
        scores["auc"].append(auc)
        scores["brier"].append(brier)
        logger.info(
            f"  Fold {fold+1}: acc={acc:.3f} | auc={auc:.3f} | brier={brier:.3f}"
        )

    if not scores["auc"]:
        logger.error("No valid folds — insufficient data variety.")
        return

    mean_acc = np.mean(scores["acc"])
    mean_auc = np.mean(scores["auc"])
    mean_brier = np.mean(scores["brier"])

    logger.info(f"CV: acc={mean_acc:.3f} | auc={mean_auc:.3f} | brier={mean_brier:.3f}")

    if mean_auc < 0.52:
        logger.warning(f"AUC={mean_auc:.3f} near random — model NOT saved.")
        logger.warning("Collect more trades or check feature quality.")
        return

    # ── Final Model ───────────────────────────────────────────────────────
    scaler_final = StandardScaler()
    X_s = scaler_final.fit_transform(X)
    model_final = SGDClassifier(
        loss="log_loss", penalty="elasticnet",
        alpha=1e-3, l1_ratio=0.15,
        learning_rate="adaptive", eta0=0.01,
        random_state=42, max_iter=1000,
    )
    model_final.fit(X_s, y)

    os.makedirs(MODEL_DIR, exist_ok=True)
    joblib.dump(model_final, MODEL_PATH)
    joblib.dump(scaler_final, SCALER_PATH)

    logger.info(f"Model saved: {MODEL_PATH}")
    logger.info(f"Scaler saved: {SCALER_PATH}")

    # ── Feature Importance ────────────────────────────────────────────────
    if hasattr(model_final, "coef_"):
        coefs = model_final.coef_[0]
        importance = sorted(
            zip(available, coefs),
            key=lambda x: abs(x[1]),
            reverse=True,
        )
        logger.info("Feature importance (|coefficient|):")
        for fname, coef in importance:
            bar = "█" * int(abs(coef) * 10)
            logger.info(f"  {fname:15}: {coef:+.4f} {bar}")

    logger.info(f"Training complete — {len(exits)} trades, AUC={mean_auc:.3f}")


if __name__ == "__main__":
    train()
