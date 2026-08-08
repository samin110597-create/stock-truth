# Deployment

Stock Truth is a static GitHub Pages dashboard backed by JSON generated in GitHub Actions.

## Secrets

Keep vendor credentials only in **Settings → Secrets and variables → Actions**. The data workflow reads `FINNHUB_KEY`, `TWELVE_KEY`, `FMP_KEY`, `POLYGON_KEY`, and `ALPHAVANTAGE_KEY`. They must never be placed in `docs/index.html`, committed JSON, query strings in browser JavaScript, or browser storage.

## Pages

Set **Settings → Pages → Build and deployment → Source** to **GitHub Actions**. Both the market-data workflow and ML workflow deploy the `docs` directory after successful generation.

## Model data

The daily ML workflow reads completed daily bars from `docs/data/*.json` and writes public model outputs to `docs/data/ml/*.json`. Model files contain predictions and validation metrics only; they contain no API secrets or fitted model binaries.

## Updating the watchlist

Edit `watchlist.json`. The visible tickers come from this file. SPY is used as a benchmark by the ML workflow when its data file is available and does not need to be displayed as a normal watchlist holding.
