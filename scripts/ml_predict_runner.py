#!/usr/bin/env python3
"""Production entry point for Stock Truth ML.

Normalizes stock and benchmark timestamps to trading calendar dates before
calling the core model. Repository candles use date-only midnight UTC while
public benchmark feeds may include a non-zero UTC time for the same session.
"""

from __future__ import annotations

import pandas as pd
import ml_predict as core

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
