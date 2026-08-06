#!/usr/bin/env python3
"""
Stock data pipeline — keys live in GitHub Secrets, never in a browser.

Reads watchlist.json, pulls from yfinance, Finnhub, Twelve Data, Polygon,
FMP and Alpha Vantage, and writes one JSON per ticker into docs/data/.
The GitHub Pages dashboard reads those files, so any visitor sees full
data with no API key of their own.

FUNDAMENTALS ARE THE PRIORITY. They are attempted from four independent
sources in order, and the first one that yields real statements wins:

    1. yfinance   — full statements, no key, most complete
    2. FMP        — statements, /stable then /api/v3
    3. Finnhub    — basic financials (ratios, no raw statements)
    4. Alpha Vantage OVERVIEW — ratios and margins

Only if all four fail is the block written as an error. Every field
records which source produced it.
"""

import json, math, os, sys, time
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

KEYS = {k: os.environ.get(e, "") for k, e in [
    ("finnhub", "FINNHUB_KEY"), ("twelve", "TWELVE_KEY"),
    ("polygon", "POLYGON_KEY"), ("fmp", "FMP_KEY"), ("av", "ALPHAVANTAGE_KEY")]}

NOW = datetime.now(timezone.utc)
AV_BUDGET = 18
av_calls = 0


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url, params=None, timeout=30):
    r = requests.get(url, params=params, timeout=timeout,
                     headers={"User-Agent": "stock-pipeline/2.0"})
    r.raise_for_status()
    j = r.json()
    if isinstance(j, dict):
        for k in ("Error Message", "Note", "Information"):
            if j.get(k):
                raise ValueError(str(j[k])[:200])
    return j


def num(v):
    """Coerce to a finite float or None — never NaN, which breaks JSON."""
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def safe_div(a, b):
    a, b = num(a), num(b)
    return a / b if (a is not None and b not in (None, 0)) else None


def cagr(end, start, years):
    end, start = num(end), num(start)
    if end and start and end > 0 and start > 0 and years > 0:
        return (end / start) ** (1 / years) - 1
    return None


def is_fresh(block, hours=None, days=None):
    if not block or block.get("error") or not block.get("fetched_at"):
        return False
    try:
        t = datetime.strptime(block["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return NOW - t < (timedelta(hours=hours) if hours else timedelta(days=days))


def block(source, **d):
    return {"source": source, "fetched_at": now_iso(), **d}


def err(source, e):
    return {"source": source, "fetched_at": now_iso(), "error": str(e)[:300]}


# ------------------------------------------------------------------ candles

def candles_twelve(sym):
    if not KEYS["twelve"]:
        return None, "no key"
    try:
        j = get("https://api.twelvedata.com/time_series",
                {"symbol": sym, "interval": "1day", "outputsize": 900,
                 "order": "ASC", "apikey": KEYS["twelve"]})
        if j.get("status") == "error":
            raise ValueError(j.get("message"))
        bars = [{"date": v["datetime"], "open": num(v["open"]), "high": num(v["high"]),
                 "low": num(v["low"]), "close": num(v["close"]), "volume": num(v.get("volume")) or 0}
                for v in j.get("values", [])]
        bars = [b for b in bars if b["close"]]
        return (bars, None) if bars else (None, "empty series")
    except Exception as e:
        return None, str(e)[:200]


def candles_polygon(sym):
    if not KEYS["polygon"]:
        return None, "no key"
    try:
        frm = (NOW - timedelta(days=800)).strftime("%Y-%m-%d")
        to = NOW.strftime("%Y-%m-%d")
        j = get(f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day/{frm}/{to}",
                {"adjusted": "true", "sort": "asc", "limit": 900, "apiKey": KEYS["polygon"]})
        bars = [{"date": datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                 "open": num(r["o"]), "high": num(r["h"]), "low": num(r["l"]),
                 "close": num(r["c"]), "volume": num(r.get("v")) or 0}
                for r in j.get("results", [])]
        return (bars, None) if bars else (None, "empty series")
    except Exception as e:
        return None, str(e)[:200]


def candles_yf(sym):
    if not HAVE_YF:
        return None, "yfinance unavailable"
    try:
        h = yf.Ticker(sym).history(period="3y", interval="1d", auto_adjust=False)
        if h is None or h.empty:
            return None, "empty history"
        bars = []
        for idx, r in h.iterrows():
            c = num(r.get("Close"))
            if not c:
                continue
            bars.append({"date": idx.strftime("%Y-%m-%d"), "open": num(r.get("Open")),
                         "high": num(r.get("High")), "low": num(r.get("Low")),
                         "close": c, "volume": num(r.get("Volume")) or 0})
        return (bars, None) if bars else (None, "no usable rows")
    except Exception as e:
        return None, str(e)[:200]


def cross_verify(a, b):
    if not a or not b:
        return None
    m = {x["date"]: x["close"] for x in b}
    pairs = [(x["close"], m[x["date"]]) for x in a[-40:] if x["date"] in m][-12:]
    if len(pairs) < 5:
        return {"checked": len(pairs), "status": "NO OVERLAP", "worst": None}
    worst = max(abs(p - q) / q * 100 for p, q in pairs if q)
    return {"checked": len(pairs), "worst": round(worst, 4),
            "status": "AGREE" if worst < 0.5 else "MINOR DIVERGENCE" if worst < 2 else "DISAGREE"}


# ------------------------------------------------- fundamentals: four sources

def fund_from_yf(sym):
    """Primary source. yfinance gives full statements with no API key."""
    if not HAVE_YF:
        raise ValueError("yfinance unavailable")
    t = yf.Ticker(sym)
    info = {}
    try:
        info = t.get_info() or {}
    except Exception:
        try:
            info = t.info or {}
        except Exception:
            info = {}
    qt = (info.get("quoteType") or "").upper()
    if qt in ("ETF", "MUTUALFUND", "INDEX", "CURRENCY", "CRYPTOCURRENCY"):
        return block("yfinance", is_etf=True, name=info.get("longName") or info.get("shortName") or sym,
                     quote_type=qt)

    fin = t.financials
    bs = t.balance_sheet
    cf = t.cashflow
    if fin is None or fin.empty:
        raise ValueError("no income statement from yfinance")

    def row(df, *names):
        if df is None or df.empty:
            return []
        for nm in names:
            if nm in df.index:
                return [num(v) for v in df.loc[nm].tolist()]
        return []

    years = [str(c.year) for c in fin.columns]
    rev = row(fin, "Total Revenue", "TotalRevenue")
    ni = row(fin, "Net Income", "NetIncome", "Net Income Common Stockholders")
    gp = row(fin, "Gross Profit", "GrossProfit")
    ebit = row(fin, "Operating Income", "OperatingIncome", "EBIT")
    intexp = row(fin, "Interest Expense", "InterestExpense")
    pretax = row(fin, "Pretax Income", "PretaxIncome", "Income Before Tax")
    taxexp = row(fin, "Tax Provision", "TaxProvision", "Income Tax Expense")

    eq = row(bs, "Stockholders Equity", "StockholdersEquity", "Total Stockholder Equity")
    ta = row(bs, "Total Assets", "TotalAssets")
    tl = row(bs, "Total Liabilities Net Minority Interest", "Total Liab")
    cash = row(bs, "Cash Cash Equivalents And Short Term Investments",
               "CashCashEquivalentsAndShortTermInvestments", "Cash And Cash Equivalents")
    debt = row(bs, "Total Debt", "TotalDebt")
    ca = row(bs, "Current Assets", "Total Current Assets")
    cl = row(bs, "Current Liabilities", "Total Current Liabilities")
    inv = row(bs, "Inventory")

    ocf = row(cf, "Operating Cash Flow", "Total Cash From Operating Activities")
    capex = row(cf, "Capital Expenditure", "Capital Expenditures")
    fcf_row = row(cf, "Free Cash Flow", "FreeCashFlow")
    buyback = row(cf, "Repurchase Of Capital Stock", "Sale Purchase Of Stock")

    n = len(rev)
    if not n:
        raise ValueError("yfinance returned no revenue line")

    def at(a, i=0):
        return a[i] if a and len(a) > i else None

    fcf = []
    for i in range(n):
        v = at(fcf_row, i)
        if v is None:
            o, cx = at(ocf, i), at(capex, i)
            v = (o - abs(cx)) if (o is not None and cx is not None) else None
        fcf.append(v)

    shares = num(info.get("sharesOutstanding")) or num(info.get("impliedSharesOutstanding"))
    eps = num(info.get("trailingEps"))
    if eps is None and at(ni) and shares:
        eps = at(ni) / shares
    equity, debt0, cash0 = at(eq), at(debt) or 0, at(cash) or 0
    tax_rate = safe_div(at(taxexp), at(pretax)) or 0.21
    tax_rate = min(max(tax_rate, 0), 0.45)
    ic = (equity or 0) + debt0 - cash0
    yrs = n - 1
    fcf_ok = [v for v in fcf if v is not None]

    annual = []
    for i in range(n - 1, -1, -1):
        annual.append({"year": years[i] if i < len(years) else None,
                       "revenue": at(rev, i), "net_income": at(ni, i), "fcf": fcf[i],
                       "gross": safe_div(at(gp, i), at(rev, i)),
                       "op": safe_div(at(ebit, i), at(rev, i)),
                       "net": safe_div(at(ni, i), at(rev, i))})

    return block("yfinance", is_etf=False,
        name=info.get("longName") or info.get("shortName") or sym,
        sector=info.get("sector"), industry=info.get("industry"),
        employees=num(info.get("fullTimeEmployees")), period=years[0] if years else None,
        years=n, currency=info.get("financialCurrency") or "USD",
        mcap=num(info.get("marketCap")), shares=shares, annual=annual,
        revenue=at(rev), net_income=at(ni), ebit=at(ebit),
        eps_ttm=eps, bvps=safe_div(equity, shares),
        fcf_latest=at(fcf), fcf_base=(sum(fcf_ok[:2]) / 2 if len(fcf_ok) >= 2 else at(fcf)),
        dividend_ps=num(info.get("dividendRate")),
        rev_cagr_3y=cagr(at(rev), at(rev, min(3, yrs)), min(3, yrs)),
        rev_cagr_5y=cagr(at(rev), at(rev, yrs), yrs),
        eps_cagr=cagr(at(ni), at(ni, yrs), yrs),
        fcf_cagr=cagr(fcf_ok[0], fcf_ok[-1], len(fcf_ok) - 1) if len(fcf_ok) >= 2 else None,
        gross_margin=safe_div(at(gp), at(rev)), op_margin=safe_div(at(ebit), at(rev)),
        net_margin=safe_div(at(ni), at(rev)), fcf_margin=safe_div(at(fcf), at(rev)),
        roe=num(info.get("returnOnEquity")) or safe_div(at(ni), equity),
        roa=num(info.get("returnOnAssets")) or safe_div(at(ni), at(ta)),
        roic=(at(ebit) * (1 - tax_rate) / ic) if (at(ebit) is not None and ic and ic > 0) else None,
        debt_to_equity=safe_div(debt0, equity),
        current_ratio=num(info.get("currentRatio")) or safe_div(at(ca), at(cl)),
        quick_ratio=num(info.get("quickRatio")) or safe_div(
            (at(ca) - (at(inv) or 0)) if at(ca) is not None else None, at(cl)),
        interest_coverage=safe_div(at(ebit), abs(at(intexp)) if at(intexp) else None),
        net_cash=cash0 - debt0, total_debt=debt0, cash=cash0, tax_rate=tax_rate,
        total_assets=at(ta), total_liabilities=at(tl),
        capex=abs(at(capex)) if at(capex) is not None else None, ocf=at(ocf),
        buyback=at(buyback),
        pe=num(info.get("trailingPE")), forward_pe=num(info.get("forwardPE")),
        pb=num(info.get("priceToBook")), ps=num(info.get("priceToSalesTrailing12Months")),
        peg=num(info.get("trailingPegRatio")),
        ev_ebitda=num(info.get("enterpriseToEbitda")),
        payout=num(info.get("payoutRatio")))


FMP_MODE = {"m": ""}


def fmp_get(name, sym, extra=None):
    key = KEYS["fmp"]
    if not key:
        raise ValueError("no FMP key")
    ps = {"symbol": sym, "apikey": key, **(extra or {})}
    pv = {"apikey": key, **(extra or {})}
    stable = ("https://financialmodelingprep.com/stable/" + name, ps)
    v3 = (f"https://financialmodelingprep.com/api/v3/{name}/{sym}", pv)
    if FMP_MODE["m"] == "v3":
        return get(*v3)
    if FMP_MODE["m"] == "stable":
        return get(*stable)
    try:
        j = get(*stable); FMP_MODE["m"] = "stable"; return j
    except Exception:
        j = get(*v3); FMP_MODE["m"] = "v3"; return j


def fund_from_fmp(sym):
    prof = fmp_get("profile", sym)
    P = prof[0] if isinstance(prof, list) and prof else (prof or {})
    if P.get("isEtf"):
        return block("fmp", is_etf=True, name=P.get("companyName"))
    inc = fmp_get("income-statement", sym, {"period": "annual", "limit": 6})
    bal = fmp_get("balance-sheet-statement", sym, {"period": "annual", "limit": 6})
    cfs = fmp_get("cash-flow-statement", sym, {"period": "annual", "limit": 6})
    rat = fmp_get("ratios-ttm", sym)
    if not inc or not bal or not cfs:
        raise ValueError("FMP returned no statements")
    R = rat[0] if isinstance(rat, list) and rat else (rat or {})
    y0, b0, c0 = inc[0], bal[0], cfs[0]
    nI = len(inc) - 1
    fcf = [x.get("freeCashFlow") if x.get("freeCashFlow") is not None
           else (num(x.get("operatingCashFlow")) or 0) - abs(num(x.get("capitalExpenditure")) or 0)
           for x in cfs]
    shares = num(y0.get("weightedAverageShsOutDil")) or num(y0.get("weightedAverageShsOut"))
    eq = num(b0.get("totalStockholdersEquity"))
    debt = num(b0.get("totalDebt")) or 0
    cash = num(b0.get("cashAndShortTermInvestments")) or 0
    ebit, rev = num(y0.get("operatingIncome")), num(y0.get("revenue"))
    tax = min(max(safe_div(y0.get("incomeTaxExpense"), y0.get("incomeBeforeTax")) or 0.21, 0), 0.45)
    ic = (eq or 0) + debt - cash
    eps = num(y0.get("epsdiluted")) or num(y0.get("eps"))
    return block("fmp", is_etf=False, name=P.get("companyName") or sym, sector=P.get("sector"),
        industry=P.get("industry"), employees=num(P.get("fullTimeEmployees")),
        period=y0.get("calendarYear"), years=len(inc), currency=y0.get("reportedCurrency"),
        mcap=num(P.get("mktCap")), shares=shares,
        annual=[{"year": r.get("calendarYear"), "revenue": num(r.get("revenue")),
                 "net_income": num(r.get("netIncome")),
                 "fcf": next((f for x, f in zip(cfs, fcf) if x.get("calendarYear") == r.get("calendarYear")), None),
                 "gross": safe_div(r.get("grossProfit"), r.get("revenue")),
                 "op": safe_div(r.get("operatingIncome"), r.get("revenue")),
                 "net": safe_div(r.get("netIncome"), r.get("revenue"))} for r in reversed(inc)],
        revenue=rev, net_income=num(y0.get("netIncome")), ebit=ebit,
        eps_ttm=eps, bvps=safe_div(eq, shares), fcf_latest=fcf[0],
        fcf_base=(fcf[0] + fcf[1]) / 2 if len(fcf) >= 2 and None not in fcf[:2] else fcf[0],
        dividend_ps=num(R.get("dividendPerShareTTM")),
        rev_cagr_3y=cagr(rev, inc[min(3, nI)].get("revenue"), min(3, nI)),
        rev_cagr_5y=cagr(rev, inc[nI].get("revenue"), nI),
        eps_cagr=cagr(num(y0.get("netIncome")), num(inc[nI].get("netIncome")), nI),
        fcf_cagr=cagr(fcf[0], fcf[-1], len(fcf) - 1),
        gross_margin=safe_div(y0.get("grossProfit"), rev), op_margin=safe_div(ebit, rev),
        net_margin=safe_div(y0.get("netIncome"), rev), fcf_margin=safe_div(fcf[0], rev),
        roe=num(R.get("returnOnEquityTTM")) or safe_div(y0.get("netIncome"), eq),
        roa=safe_div(y0.get("netIncome"), b0.get("totalAssets")),
        roic=(ebit * (1 - tax) / ic) if (ebit is not None and ic > 0) else None,
        debt_to_equity=safe_div(debt, eq), net_cash=cash - debt, total_debt=debt, cash=cash,
        tax_rate=tax, total_assets=num(b0.get("totalAssets")),
        current_ratio=safe_div(b0.get("totalCurrentAssets"), b0.get("totalCurrentLiabilities")),
        quick_ratio=safe_div((num(b0.get("totalCurrentAssets")) or 0) - (num(b0.get("inventory")) or 0),
                             b0.get("totalCurrentLiabilities")),
        interest_coverage=safe_div(ebit, abs(num(y0.get("interestExpense"))) if y0.get("interestExpense") else None),
        capex=abs(num(c0.get("capitalExpenditure")) or 0), ocf=num(c0.get("operatingCashFlow")),
        buyback=num(c0.get("commonStockRepurchased")),
        pe=num(R.get("peRatioTTM")) or num(R.get("priceEarningsRatioTTM")),
        pb=num(R.get("priceToBookRatioTTM")), ps=num(R.get("priceToSalesRatioTTM")),
        peg=num(R.get("pegRatioTTM")) or num(R.get("priceEarningsToGrowthRatioTTM")))


def fund_from_finnhub(sym):
    """Ratios only — no raw statements, but enough to fill the pillars."""
    if not KEYS["finnhub"]:
        raise ValueError("no Finnhub key")
    j = get("https://finnhub.io/api/v1/stock/metric",
            {"symbol": sym, "metric": "all", "token": KEYS["finnhub"]})
    m = (j or {}).get("metric") or {}
    if not m:
        raise ValueError("Finnhub returned no metrics")
    p = lambda k: (num(m.get(k)) / 100) if num(m.get(k)) is not None else None
    return block("finnhub", is_etf=False, name=sym, partial=True, years=None, annual=[],
        eps_ttm=num(m.get("epsTTM")) or num(m.get("epsBasicExclExtraItemsTTM")),
        bvps=num(m.get("bookValuePerShareQuarterly")),
        rev_cagr_3y=p("revenueGrowth3Y"), rev_cagr_5y=p("revenueGrowth5Y"),
        eps_cagr=p("epsGrowth5Y"), gross_margin=p("grossMarginTTM"),
        op_margin=p("operatingMarginTTM"), net_margin=p("netProfitMarginTTM"),
        roe=p("roeTTM"), roa=p("roaTTM"), roic=p("roiTTM"),
        debt_to_equity=num(m.get("totalDebt/totalEquityQuarterly")),
        current_ratio=num(m.get("currentRatioQuarterly")),
        quick_ratio=num(m.get("quickRatioQuarterly")),
        interest_coverage=num(m.get("netInterestCoverageTTM")),
        pe=num(m.get("peTTM")), pb=num(m.get("pbQuarterly")), ps=num(m.get("psTTM")),
        peg=num(m.get("pegTTM")), ev_ebitda=num(m.get("evEbitdaTTM")),
        dividend_ps=num(m.get("dividendPerShareTTM")),
        note="Ratios only — Finnhub's free tier carries no raw statements, so the "
             "growth chart and cash-flow-based models are unavailable from this source.")


def fund_from_av(sym):
    global av_calls
    if not KEYS["av"]:
        raise ValueError("no Alpha Vantage key")
    if av_calls >= AV_BUDGET:
        raise ValueError("Alpha Vantage call budget reached this run")
    av_calls += 1
    j = get("https://www.alphavantage.co/query",
            {"function": "OVERVIEW", "symbol": sym, "apikey": KEYS["av"]})
    if not j or not j.get("Symbol"):
        raise ValueError("Alpha Vantage returned no overview")
    f = lambda k: num(j.get(k))
    return block("alphavantage", is_etf=(j.get("AssetType") == "ETF"), name=j.get("Name") or sym,
        partial=True, sector=j.get("Sector"), industry=j.get("Industry"), years=None, annual=[],
        mcap=f("MarketCapitalization"), shares=f("SharesOutstanding"),
        revenue=f("RevenueTTM"), eps_ttm=f("EPS"), bvps=f("BookValue"),
        dividend_ps=f("DividendPerShare"), gross_margin=f("GrossProfitTTM") and safe_div(f("GrossProfitTTM"), f("RevenueTTM")),
        op_margin=f("OperatingMarginTTM"), net_margin=f("ProfitMargin"),
        roe=f("ReturnOnEquityTTM"), roa=f("ReturnOnAssetsTTM"),
        rev_cagr_3y=None, eps_cagr=None,
        pe=f("PERatio"), forward_pe=f("ForwardPE"), pb=f("PriceToBookRatio"),
        ps=f("PriceToSalesRatioTTM"), peg=f("PEGRatio"), ev_ebitda=f("EVToEBITDA"),
        note="Summary ratios only — Alpha Vantage's OVERVIEW carries no raw statements, "
             "so cash-flow-based valuation models cannot run from this source.")


def get_fundamentals(sym, prev):
    """Try four sources in order. Only an all-four failure is an error."""
    if is_fresh(prev, days=3) and not prev.get("partial"):
        return prev
    attempts = []
    for name, fn in [("yfinance", fund_from_yf), ("fmp", fund_from_fmp),
                     ("finnhub", fund_from_finnhub), ("alphavantage", fund_from_av)]:
        try:
            out = fn(sym)
            if out and not out.get("error"):
                out["attempts"] = attempts
                out["fallback_used"] = name != "yfinance"
                return out
        except Exception as e:
            attempts.append({"source": name, "error": str(e)[:160]})
    return {"source": "none", "fetched_at": now_iso(),
            "error": "all four fundamental sources failed", "attempts": attempts}


# --------------------------------------------------------------- other blocks

def get_quote(sym):
    if not KEYS["finnhub"]:
        return err("finnhub", "no key")
    try:
        j = get("https://finnhub.io/api/v1/quote", {"symbol": sym, "token": KEYS["finnhub"]})
        if not j.get("c"):
            raise ValueError("no quote returned")
        return block("finnhub", current=num(j["c"]), change=num(j.get("d")),
                     change_pct=num(j.get("dp")), high=num(j.get("h")), low=num(j.get("l")),
                     open=num(j.get("o")), prev_close=num(j.get("pc")), ts=j.get("t"))
    except Exception as e:
        return err("finnhub", e)


def get_profile(sym):
    if not KEYS["finnhub"]:
        return err("finnhub", "no key")
    try:
        j = get("https://finnhub.io/api/v1/stock/profile2", {"symbol": sym, "token": KEYS["finnhub"]})
        if not j or not j.get("name"):
            raise ValueError("no profile")
        return block("finnhub", name=j.get("name"), industry=j.get("finnhubIndustry"),
                     exchange=j.get("exchange"), country=j.get("country"),
                     mcap=(num(j.get("marketCapitalization")) or 0) * 1e6 or None,
                     ipo=j.get("ipo"), web=j.get("weburl"))
    except Exception as e:
        return err("finnhub", e)


def get_analysts(sym):
    if not KEYS["finnhub"]:
        return err("finnhub", "no key")
    try:
        j = get("https://finnhub.io/api/v1/stock/recommendation",
                {"symbol": sym, "token": KEYS["finnhub"]})
        if not isinstance(j, list) or not j:
            raise ValueError("no coverage")
        return block("finnhub", trends=j[:6])
    except Exception as e:
        return err("finnhub", e)


def get_yf_extras(sym):
    if not HAVE_YF:
        return err("yfinance", "unavailable")
    try:
        info = yf.Ticker(sym).get_info() or {}
        keep = {"analyst_target_mean": num(info.get("targetMeanPrice")),
                "analyst_target_low": num(info.get("targetLowPrice")),
                "analyst_target_high": num(info.get("targetHighPrice")),
                "analyst_count": num(info.get("numberOfAnalystOpinions")),
                "recommendation": info.get("recommendationKey"),
                "forward_pe": num(info.get("forwardPE")), "forward_eps": num(info.get("forwardEps")),
                "short_pct_float": num(info.get("shortPercentOfFloat")),
                "short_ratio": num(info.get("shortRatio")),
                "short_asof": info.get("dateShortInterest"),
                "institutions_pct": num(info.get("heldPercentInstitutions")),
                "insiders_pct": num(info.get("heldPercentInsiders")),
                "beta": num(info.get("beta")),
                "next_earnings": info.get("earningsTimestamp") or info.get("earningsTimestampStart")}
        if all(v is None for v in keep.values()):
            raise ValueError("no usable fields")
        return block("yfinance", **keep)
    except Exception as e:
        return err("yfinance", e)


def get_earnings(sym, prev):
    global av_calls
    if is_fresh(prev, days=7):
        return prev
    if not KEYS["av"]:
        return err("alphavantage", "no key")
    if av_calls >= AV_BUDGET:
        return prev if prev and not prev.get("error") else err("alphavantage", "budget reached")
    try:
        av_calls += 1
        j = get("https://www.alphavantage.co/query",
                {"function": "EARNINGS", "symbol": sym, "apikey": KEYS["av"]})
        q = j.get("quarterlyEarnings")
        if not q:
            raise ValueError("no earnings history")
        rows = [{"period": r.get("fiscalDateEnding"), "reported": r.get("reportedDate"),
                 "est": num(r.get("estimatedEPS")), "act": num(r.get("reportedEPS")),
                 "surprise": num(r.get("surprisePercentage"))} for r in q[:8]]
        sc = [r for r in rows if r["surprise"] is not None]
        return block("alphavantage", rows=rows, beats=sum(1 for r in sc if r["surprise"] > 0),
                     scored=len(sc),
                     avg_surprise=(sum(r["surprise"] for r in sc) / len(sc)) if sc else None)
    except Exception as e:
        return prev if prev and not prev.get("error") else err("alphavantage", e)


def get_market_status():
    if not KEYS["polygon"]:
        return err("polygon", "no key")
    try:
        j = get("https://api.polygon.io/v1/marketstatus/now", {"apiKey": KEYS["polygon"]})
        return block("polygon", market=j.get("market"), server_time=j.get("serverTime"))
    except Exception as e:
        return err("polygon", e)


def get_macro(prev):
    global av_calls
    if is_fresh(prev, hours=20):
        return prev
    if not KEYS["av"]:
        return err("alphavantage", "no key")
    out = {}
    try:
        for name, params in [("cpi", {"function": "CPI", "interval": "monthly"}),
                             ("fed", {"function": "FEDERAL_FUNDS_RATE", "interval": "monthly"}),
                             ("t10", {"function": "TREASURY_YIELD", "interval": "monthly",
                                      "maturity": "10year"})]:
            if av_calls >= AV_BUDGET:
                raise ValueError("budget reached")
            av_calls += 1
            j = get("https://www.alphavantage.co/query", {**params, "apikey": KEYS["av"]})
            d = j.get("data")
            if not d:
                raise ValueError(f"no data for {name}")
            e = {"date": d[0]["date"], "value": num(d[0]["value"])}
            if name == "cpi" and len(d) >= 13 and num(d[12]["value"]):
                e["yoy"] = round((e["value"] / num(d[12]["value"]) - 1) * 100, 2)
            out[name] = e
        return block("alphavantage", **out)
    except Exception as e:
        return prev if prev and not prev.get("error") else err("alphavantage", e)


# ------------------------------------------------------------------ assembly

def build(sym, prev):
    prev = prev or {}
    t = {"symbol": sym, "generated_at": now_iso()}
    t["quote"] = get_quote(sym)
    t["profile"] = get_profile(sym)

    tw, tw_e = candles_twelve(sym)
    pg, pg_e = candles_polygon(sym)
    yfb, yf_e = (None, "not attempted")
    if not tw:
        yfb, yf_e = candles_yf(sym)
    bars = tw or yfb or pg
    primary = "twelvedata" if tw else ("yfinance" if yfb else ("polygon" if pg else None))
    xc = cross_verify(tw or yfb, pg)
    if bars:
        t["candles"] = block(primary, bars=bars, n=len(bars), latest=bars[-1]["date"],
                             cross_check=xc,
                             errors={"twelvedata": tw_e, "polygon": pg_e, "yfinance": yf_e},
                             confidence=("HIGH" if xc and xc["status"] == "AGREE"
                                         else "LOW" if xc and xc["status"] == "DISAGREE"
                                         else "MEDIUM"))
    else:
        t["candles"] = {"source": "none", "fetched_at": now_iso(), "confidence": "NONE",
                        "error": f"twelvedata: {tw_e}; yfinance: {yf_e}; polygon: {pg_e}"}

    t["fundamentals"] = get_fundamentals(sym, prev.get("fundamentals"))
    t["earnings"] = get_earnings(sym, prev.get("earnings"))
    t["analysts"] = get_analysts(sym)
    t["extras"] = get_yf_extras(sym)
    return t


def main():
    wl = json.loads((ROOT / "watchlist.json").read_text())
    tickers = [s.strip().upper() for s in wl.get("tickers", []) if s.strip()]
    extra = os.environ.get("EXTRA_TICKERS", "")
    for s in extra.replace(";", ",").split(","):
        s = s.strip().upper()
        if s and s not in tickers:
            tickers.append(s)
    if not tickers:
        print("watchlist is empty"); return 1

    idx_path = OUT / "index.json"
    prev_index = {}
    if idx_path.exists():
        try:
            prev_index = json.loads(idx_path.read_text())
        except Exception:
            pass

    done, summary = [], []
    for sym in tickers:
        p = OUT / f"{sym}.json"
        prev = {}
        if p.exists():
            try:
                prev = json.loads(p.read_text())
            except Exception:
                pass
        print(f"-> {sym}", flush=True)
        d = build(sym, prev)
        p.write_text(json.dumps(d, indent=1, allow_nan=False))
        f = d["fundamentals"]
        summary.append({"symbol": sym,
                        "name": f.get("name") or sym,
                        "fundamentals_source": f.get("source"),
                        "fundamentals_ok": not f.get("error"),
                        "candles_ok": not d["candles"].get("error"),
                        "confidence": d["candles"].get("confidence"),
                        "price": (d["quote"] or {}).get("current")})
        print(f"   candles={d['candles'].get('source')} "
              f"fundamentals={f.get('source')}{' (FALLBACK)' if f.get('fallback_used') else ''}",
              flush=True)
        done.append(sym)
        time.sleep(9)  # Twelve Data 8/min, Polygon 5/min

    index = {"generated_at": now_iso(), "tickers": done, "summary": summary,
             "market_status": get_market_status(),
             "macro": get_macro(prev_index.get("macro")),
             "sources_configured": {k: bool(v) for k, v in KEYS.items()},
             "yfinance_available": HAVE_YF,
             "av_calls_this_run": av_calls}
    idx_path.write_text(json.dumps(index, indent=1, allow_nan=False))

    okf = sum(1 for s in summary if s["fundamentals_ok"])
    print(f"\nDone. {len(done)} tickers · fundamentals resolved for {okf}/{len(done)} · "
          f"Alpha Vantage calls {av_calls}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
