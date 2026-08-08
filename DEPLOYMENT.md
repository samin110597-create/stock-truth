# Deployment

Stock Truth is a static GitHub Pages dashboard backed by JSON generated in GitHub Actions.

## Secrets

Keep vendor credentials only in **Settings → Secrets and variables → Actions**. The data workflow reads `FINNHUB_KEY`, `TWELVE_KEY`, `FMP_KEY`, `POLYGON_KEY`, and `ALPHAVANTAGE_KEY`. They must never be placed in `docs/index.html`, committed JSON, browser query strings, or browser storage.

## Pages

The repository keeps the existing branch-based GitHub Pages setup that publishes the `docs` folder from `main`. The workflows only update files under `docs/`; GitHub Pages then publishes those committed changes through the repository's existing Pages configuration.

## Model data

The ML workflow reads completed daily bars from `docs/data/*.json` and writes public model outputs to `docs/data/ml/*.json`. Model files contain predictions and validation metrics only; they contain no API secrets or fitted model binaries.

The ML workflow runs automatically after completed U.S. sessions and can also be started manually if ever needed. A change to the ML workflow itself also triggers a build, which is used for initial activation.

## Updating the watchlist

Edit `watchlist.json`. The visible tickers come from this file. SPY is used as a benchmark by the ML model when available and does not need to be displayed as a normal watchlist ticker.
