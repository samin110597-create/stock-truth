# Stock Truth ML model

The dashboard uses a deliberately conservative, open-source machine-learning layer built with scikit-learn. It trains only on completed daily OHLCV bars already produced by the repository pipeline; API credentials stay in GitHub Actions secrets and never reach the browser.

## What it predicts

For 5-session and 21-session horizons the model estimates:

- probability that the forward return is positive;
- a median forward return estimate;
- a lower and upper quantile range;
- out-of-sample validation metrics and a model status.

The dashboard must not present the forecast as certainty. When the walk-forward validation does not demonstrate sufficient improvement over a naive baseline, the public result is **NO VERIFIED EDGE**.

## Validation design

The model uses chronological expanding-window validation (`TimeSeriesSplit`) with a gap equal to the forecast horizon. This reduces leakage from overlapping forward-return labels. The model reports sample count, balanced accuracy, ROC-AUC, Brier score, and Brier skill relative to the historical positive-return rate.

A forecast becomes `VERIFIED EDGE` only when the holdout sample and skill thresholds in `scripts/ml_predict.py` are met. Otherwise it remains `NO VERIFIED EDGE`. This gate is intentionally strict because financial series are noisy and regime-dependent.

## Features

Features are derived from point-in-time market history, including multi-horizon returns, moving-average distances, RSI, ATR, realized volatility, volume behavior, gap/range structure, drawdown, and 52-week position. When `docs/data/SPY.json` exists, benchmark-relative returns, beta, correlation and benchmark volatility are also used.

## Important limitations

This is a research aid, not a guarantee of future returns. Historical validation can degrade after regime changes. Earnings, corporate actions, macro shocks, data revisions and unusual liquidity can invalidate a forecast. The model should be interpreted together with the dashboard's data-integrity, valuation, fundamental and risk sections.

## Data integrity priorities

For best results, keep several years of split-adjusted daily OHLCV, use closed bars only, retain an independent candle cross-check, and avoid point-in-time leakage from fundamentals or analyst data. If those datasets are later added to the ML model, only values that were actually known on each historical date should be used.
