#!/usr/bin/env python3
"""
Stock data pipeline — live data only, no simulation.

Reads watchlist.json, pulls from Finnhub, Twelve Data, Polygon, FMP,
Alpha Vantage, and yfinance, cross-verifies candles between two
independent sources, computes indicators, and writes one JSON per
ticker to docs/data/ for the GitHub Pages dashboard.

Rules enforced here:
  * A failed fetch is recorded as {"error": ...} — never replaced
    with invented numbers.
  * Every block carries its own source name and fetched_at timestamp.
  * Slow-moving data (fundamentals, earnings history, macro) is
    cached and only refreshed when stale, to protect free-tier quotas.
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    import yfinance as yf
    HAVE_YF = True
except Exception:
    HAVE_YF = False

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
OUT.mkdir(parents=True, exist_ok=True)

KEYS = {
    "finnhub": os.environ.get("FINNHUB_KEY", ""),
    "twelve": os.environ.get("TWELVE_KEY", ""),
    "polygon": os.environ.get("POLYGON_KEY", ""),
    "fmp": os.environ.get("FMP_KEY", ""),
    "av": os.environ.get("ALPHAVANTAGE_KEY", ""),
}

# Cache freshness windows
FRESH = {
    "fundamentals_days": 7,   # FMP statements: refresh weekly
    "earnings_days": 7,       # Alpha Vantage earnings history: weekly
    "yf_hours": 6,            # yfinance extras: a few times per day
    "macro_hours": 20,        # macro series: daily
}
AV_BUDGET = 20                # max Alpha Vantage calls per run (limit 25/day)
av_calls = 0

NOW = datetime.now(timezone.utc)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url, params=None, timeout=30):
    r = requests.get(url, params=params, timeout=timeout,
                     headers={"User-Agent": "stock-pipeline/1.0"})
    r.raise_for_status()
    return r.json()


# FMP moved new accounts to /stable/ (Aug 2025); old keys still use /api/v3/.
# Try stable first, fall back to v3, and remember which one the key accepts.
FMP_MODE = {"mode": ""}


def fmp_get(name, sym, extra=None):
    key = KEYS["fmp"]
    p_stable = {"symbol": sym, "apikey": key, **(extra or {})}
    p_v3 = {"apikey": key, **(extra or {})}

    def attempt(url, params):
        j = get(url, params)
        if isinstance(j, dict) and j.get("Error Message"):
            raise ValueError(j["Error Message"])
        return j

    stable = ("https://financialmodelingprep.com/stable/" + name, p_stable)
    v3 = (f"https://financialmodelingprep.com/api/v3/{name}/{sym}", p_v3)
    if FMP_MODE["mode"] == "v3":
        return attempt(*v3)
    if FMP_MODE["mode"] == "stable":
        return attempt(*stable)
    try:
        j = attempt(*stable)
        FMP_MODE["mode"] = "stable"
        return j
    except Exception:
        j = attempt(*v3)
        FMP_MODE["mode"] = "v3"
        return j


def is_fresh(block, hours=None, days=None):
    """True if a previously cached block exists, has no error, and is recent."""
    if not block or block.get("error") or "fetched_at" in block is False:
        return False
    ts = block.get("fetched_at")
    if not ts:
        return False
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    limit = timedelta(hours=hours) if hours else timedelta(days=days)
    return NOW - t < limit


# ---------------- indicator math (pure python, real candles in) ----------------

def ema(vals, p):
    out = [None] * len(vals)
    k = 2 / (p + 1)
    prev = None
    for i, v in enumerate(vals):
        if i == p - 1:
            prev = sum(vals[:p]) / p
            out[i] = prev
        elif i >= p:
            prev = v * k + prev * (1 - k)
            out[i] = prev
    return out


def rsi(closes, p=14):
    out = [None] * len(closes)
    g = l = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        if i <= p:
            if ch > 0:
                g += ch
            else:
                l -= ch
            if i == p:
                g /= p
                l /= p
                out[i] = 100 - 100 / (1 + (1e9 if l == 0 else g / l))
        else:
            g = (g * (p - 1) + max(ch, 0)) / p
            l = (l * (p - 1) + max(-ch, 0)) / p
            out[i] = 100 - 100 / (1 + (1e9 if l == 0 else g / l))
    return out


def macd(closes):
    e12, e26 = ema(closes, 12), ema(closes, 26)
    line = [a - b if a is not None and b is not None else None
            for a, b in zip(e12, e26)]
    valid = [v for v in line if v is not None]
    sig_valid = ema(valid, 9)
    signal, j = [None] * len(line), 0
    for i, v in enumerate(line):
        if v is not None:
            signal[i] = sig_valid[j]
            j += 1
    return line, signal


def atr(bars, p=14):
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["high"] - b["low"])
        else:
            pc = bars[i - 1]["close"]
            trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    out, prev = [None] * len(bars), None
    for i, tr in enumerate(trs):
        if i == p - 1:
            prev = sum(trs[:p]) / p
            out[i] = prev
        elif i >= p:
            prev = (prev * (p - 1) + tr) / p
            out[i] = prev
    return out


def swing_levels(bars, price, win=5):
    highs, lows = [], []
    for i in range(win, len(bars) - win):
        seg = bars[i - win:i + win + 1]
        if bars[i]["high"] == max(b["high"] for b in seg):
            highs.append(bars[i]["high"])
        if bars[i]["low"] == min(b["low"] for b in seg):
            lows.append(bars[i]["low"])
    res = sorted({h for h in highs if h > price})[:3]
    sup = sorted({l for l in lows if l < price}, reverse=True)[:3]
    return {"resistance": res, "support": sup}


def trend_read(bars):
    if not bars or len(bars) < 25:
        return {"label": "INSUFFICIENT DATA", "detail": "not enough bars"}
    closes = [b["close"] for b in bars]
    e20 = ema(closes, 20)
    px, above = closes[-1], e20[-1] is not None and closes[-1] > e20[-1]
    recent, older = bars[-12:], bars[-24:-12]
    hh = max(b["high"] for b in recent) > max(b["high"] for b in older)
    hl = min(b["low"] for b in recent) > min(b["low"] for b in older)
    if above and hh and hl:
        return {"label": "BULLISH", "detail": "above EMA20, higher highs & lows"}
    if not above and not hh and not hl:
        return {"label": "BEARISH", "detail": "below EMA20, lower highs & lows"}
    return {"label": "MIXED",
            "detail": f"{'above' if above else 'below'} EMA20, HH:{'Y' if hh else 'N'} HL:{'Y' if hl else 'N'}"}


def aggregate(bars, mode):
    out = {}
    for b in bars:
        if mode == "month":
            k = b["date"][:7]
        else:
            d = datetime.strptime(b["date"], "%Y-%m-%d")
            monday = d - timedelta(days=d.weekday())
            k = monday.strftime("%Y-%m-%d")
        if k not in out:
            out[k] = dict(b, date=k)
        else:
            m = out[k]
            m["high"] = max(m["high"], b["high"])
            m["low"] = min(m["low"], b["low"])
            m["close"] = b["close"]
            m["volume"] += b["volume"]
    return list(out.values())


# ---------------- fetchers (each returns a tagged block or an error block) ----------------

def block(source, **data):
    return {"source": source, "fetched_at": now_iso(), **data}


def err(source, e):
    return {"source": source, "fetched_at": now_iso(), "error": str(e)}


def fetch_quote(sym):
    if not KEYS["finnhub"]:
        return err("finnhub", "no key")
    try:
        j = get("https://finnhub.io/api/v1/quote",
                {"symbol": sym, "token": KEYS["finnhub"]})
        if not j.get("c"):
            raise ValueError("no quote returned")
        return block("finnhub", current=j["c"], change=j.get("d"),
                     change_pct=j.get("dp"), high=j.get("h"), low=j.get("l"),
                     open=j.get("o"), prev_close=j.get("pc"),
                     last_trade_unix=j.get("t"))
    except Exception as e:
        return err("finnhub", e)


def fetch_candles_twelve(sym):
    if not KEYS["twelve"]:
        return None, "no key"
    try:
        j = get("https://api.twelvedata.com/time_series",
                {"symbol": sym, "interval": "1day", "outputsize": 600,
                 "apikey": KEYS["twelve"]})
        if j.get("status") == "error":
            raise ValueError(j.get("message"))
        bars = [{"date": v["datetime"], "open": float(v["open"]),
                 "high": float(v["high"]), "low": float(v["low"]),
                 "close": float(v["close"]), "volume": float(v.get("volume") or 0)}
                for v in j.get("values", [])]
        bars.reverse()
        return (bars, None) if bars else (None, "empty series")
    except Exception as e:
        return None, str(e)


def fetch_candles_polygon(sym):
    if not KEYS["polygon"]:
        return None, "no key"
    try:
        frm = (NOW - timedelta(days=900)).strftime("%Y-%m-%d")
        to = NOW.strftime("%Y-%m-%d")
        j = get(f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day/{frm}/{to}",
                {"adjusted": "true", "sort": "asc", "limit": 700,
                 "apiKey": KEYS["polygon"]})
        bars = [{"date": datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                 "open": r["o"], "high": r["h"], "low": r["l"],
                 "close": r["c"], "volume": r.get("v", 0)}
                for r in j.get("results", [])]
        return (bars, None) if bars else (None, "empty series")
    except Exception as e:
        return None, str(e)


def cross_verify(a, b):
    """Compare closes on the last 10 shared dates between two sources."""
    if not a or not b:
        return None
    bmap = {x["date"]: x["close"] for x in b}
    shared = [(x["close"], bmap[x["date"]]) for x in a[-30:] if x["date"] in bmap]
    shared = shared[-10:]
    if len(shared) < 5:
        return {"status": "INSUFFICIENT_OVERLAP", "checked": len(shared)}
    max_dev = max(abs(x - y) / y for x, y in shared if y)
    if max_dev <= 0.003:
        return {"status": "AGREE", "max_deviation_pct": round(max_dev * 100, 3),
                "checked": len(shared)}
    return {"status": "DISAGREE", "max_deviation_pct": round(max_dev * 100, 3),
            "checked": len(shared),
            "note": "sources differ — possible adjustment mismatch (splits/dividends)"}


def fetch_market_status():
    if not KEYS["polygon"]:
        return err("polygon", "no key")
    try:
        j = get("https://api.polygon.io/v1/marketstatus/now",
                {"apiKey": KEYS["polygon"]})
        return block("polygon", market=j.get("market"),
                     exchanges=j.get("exchanges"), server_time=j.get("serverTime"))
    except Exception as e:
        return err("polygon", e)


def fetch_fundamentals(sym):
    if not KEYS["fmp"]:
        return err("fmp", "no key")
    try:
        prof = fmp_get("profile", sym)
        prof = prof[0] if isinstance(prof, list) and prof else (prof or {})
        if prof.get("isEtf"):
            return block("fmp", is_etf=True, name=prof.get("companyName"))
        inc = fmp_get("income-statement", sym, {"limit": 6})
        bal = fmp_get("balance-sheet-statement", sym, {"limit": 2})
        cfs = fmp_get("cash-flow-statement", sym, {"limit": 6})
        rat = fmp_get("ratios-ttm", sym)
        rat = rat[0] if isinstance(rat, list) and rat else (rat or {})
        if not inc:
            raise ValueError("no statements (unknown ticker or fund)")
        latest, b0 = inc[0], (bal[0] if bal else {})
        yrs = min(len(inc) - 1, 5)

        def cagr(last, first, n):
            if last and first and last > 0 and first > 0 and n > 0:
                return (last / first) ** (1 / n) - 1
            return None

        eps_now = latest.get("epsdiluted") or latest.get("eps")
        eps_old = (inc[yrs].get("epsdiluted") or inc[yrs].get("eps")) if yrs else None
        fcfs = [c.get("freeCashFlow") for c in cfs if c.get("freeCashFlow") is not None]
        shares = latest.get("weightedAverageShsOutDil") or latest.get("weightedAverageShsOut")
        equity = b0.get("totalStockholdersEquity") or 0
        debt = b0.get("totalDebt") or 0
        rev = latest.get("revenue") or 0
        return block(
            "fmp", is_etf=False,
            name=prof.get("companyName"), sector=prof.get("sector"),
            period=latest.get("date"), currency=latest.get("reportedCurrency"),
            years_of_data=len(inc),
            annual=[{"year": r.get("calendarYear") or (r.get("date") or "")[:4],
                     "revenue": r.get("revenue"), "net_income": r.get("netIncome"),
                     "fcf": next((c.get("freeCashFlow") for c in cfs
                                  if c.get("calendarYear") == r.get("calendarYear")), None)}
                    for r in reversed(inc)],
            eps_ttm_diluted=eps_now,
            rev_cagr_3y=cagr(inc[0].get("revenue"), inc[3].get("revenue"), 3) if len(inc) >= 4 else None,
            rev_cagr_5y=cagr(inc[0].get("revenue"), inc[5].get("revenue"), 5) if len(inc) >= 6 else None,
            eps_cagr=cagr(eps_now, eps_old, yrs),
            fcf_cagr=cagr(fcfs[0], fcfs[-1], len(fcfs) - 1) if len(fcfs) >= 2 else None,
            gross_margin=(latest.get("grossProfit") or 0) / rev if rev else None,
            op_margin=(latest.get("operatingIncome") or 0) / rev if rev else None,
            net_margin=(latest.get("netIncome") or 0) / rev if rev else None,
            roe=rat.get("returnOnEquityTTM") or rat.get("roeTTM"),
            debt_to_equity=(debt / equity) if equity else None,
            current_ratio=rat.get("currentRatioTTM"),
            interest_coverage=((latest.get("operatingIncome") or 0) / latest["interestExpense"])
                if latest.get("interestExpense") else None,
            net_cash=(b0.get("cashAndShortTermInvestments") or 0) - debt,
            shares=shares,
            book_value_per_share=(equity / shares) if shares else None,
            fcf_base=(sum(fcfs[:2]) / 2) if len(fcfs) >= 2 else (fcfs[0] if fcfs else None),
            pe_ttm=rat.get("peRatioTTM") or rat.get("priceEarningsRatioTTM") or rat.get("priceToEarningsRatioTTM"),
            ps_ttm=rat.get("priceToSalesRatioTTM") or rat.get("psRatioTTM"),
            pb_ttm=rat.get("priceToBookRatioTTM") or rat.get("pbRatioTTM"),
            peg_ttm=rat.get("pegRatioTTM") or rat.get("priceEarningsToGrowthRatioTTM"),
        )
    except Exception as e:
        return err("fmp", e)


def fetch_earnings_history(sym):
    """Alpha Vantage EARNINGS: quarterly estimate vs actual vs surprise."""
    global av_calls
    if not KEYS["av"]:
        return err("alphavantage", "no key")
    if av_calls >= AV_BUDGET:
        return err("alphavantage", "daily call budget reached — kept previous cache")
    try:
        av_calls += 1
        j = get("https://www.alphavantage.co/query",
                {"function": "EARNINGS", "symbol": sym, "apikey": KEYS["av"]})
        q = j.get("quarterlyEarnings")
        if not q:
            raise ValueError(j.get("Note") or j.get("Information") or "no earnings data")
        rows = []
        for r in q[:8]:
            rows.append({
                "period": r.get("fiscalDateEnding"),
                "reported": r.get("reportedDate"),
                "eps_actual": r.get("reportedEPS"),
                "eps_estimate": r.get("estimatedEPS"),
                "surprise_pct": r.get("surprisePercentage"),
            })
        beats = sum(1 for r in rows
                    if r["surprise_pct"] not in (None, "None")
                    and float(r["surprise_pct"]) > 0)
        return block("alphavantage", quarters=rows,
                     beats_of_last=f"{beats}/{len(rows)}")
    except Exception as e:
        return err("alphavantage", e)


def fetch_yf_extras(sym):
    """yfinance: the data free REST APIs don't give — analyst targets,
    short interest, institutional ownership. Best-effort; Yahoo fields
    come and go, so every field is optional."""
    if not HAVE_YF:
        return err("yfinance", "yfinance not installed")
    try:
        info = yf.Ticker(sym).info or {}
        keep = {
            "analyst_target_mean": info.get("targetMeanPrice"),
            "analyst_target_low": info.get("targetLowPrice"),
            "analyst_target_high": info.get("targetHighPrice"),
            "analyst_count": info.get("numberOfAnalystOpinions"),
            "recommendation": info.get("recommendationKey"),
            "forward_pe": info.get("forwardPE"),
            "forward_eps": info.get("forwardEps"),
            "shares_short": info.get("sharesShort"),
            "short_pct_of_float": info.get("shortPercentOfFloat"),
            "short_ratio_days_to_cover": info.get("shortRatio"),
            "short_interest_asof_unix": info.get("dateShortInterest"),
            "institutions_pct": info.get("heldPercentInstitutions"),
            "insiders_pct": info.get("heldPercentInsiders"),
            "beta": info.get("beta"),
            "next_earnings_unix": (info.get("earningsTimestamp")
                                   or info.get("earningsTimestampStart")),
        }
        if all(v is None for v in keep.values()):
            raise ValueError("Yahoo returned no usable fields")
        return block("yfinance", **keep,
                     note="Yahoo data: short interest is exchange-reported ~2x/month, "
                          "not live; analyst figures are consensus snapshots")
    except Exception as e:
        return err("yfinance", e)


def fetch_macro(prev):
    """Alpha Vantage macro series — 3 calls, cached ~daily."""
    global av_calls
    if prev and is_fresh(prev, hours=FRESH["macro_hours"]):
        return prev
    if not KEYS["av"]:
        return err("alphavantage", "no key")
    out = {}
    series = [
        ("cpi_yoy_latest", {"function": "CPI", "interval": "monthly"}),
        ("fed_funds_rate", {"function": "FEDERAL_FUNDS_RATE", "interval": "monthly"}),
        ("treasury_10y", {"function": "TREASURY_YIELD", "interval": "monthly",
                          "maturity": "10year"}),
    ]
    try:
        for name, params in series:
            if av_calls >= AV_BUDGET:
                raise ValueError("AV budget reached")
            av_calls += 1
            j = get("https://www.alphavantage.co/query",
                    {**params, "apikey": KEYS["av"]})
            data = j.get("data")
            if not data:
                raise ValueError(j.get("Note") or j.get("Information") or f"no data for {name}")
            latest = data[0]
            entry = {"date": latest["date"], "value": float(latest["value"])}
            if name == "cpi_yoy_latest" and len(data) >= 13:
                yr_ago = float(data[12]["value"])
                entry["yoy_pct"] = round((entry["value"] / yr_ago - 1) * 100, 2)
            out[name] = entry
        return block("alphavantage", **out)
    except Exception as e:
        return prev if prev and not prev.get("error") else err("alphavantage", e)


# ---------------- per-ticker assembly ----------------

def build_ticker(sym, prev):
    prev = prev or {}
    t = {"symbol": sym, "generated_at": now_iso()}

    t["quote"] = fetch_quote(sym)

    td_bars, td_err = fetch_candles_twelve(sym)
    pg_bars, pg_err = fetch_candles_polygon(sym)
    bars = td_bars or pg_bars
    primary = "twelvedata" if td_bars else ("polygon" if pg_bars else None)
    verification = cross_verify(td_bars, pg_bars) if td_bars and pg_bars else None
    confidence = ("HIGH" if verification and verification["status"] == "AGREE"
                  else "LOW" if verification and verification["status"] == "DISAGREE"
                  else "MEDIUM" if bars else "NONE")

    if bars:
        closes = [b["close"] for b in bars]
        price = (t["quote"].get("current")
                 if not t["quote"].get("error") else closes[-1])
        e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
        r = rsi(closes)
        m_line, m_sig = macd(closes)
        a = atr(bars)
        vol20 = sum(b["volume"] for b in bars[-21:-1]) / 20 if len(bars) > 21 else None
        yr = bars[-252:]
        t["technicals"] = block(
            f"computed from {primary} candles",
            candle_source=primary,
            candle_errors={"twelvedata": td_err, "polygon": pg_err},
            cross_verification=verification,
            data_confidence=confidence,
            n_bars=len(bars), latest_bar=bars[-1]["date"],
            price_used=price,
            ema20=e20[-1], ema50=e50[-1], ema200=e200[-1],
            rsi14=r[-1], macd=m_line[-1], macd_signal=m_sig[-1], atr14=a[-1],
            volume_last=bars[-1]["volume"], volume_avg20=vol20,
            hi_52wk=max(b["high"] for b in yr), lo_52wk=min(b["low"] for b in yr),
            levels=swing_levels(bars, price),
            trend={"daily": trend_read(bars[-120:]),
                   "weekly": trend_read(aggregate(bars, "week")),
                   "monthly": trend_read(aggregate(bars, "month"))},
            chart=[{"date": b["date"], "close": b["close"], "volume": b["volume"],
                    "ema20": e20[i], "ema50": e50[i], "ema200": e200[i]}
                   for i, b in enumerate(bars)][-260:],
        )
    else:
        t["technicals"] = {"source": "none", "fetched_at": now_iso(),
                           "error": f"twelvedata: {td_err}; polygon: {pg_err}",
                           "data_confidence": "NONE"}

    prev_f = prev.get("fundamentals")
    t["fundamentals"] = (prev_f if is_fresh(prev_f, days=FRESH["fundamentals_days"])
                         else fetch_fundamentals(sym))

    prev_e = prev.get("earnings_history")
    t["earnings_history"] = (prev_e if is_fresh(prev_e, days=FRESH["earnings_days"])
                             else fetch_earnings_history(sym))

    prev_y = prev.get("yahoo_extras")
    t["yahoo_extras"] = (prev_y if is_fresh(prev_y, hours=FRESH["yf_hours"])
                         else fetch_yf_extras(sym))
    return t


def main():
    wl_path = ROOT / "watchlist.json"
    watchlist = json.loads(wl_path.read_text())["tickers"]
    watchlist = [s.strip().upper() for s in watchlist if s.strip()]

    prev_index = {}
    idx_path = OUT / "index.json"
    if idx_path.exists():
        try:
            prev_index = json.loads(idx_path.read_text())
        except Exception:
            prev_index = {}

    results = []
    for sym in watchlist:
        prev = {}
        p = OUT / f"{sym}.json"
        if p.exists():
            try:
                prev = json.loads(p.read_text())
            except Exception:
                prev = {}
        print(f"-> {sym}", flush=True)
        data = build_ticker(sym, prev)
        p.write_text(json.dumps(data, indent=1))
        results.append(sym)
        time.sleep(9)  # respect Twelve Data (8/min) and Polygon (5/min)

    index = {
        "generated_at": now_iso(),
        "tickers": results,
        "market_status": fetch_market_status(),
        "macro": fetch_macro(prev_index.get("macro")),
        "sources_configured": {k: bool(v) for k, v in KEYS.items()},
        "yfinance_available": HAVE_YF,
        "alpha_vantage_calls_this_run": av_calls,
    }
    idx_path.write_text(json.dumps(index, indent=1))
    print(f"Done: {len(results)} tickers, AV calls used: {av_calls}")


if __name__ == "__main__":
    sys.exit(main())
