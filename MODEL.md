# Stock Truth — model, accuracy and security

Stock Truth is an evidence dashboard, not an automated trading system. The public browser reads committed JSON snapshots only. API credentials stay in GitHub Actions Secrets and are never requested from visitors or stored in browser JavaScript/localStorage.

## What the ML model does

`scripts/ml_predict.py` trains from completed daily candles and writes `docs/data/ml/<TICKER>.json`.

It estimates two horizons:

- 5 trading sessions
- 21 trading sessions

For each horizon it reports probability of a positive return, median expected return, a 20th–80th percentile return interval, walk-forward metrics, and feature importance/sensitivity.

The direction ensemble uses scikit-learn histogram gradient boosting plus regularized logistic regression. Return ranges use quantile histogram gradient boosting. The model uses expanding `TimeSeriesSplit` validation with a gap equal to the forecast horizon to reduce label leakage.

## Edge gate

A forecast is not promoted merely because a model produced a number. The dashboard can deliberately display **NO VERIFIED EDGE**. The current gate requires enough out-of-sample observations and improvement over a naive historical-probability baseline, including minimum balanced accuracy and Brier skill.

This is important: a model that fails its holdout test is evidence that the current feature set has not demonstrated usable predictive skill for that ticker/horizon.

## Inputs

Core features include multi-horizon returns, moving-average distance, RSI, ATR, realized volatility, volume behavior, drawdown and 52-week range position.

If a committed `SPY.json` is available, it is used as the benchmark. Otherwise the GitHub Action makes a best-effort public yfinance SPY history request. Benchmark features include relative strength, beta and correlation. No benchmark API key is required.

## Accuracy rules

- Technical calculations use closed daily bars, never a forming daily candle.
- A newer quote may be displayed as a snapshot but is not injected into closed-bar RSI/MACD/pattern calculations.
- Candle-source disagreement lowers confidence.
- Missing data is displayed as missing rather than filled with invented values.
- Model validation is time-ordered, not random train/test shuffling.
- Predictions do not know future earnings surprises, macro shocks, news or order flow.

## Security architecture

Market-data keys are expected only as repository Actions secrets:

- `FINNHUB_KEY`
- `TWELVE_KEY`
- `FMP_KEY`
- `POLYGON_KEY`
- `ALPHAVANTAGE_KEY`

The public dashboard contains no vendor API endpoints, password fields or key prompts. `.github/workflows/ml-prediction.yml` also runs a static security scan on pull requests.

## Deployment

`fetch.yml` refreshes market snapshots and deploys `docs/` to GitHub Pages. `ml-prediction.yml` retrains the ML model after completed sessions and deploys the model snapshots.

One repository setting may still require a manual one-time choice because GitHub does not expose it through this integration: **Settings → Pages → Build and deployment → Source → GitHub Actions**.

## Interpretation

The dashboard is designed to answer:

1. What is true now? — price, trend, momentum, fundamentals, valuation, ownership and risk.
2. What would invalidate the setup? — structural levels and risk context.
3. What has historically happened after similar measurable states? — walk-forward backtests and ML probabilities.
4. Has the prediction method demonstrated out-of-sample skill? — explicit validation metrics and the NO VERIFIED EDGE gate.

It should not be interpreted as certainty, personalized financial advice, or a guarantee of future returns.
