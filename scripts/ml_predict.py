#!/usr/bin/env python3
"""Stock Truth ML prediction pipeline.

Reads committed, closed-bar JSON snapshots from docs/data/*.json.
It does not need API keys. If SPY.json is absent, it may fetch public SPY
history through yfinance solely as a benchmark for relative-strength, beta,
and correlation features; that benchmark is never treated as a prediction.

For each ticker it builds leakage-aware technical features, runs expanding
TimeSeriesSplit out-of-sample tests with a horizon gap, calibrates an ensemble
probability from out-of-fold predictions, fits quantile regressors for an
expected-return interval, and writes a small public JSON file to docs/data/ml.

The output is deliberately allowed to say NO_VERIFIED_EDGE.  A model that
cannot beat a naive historical-probability baseline is not presented as a
useful forecast.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import sklearn

try:
    import yfinance as yf
    HAVE_YF = True
except Exception:
    yf = None
    HAVE_YF = False
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    brier_score_loss,
    mean_absolute_error,
    roc_auc_score,
)
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler

MODEL_VERSION = "stock-truth-ml-v1"
DEFAULT_HORIZONS = (5, 21)
MIN_LABELLED_ROWS = 350
MIN_OOS_ROWS = 100
BASE_FEATURE_COLUMNS = [
    "ret_1",
    "ret_5",
    "ret_21",
    "ret_63",
    "gap_1",
    "range_pct",
    "body_pct",
    "sma20_rel",
    "sma50_rel",
    "sma200_rel",
    "rsi14",
    "atr14_pct",
    "vol20",
    "vol60",
    "volume_ratio20",
    "volume_z20",
    "drawdown63",
    "range_pos252",
    "trend_spread",
]
BENCHMARK_FEATURE_COLUMNS = [
    "bench_ret_5",
    "bench_ret_21",
    "bench_vol20",
    "relative_ret_5",
    "relative_ret_21",
    "beta60",
    "corr60",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_float(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def bars_from_snapshot(obj: Dict[str, Any]) -> pd.DataFrame:
    bars = ((obj.get("candles") or {}).get("bars") or [])
    if not isinstance(bars, list) or not bars:
        raise ValueError("candles.bars is missing or empty")
    rows = []
    for b in bars:
        if not isinstance(b, dict):
            continue
        rows.append(
            {
                "date": b.get("date"),
                "open": safe_float(b.get("open")),
                "high": safe_float(b.get("high")),
                "low": safe_float(b.get("low")),
                "close": safe_float(b.get("close")),
                "volume": safe_float(b.get("volume")),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("no valid candle rows")
    df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True)
    df = df.dropna(subset=["date", "open", "high", "low", "close"])
    df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    if len(df) < 220:
        raise ValueError(f"only {len(df)} usable closed bars; at least 220 required")
    # Keep volume nullable; price-only features still work when a source omits it.
    return df


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    d = series.diff()
    gain = d.clip(lower=0)
    loss = -d.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return atr / df["close"]


def build_features(df: pd.DataFrame, benchmark_df: pd.DataFrame | None = None) -> Tuple[pd.DataFrame, List[str], str | None]:
    x = pd.DataFrame(index=df.index)
    c = df["close"]
    o = df["open"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    x["ret_1"] = c.pct_change(1)
    x["ret_5"] = c.pct_change(5)
    x["ret_21"] = c.pct_change(21)
    x["ret_63"] = c.pct_change(63)
    x["gap_1"] = o / c.shift(1) - 1
    x["range_pct"] = (h - l) / c
    x["body_pct"] = (c - o) / o

    sma20 = c.rolling(20).mean()
    sma50 = c.rolling(50).mean()
    sma200 = c.rolling(200).mean()
    x["sma20_rel"] = c / sma20 - 1
    x["sma50_rel"] = c / sma50 - 1
    x["sma200_rel"] = c / sma200 - 1
    x["trend_spread"] = sma20 / sma200 - 1

    x["rsi14"] = rsi(c, 14) / 100.0
    x["atr14_pct"] = atr_pct(df, 14)

    logret = np.log(c / c.shift(1))
    x["vol20"] = logret.rolling(20).std(ddof=1) * math.sqrt(252)
    x["vol60"] = logret.rolling(60).std(ddof=1) * math.sqrt(252)

    if v.notna().sum() >= 60:
        v20 = v.rolling(20).mean()
        vsd20 = v.rolling(20).std(ddof=1).replace(0, np.nan)
        x["volume_ratio20"] = v / v20
        x["volume_z20"] = (v - v20) / vsd20
    else:
        x["volume_ratio20"] = np.nan
        x["volume_z20"] = np.nan

    high63 = c.rolling(63).max()
    x["drawdown63"] = c / high63 - 1
    lo252 = l.rolling(252).min()
    hi252 = h.rolling(252).max()
    x["range_pos252"] = (c - lo252) / (hi252 - lo252).replace(0, np.nan)

    benchmark_used = None
    feature_columns = list(BASE_FEATURE_COLUMNS)
    if benchmark_df is not None and not benchmark_df.empty:
        # Align benchmark closes by exchange date.  This creates market-regime and
        # benchmark-relative features without using any future observation.
        b = benchmark_df[["date", "close"]].copy().rename(columns={"close": "bench_close"})
        aligned = df[["date"]].merge(b, on="date", how="left")
        bc = aligned["bench_close"].ffill(limit=3)
        bret1 = bc.pct_change(1)
        x["bench_ret_5"] = bc.pct_change(5)
        x["bench_ret_21"] = bc.pct_change(21)
        x["bench_vol20"] = np.log(bc / bc.shift(1)).rolling(20).std(ddof=1) * math.sqrt(252)
        x["relative_ret_5"] = x["ret_5"] - x["bench_ret_5"]
        x["relative_ret_21"] = x["ret_21"] - x["bench_ret_21"]
        sret1 = c.pct_change(1)
        cov = sret1.rolling(60).cov(bret1)
        bvar = bret1.rolling(60).var(ddof=1).replace(0, np.nan)
        x["beta60"] = cov / bvar
        x["corr60"] = sret1.rolling(60).corr(bret1)
        feature_columns += BENCHMARK_FEATURE_COLUMNS
        benchmark_used = "SPY"

    x = x.replace([np.inf, -np.inf], np.nan)
    return x, feature_columns, benchmark_used


def classifier_pair() -> Tuple[Pipeline, Pipeline]:
    tree = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            (
                "model",
                HistGradientBoostingClassifier(
                    loss="log_loss",
                    learning_rate=0.045,
                    max_iter=180,
                    max_leaf_nodes=15,
                    min_samples_leaf=20,
                    l2_regularization=1.0,
                    early_stopping=False,
                    random_state=42,
                ),
            ),
        ]
    )
    linear = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=0.5,
                    class_weight="balanced",
                    max_iter=2500,
                    solver="lbfgs",
                    random_state=42,
                ),
            ),
        ]
    )
    return tree, linear


def regressor(quantile: float = 0.5) -> Pipeline:
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            (
                "model",
                HistGradientBoostingRegressor(
                    loss="quantile",
                    quantile=quantile,
                    learning_rate=0.045,
                    max_iter=180,
                    max_leaf_nodes=15,
                    min_samples_leaf=20,
                    l2_regularization=1.0,
                    early_stopping=False,
                    random_state=42,
                ),
            ),
        ]
    )


def probability_label(p: float, verified: bool) -> str:
    if not verified:
        return "NO VERIFIED EDGE"
    if p >= 0.57:
        return "UP BIAS"
    if p <= 0.43:
        return "DOWN BIAS"
    return "NEUTRAL"


def confidence_grade(metrics: Dict[str, Any], verified: bool) -> str:
    if not verified:
        return "LOW"
    n = int(metrics.get("oos_samples") or 0)
    skill = float(metrics.get("brier_skill") or 0)
    bal = float(metrics.get("balanced_accuracy") or 0)
    if n >= 180 and skill >= 0.05 and bal >= 0.56:
        return "HIGH"
    return "MEDIUM"


def top_permutation_features(model: Pipeline, X_test: pd.DataFrame, y_test: pd.Series, feature_columns: List[str]) -> List[Dict[str, Any]]:
    if len(X_test) < 30 or y_test.nunique() < 2:
        return []
    try:
        pi = permutation_importance(
            model,
            X_test,
            y_test,
            scoring="neg_brier_score",
            n_repeats=8,
            random_state=42,
            n_jobs=1,
        )
        order = np.argsort(pi.importances_mean)[::-1]
        out = []
        for idx in order[:5]:
            val = float(pi.importances_mean[idx])
            if not math.isfinite(val) or val <= 0:
                continue
            out.append({"feature": feature_columns[idx], "importance": val})
        return out
    except Exception:
        return []


def fit_horizon(features: pd.DataFrame, close: pd.Series, horizon: int, feature_columns: List[str]) -> Dict[str, Any]:
    fwd = close.shift(-horizon) / close - 1
    work = features.copy()
    work["target_return"] = fwd
    work = work.dropna(subset=["target_return"])
    # Price-only rows can survive missing volume through the model imputers.
    work = work.loc[work[feature_columns].notna().sum(axis=1) >= len(feature_columns) - 2]

    if len(work) < MIN_LABELLED_ROWS:
        return {
            "horizon_sessions": horizon,
            "status": "INSUFFICIENT_DATA",
            "reason": f"{len(work)} labelled rows; at least {MIN_LABELLED_ROWS} are required",
        }

    X = work[feature_columns].astype(float)
    yret = work["target_return"].astype(float)
    y = (yret > 0).astype(int)
    if y.nunique() < 2:
        return {
            "horizon_sessions": horizon,
            "status": "INSUFFICIENT_CLASS_VARIATION",
            "reason": "historical target contains only one direction",
        }

    # Expanding time-series validation. The gap removes rows whose forward-return
    # target overlaps the first test observations.
    n_splits = 5 if len(work) >= 430 else 4
    splitter = TimeSeriesSplit(n_splits=n_splits, gap=horizon)
    oos_prob = np.full(len(work), np.nan)
    oos_reg = np.full(len(work), np.nan)
    last_fold_model = None
    last_fold_test_idx = None

    for train_idx, test_idx in splitter.split(X):
        ytr = y.iloc[train_idx]
        if ytr.nunique() < 2:
            continue
        tree, linear = classifier_pair()
        tree.fit(X.iloc[train_idx], ytr)
        linear.fit(X.iloc[train_idx], ytr)
        p = (tree.predict_proba(X.iloc[test_idx])[:, 1] + linear.predict_proba(X.iloc[test_idx])[:, 1]) / 2.0
        oos_prob[test_idx] = p

        med = regressor(0.5)
        med.fit(X.iloc[train_idx], yret.iloc[train_idx])
        oos_reg[test_idx] = med.predict(X.iloc[test_idx])

        last_fold_model = tree
        last_fold_test_idx = test_idx

    mask = np.isfinite(oos_prob)
    if int(mask.sum()) < MIN_OOS_ROWS:
        return {
            "horizon_sessions": horizon,
            "status": "INSUFFICIENT_OOS_DATA",
            "reason": f"only {int(mask.sum())} out-of-sample predictions; at least {MIN_OOS_ROWS} required",
        }

    yo = y.to_numpy()[mask]
    po_raw = oos_prob[mask]
    prevalence = float(yo.mean())

    # Calibrate on predictions that were already out-of-sample. This avoids
    # fitting the calibration map on in-sample classifier outputs.
    calibrator = None
    po = po_raw.copy()
    if len(np.unique(yo)) == 2:
        calibrator = LogisticRegression(C=1.0, max_iter=1000, solver="lbfgs", random_state=42)
        calibrator.fit(po_raw.reshape(-1, 1), yo)
        po = calibrator.predict_proba(po_raw.reshape(-1, 1))[:, 1]

    pred = (po >= 0.5).astype(int)
    acc = float(accuracy_score(yo, pred))
    bal = float(balanced_accuracy_score(yo, pred))
    brier = float(brier_score_loss(yo, po))
    baseline_brier = float(brier_score_loss(yo, np.full_like(po, prevalence, dtype=float)))
    baseline_acc = float(max(prevalence, 1 - prevalence))
    auc = float(roc_auc_score(yo, po)) if len(np.unique(yo)) == 2 else None
    brier_skill = (baseline_brier - brier) / baseline_brier if baseline_brier > 0 else 0.0

    reg_mask = np.isfinite(oos_reg)
    reg_mae = float(mean_absolute_error(yret.to_numpy()[reg_mask], oos_reg[reg_mask])) if reg_mask.any() else None
    zero_mae = float(mean_absolute_error(yret.to_numpy()[reg_mask], np.zeros(int(reg_mask.sum())))) if reg_mask.any() else None

    metrics = {
        "oos_samples": int(mask.sum()),
        "walk_forward_splits": n_splits,
        "accuracy": acc,
        "balanced_accuracy": bal,
        "roc_auc": auc,
        "brier": brier,
        "baseline_accuracy": baseline_acc,
        "baseline_brier": baseline_brier,
        "brier_skill": float(brier_skill),
        "positive_rate": prevalence,
        "median_return_mae": reg_mae,
        "zero_return_mae": zero_mae,
    }

    # Conservative gate: calibrated probabilities must improve upon the naive
    # climatology and direction must show at least modest balanced skill.
    verified = bool(
        int(mask.sum()) >= MIN_OOS_ROWS
        and brier_skill >= 0.02
        and bal >= 0.52
        and acc >= baseline_acc - 0.02
    )

    # Fit final models on all labelled history and infer the newest feature row,
    # which has not been used as a target observation.
    X_current = features[feature_columns].iloc[[-1]].astype(float)
    tree, linear = classifier_pair()
    tree.fit(X, y)
    linear.fit(X, y)
    p_raw = float((tree.predict_proba(X_current)[0, 1] + linear.predict_proba(X_current)[0, 1]) / 2.0)
    p_up = float(calibrator.predict_proba(np.array([[p_raw]]))[0, 1]) if calibrator is not None else p_raw

    q_models = {q: regressor(q).fit(X, yret) for q in (0.2, 0.5, 0.8)}
    q_values = [float(q_models[q].predict(X_current)[0]) for q in (0.2, 0.5, 0.8)]
    q_values.sort()

    top_features: List[Dict[str, Any]] = []
    if last_fold_model is not None and last_fold_test_idx is not None:
        top_features = top_permutation_features(last_fold_model, X.iloc[last_fold_test_idx], y.iloc[last_fold_test_idx], feature_columns)

    reason = (
        "walk-forward calibrated model beat the historical-probability baseline"
        if verified
        else "walk-forward tests did not clear the minimum skill gate; prediction is shown for research but not treated as an edge"
    )

    return {
        "horizon_sessions": horizon,
        "status": "OK" if verified else "NO_VERIFIED_EDGE",
        "edge_verified": verified,
        "prediction": probability_label(p_up, verified),
        "prob_up": p_up,
        "expected_return_median": q_values[1],
        "expected_return_p20": q_values[0],
        "expected_return_p80": q_values[2],
        "confidence": confidence_grade(metrics, verified),
        "metrics": metrics,
        "top_features": top_features,
        "reason": reason,
    }


def model_for_snapshot(path: Path, benchmark_df: pd.DataFrame | None = None) -> Dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    symbol = str(obj.get("symbol") or path.stem).upper()
    out: Dict[str, Any] = {
        "schema_version": 1,
        "model_version": MODEL_VERSION,
        "library": "scikit-learn",
        "library_version": sklearn.__version__,
        "symbol": symbol,
        "generated_at": utc_now(),
        "source_snapshot_generated_at": obj.get("generated_at"),
        "closed_bars_only": True,
        "features": [],
        "warnings": [
            "This model estimates conditional probabilities from historical closed bars; it cannot know future news, earnings surprises, macro shocks, or order flow.",
            "A probability is not a guarantee and NO_VERIFIED_EDGE is a valid model outcome.",
        ],
    }
    try:
        df = bars_from_snapshot(obj)
        use_benchmark = benchmark_df if symbol != "SPY" else None
        features, feature_columns, benchmark_used = build_features(df, use_benchmark)
        out["features"] = feature_columns
        out["benchmark"] = benchmark_used
        if benchmark_used is None and symbol != "SPY":
            out["warnings"].append("SPY.json was not available, so benchmark-relative and beta/correlation features were omitted.")
        out["last_signal_date"] = df["date"].iloc[-1].date().isoformat()
        out["bars_used"] = int(len(df))
        out["horizons"] = {f"{h}d": fit_horizon(features, df["close"], h, feature_columns) for h in DEFAULT_HORIZONS}
        statuses = [v.get("status") for v in out["horizons"].values()]
        out["status"] = "OK" if any(s == "OK" for s in statuses) else "NO_VERIFIED_EDGE" if any(s == "NO_VERIFIED_EDGE" for s in statuses) else "INSUFFICIENT_DATA"
    except Exception as exc:
        out["status"] = "ERROR"
        out["error"] = str(exc)
        out["horizons"] = {}
    return out




def public_spy_benchmark() -> pd.DataFrame | None:
    """Best-effort public SPY benchmark when the repository has no SPY.json.

    This uses yfinance only inside GitHub Actions. No key or browser-side request
    is involved. auto_adjust=False keeps the OHLC convention aligned with the
    repository snapshots; only the close series is used for benchmark features.
    """
    if not HAVE_YF:
        return None
    try:
        hist = yf.Ticker("SPY").history(period="5y", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        out = pd.DataFrame({
            "date": pd.to_datetime(hist.index, utc=True),
            "open": pd.to_numeric(hist["Open"], errors="coerce").to_numpy(),
            "high": pd.to_numeric(hist["High"], errors="coerce").to_numpy(),
            "low": pd.to_numeric(hist["Low"], errors="coerce").to_numpy(),
            "close": pd.to_numeric(hist["Close"], errors="coerce").to_numpy(),
            "volume": pd.to_numeric(hist["Volume"], errors="coerce").to_numpy(),
        })
        out = out.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last")
        return out.reset_index(drop=True) if len(out) >= 220 else None
    except Exception as exc:
        print(f"warning: public SPY benchmark fetch failed: {exc}")
        return None

def eligible_snapshot(path: Path) -> bool:
    if path.parent.name == "ml":
        return False
    if path.name == "index.json":
        return False
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return isinstance((obj.get("candles") or {}).get("bars"), list)
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="docs/data")
    ap.add_argument("--output-dir", default="docs/data/ml")
    ap.add_argument("--symbol", action="append", help="optional ticker filter; may be repeated")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    wanted = {s.upper() for s in (args.symbol or [])}

    snapshots = [p for p in sorted(data_dir.glob("*.json")) if eligible_snapshot(p)]
    if wanted:
        snapshots = [p for p in snapshots if p.stem.upper() in wanted]

    benchmark_df = None
    spy_path = data_dir / "SPY.json"
    if spy_path.exists() and eligible_snapshot(spy_path):
        try:
            benchmark_df = bars_from_snapshot(json.loads(spy_path.read_text(encoding="utf-8")))
            print("benchmark: SPY from committed repository snapshot")
        except Exception as exc:
            print(f"warning: could not load SPY benchmark: {exc}")
    if benchmark_df is None:
        benchmark_df = public_spy_benchmark()
        if benchmark_df is not None:
            print("benchmark: SPY from public yfinance history")
        else:
            print("warning: SPY benchmark unavailable; relative-strength/beta features will be omitted")

    results = []
    for path in snapshots:
        model = model_for_snapshot(path, benchmark_df=benchmark_df)
        dest = output_dir / f"{model['symbol']}.json"
        dest.write_text(json.dumps(model, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        results.append({
            "symbol": model["symbol"],
            "status": model.get("status"),
            "generated_at": model.get("generated_at"),
            "file": dest.name,
        })
        print(f"{model['symbol']}: {model.get('status')}")

    index = {
        "schema_version": 1,
        "model_version": MODEL_VERSION,
        "generated_at": utc_now(),
        "library": "scikit-learn",
        "tickers": results,
    }
    (output_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(results)} model snapshot(s) to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
