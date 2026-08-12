#!/usr/bin/env python3
"""Production entry point for Stock Truth verified-research ML v2.

Normalizes stock and benchmark timestamps to trading calendar dates before
calling the v2 core model. The v2 model uses max available adjusted history,
purged walk-forward validation, sequential calibration, and a dedicated 1Y
horizon while keeping UNVERIFIED research forecasts visible.
"""

from __future__ import annotations

import pandas as pd
import ml_predict_v2 as core

_original_build_features = core.build_features


def build_features_calendar_aligned(df, benchmark_df=None):
    stock = df.copy()
    stock["date"] = pd.to_datetime(stock["date"], errors="coerce", utc=True).dt.normalize()

    benchmark = benchmark_df
    if benchmark_df is not None and not benchmark_df.empty:
        benchmark = benchmark_df.copy()
        benchmark["date"] = pd.to_datetime(
            benchmark["date"], errors="coerce", utc=True
        ).dt.normalize()

    return _original_build_features(stock, benchmark)


core.build_features = build_features_calendar_aligned


if __name__ == "__main__":
    raise SystemExit(core.main())
