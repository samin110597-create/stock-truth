#!/usr/bin/env python3
"""Stock Truth verified-research ML v2.

Design goals:
- show research forecasts even when unverified, but label them UNVERIFIED;
- only call an edge VERIFIED after purged/embargoed walk-forward tests clear
  discrimination, calibration, independent-sample, significance, and fold-stability gates;
- use max available adjusted history for long horizons when yfinance is available;
- preserve a dedicated 252-session (about one trading year) model;
- never use a forming bar or future feature in training.
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
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, balanced_accuracy_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit

MODEL_VERSION = "stock-truth-ml-v2-verified-research"
DEFAULT_HORIZONS = (5, 21, 63, 252)
BASE_FEATURE_COLUMNS = [
    "ret_1","ret_5","ret_21","ret_63","ret_126","ret_252",
    "gap_1","range_pct","body_pct",
    "sma20_rel","sma50_rel","sma200_rel","trend_spread",
    "rsi14","atr14_pct","vol20","vol60","vol_ratio",
    "volume_ratio20","volume_z60","drawdown63","range_pos252",
]
BENCHMARK_FEATURE_COLUMNS = [
    "bench_ret_5","bench_ret_21","bench_ret_63","bench_vol20",
    "relative_ret_5","relative_ret_21","relative_ret_63","beta60","corr60",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_float(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def bars_from_snapshot_obj(obj: Dict[str, Any]) -> pd.DataFrame:
    bars = ((obj.get("candles") or {}).get("bars") or [])
    rows = []
    for b in bars if isinstance(bars, list) else []:
        if not isinstance(b, dict):
            continue
        rows.append({
            "date": b.get("date"), "open": safe_float(b.get("open")),
            "high": safe_float(b.get("high")), "low": safe_float(b.get("low")),
            "close": safe_float(b.get("close")), "volume": safe_float(b.get("volume")),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("candles.bars is missing or empty")
    df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.normalize()
    df = df.dropna(subset=["date","open","high","low","close"])
    return df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def public_history(symbol: str, period: str = "max") -> pd.DataFrame | None:
    if not HAVE_YF:
        return None
    try:
        h = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
        if h is None or h.empty:
            return None
        df = pd.DataFrame({
            "date": pd.to_datetime(h.index, errors="coerce", utc=True).normalize(),
            "open": pd.to_numeric(h["Open"], errors="coerce").to_numpy(),
            "high": pd.to_numeric(h["High"], errors="coerce").to_numpy(),
            "low": pd.to_numeric(h["Low"], errors="coerce").to_numpy(),
            "close": pd.to_numeric(h["Close"], errors="coerce").to_numpy(),
            "volume": pd.to_numeric(h.get("Volume"), errors="coerce").to_numpy(),
        })
        df = df.dropna(subset=["date","open","high","low","close"])
        df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
        return df if len(df) >= 500 else None
    except Exception as exc:
        print(f"warning: {symbol} max-history fetch failed: {exc}")
        return None


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    d = series.diff(); gain = d.clip(lower=0); loss = -d.clip(upper=0)
    ag = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    al = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - 100/(1+rs)).fillna(50.0)


def atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([(df["high"]-df["low"]),(df["high"]-pc).abs(),(df["low"]-pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    return atr / df["close"]


def build_features(df: pd.DataFrame, benchmark_df: pd.DataFrame | None) -> Tuple[pd.DataFrame,List[str],str|None]:
    x = pd.DataFrame(index=df.index); c=df["close"]; o=df["open"]; h=df["high"]; l=df["low"]; v=df["volume"]
    for n in (1,5,21,63,126,252): x[f"ret_{n}"] = c.pct_change(n)
    x["gap_1"] = o/c.shift(1)-1; x["range_pct"]=(h-l)/c; x["body_pct"]=(c-o)/o
    s20=c.rolling(20).mean(); s50=c.rolling(50).mean(); s200=c.rolling(200).mean()
    x["sma20_rel"]=c/s20-1; x["sma50_rel"]=c/s50-1; x["sma200_rel"]=c/s200-1; x["trend_spread"]=s20/s200-1
    x["rsi14"] = rsi(c)/100.0; x["atr14_pct"] = atr_pct(df)
    lr=np.log(c/c.shift(1)); x["vol20"]=lr.rolling(20).std(ddof=1)*math.sqrt(252); x["vol60"]=lr.rolling(60).std(ddof=1)*math.sqrt(252)
    x["vol_ratio"] = x["vol20"] / x["vol60"].replace(0,np.nan)
    if v.notna().sum() >= 80:
        vm20=v.rolling(20).mean(); vs60=v.rolling(60).std(ddof=1).replace(0,np.nan); vm60=v.rolling(60).mean()
        x["volume_ratio20"]=v/vm20; x["volume_z60"]=(v-vm60)/vs60
    else:
        x["volume_ratio20"]=np.nan; x["volume_z60"]=np.nan
    x["drawdown63"] = c/c.rolling(63).max()-1
    lo252=l.rolling(252).min(); hi252=h.rolling(252).max(); x["range_pos252"]=(c-lo252)/(hi252-lo252).replace(0,np.nan)
    cols=list(BASE_FEATURE_COLUMNS); bench_used=None
    if benchmark_df is not None and not benchmark_df.empty:
        b=benchmark_df[["date","close"]].copy().rename(columns={"close":"bench_close"})
        aligned=df[["date"]].merge(b,on="date",how="left"); bc=aligned["bench_close"].ffill(limit=3)
        br1=bc.pct_change(1)
        for n in (5,21,63): x[f"bench_ret_{n}"]=bc.pct_change(n)
        x["bench_vol20"]=np.log(bc/bc.shift(1)).rolling(20).std(ddof=1)*math.sqrt(252)
        for n in (5,21,63): x[f"relative_ret_{n}"]=x[f"ret_{n}"]-x[f"bench_ret_{n}"]
        sr1=c.pct_change(1); cov=sr1.rolling(60).cov(br1); bvar=br1.rolling(60).var(ddof=1).replace(0,np.nan)
        x["beta60"]=cov/bvar; x["corr60"]=sr1.rolling(60).corr(br1)
        cols += BENCHMARK_FEATURE_COLUMNS; bench_used="SPY"
    return x.replace([np.inf,-np.inf],np.nan), cols, bench_used


def classifier_pair() -> Tuple[Pipeline,Pipeline]:
    tree=Pipeline([("imputer",SimpleImputer(strategy="median")),("model",HistGradientBoostingClassifier(loss="log_loss",learning_rate=.035,max_iter=220,max_leaf_nodes=15,min_samples_leaf=24,l2_regularization=1.8,early_stopping=False,random_state=42))])
    linear=Pipeline([("imputer",SimpleImputer(strategy="median")),("scale",StandardScaler()),("model",LogisticRegression(C=.35,class_weight="balanced",max_iter=3000,solver="lbfgs",random_state=42))])
    return tree,linear


def quantile_regressor(q: float) -> Pipeline:
    return Pipeline([("imputer",SimpleImputer(strategy="median")),("model",HistGradientBoostingRegressor(loss="quantile",quantile=q,learning_rate=.035,max_iter=220,max_leaf_nodes=15,min_samples_leaf=24,l2_regularization=1.8,early_stopping=False,random_state=42))])


def ece_score(y: np.ndarray,p: np.ndarray,bins: int=8) -> float:
    e=0.0
    for i in range(bins):
        lo=i/bins; hi=(i+1)/bins; mask=(p>=lo)&((p<=hi) if i==bins-1 else (p<hi))
        if mask.any(): e += mask.mean()*abs(float(p[mask].mean())-float(y[mask].mean()))
    return float(e)


def binomial_tail(k:int,n:int,p0:float) -> float:
    if n<=0: return 1.0
    p0=min(max(float(p0),1e-9),1-1e-9); logs=[]
    for i in range(k,n+1):
        logs.append(math.lgamma(n+1)-math.lgamma(i+1)-math.lgamma(n-i+1)+i*math.log(p0)+(n-i)*math.log(1-p0))
    if not logs: return 1.0
    m=max(logs); return min(1.0, math.exp(m)*sum(math.exp(z-m) for z in logs))


def wilson(k:int,n:int,z:float=1.96) -> Dict[str,float|None]:
    if n<=0: return {"low":None,"high":None}
    p=k/n; d=1+z*z/n; c=(p+z*z/(2*n))/d; m=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/d
    return {"low":c-m,"high":c+m}


def metrics(y: np.ndarray,p: np.ndarray,baseline_prob: np.ndarray) -> Dict[str,Any]:
    pred=(p>=.5).astype(int); base=(baseline_prob>=.5).astype(int)
    acc=float(accuracy_score(y,pred)); bal=float(balanced_accuracy_score(y,pred)); brier=float(brier_score_loss(y,p)); base_brier=float(brier_score_loss(y,baseline_prob)); base_acc=float(accuracy_score(y,base)); auc=float(roc_auc_score(y,p)) if len(np.unique(y))==2 else None
    return {"accuracy":acc,"balanced_accuracy":bal,"baseline_accuracy":base_acc,"edge":acc-base_acc,"brier":brier,"baseline_brier":base_brier,"brier_skill":((base_brier-brier)/base_brier if base_brier>0 else 0.0),"roc_auc":auc,"ece":ece_score(y,p),"positive_rate":float(y.mean())}


def verification_thresholds(h:int) -> Dict[str,float|int]:
    if h>=200:
        return {"min_oos":180,"min_independent":12,"min_balanced":.54,"min_auc":.56,"min_brier_skill":.015,"max_ece":.12,"max_p_value":.12,"min_fold_consistency":.60}
    return {"min_oos":120,"min_independent":30,"min_balanced":.535,"min_auc":.545,"min_brier_skill":.015,"max_ece":.10,"max_p_value":.08,"min_fold_consistency":.60}


def fit_horizon(features: pd.DataFrame, close: pd.Series, horizon: int, cols: List[str]) -> Dict[str,Any]:
    fwd=close.shift(-horizon)/close-1
    work=features.copy(); work["target_return"]=fwd; work["bar_index"]=np.arange(len(work)); work=work.dropna(subset=["target_return"]); work=work.loc[work[cols].notna().sum(axis=1) >= len(cols)-3]
    min_rows=650 if horizon>=200 else 420
    if len(work)<min_rows: return {"horizon_sessions":horizon,"status":"INSUFFICIENT_DATA","reason":f"{len(work)} labelled rows; {min_rows} required"}
    X=work[cols].astype(float); yret=work["target_return"].astype(float); y=(yret>0).astype(int); bar_index=work["bar_index"].astype(int).to_numpy()
    if y.nunique()<2: return {"horizon_sessions":horizon,"status":"INSUFFICIENT_CLASS_VARIATION"}
    splitter=TimeSeriesSplit(n_splits=6,gap=horizon); oos_raw=np.full(len(work),np.nan); oos_cal=np.full(len(work),np.nan); oos_base=np.full(len(work),np.nan); past_raw=[]; past_y=[]; fold_metrics=[]
    for fold,(tr,te) in enumerate(splitter.split(X)):
        if y.iloc[tr].nunique()<2: continue
        tree,linear=classifier_pair(); tree.fit(X.iloc[tr],y.iloc[tr]); linear.fit(X.iloc[tr],y.iloc[tr]); raw=(tree.predict_proba(X.iloc[te])[:,1]+linear.predict_proba(X.iloc[te])[:,1])/2; base=float(y.iloc[tr].mean()); cal=None
        if len(past_raw)>=80 and len(set(past_y))==2: cal=LogisticRegression(C=1.0,max_iter=1500,solver="lbfgs",random_state=42).fit(np.asarray(past_raw).reshape(-1,1),np.asarray(past_y))
        pp=cal.predict_proba(raw.reshape(-1,1))[:,1] if cal is not None else raw; oos_raw[te]=raw; oos_cal[te]=pp; oos_base[te]=base; past_raw.extend(raw.tolist()); past_y.extend(y.iloc[te].tolist()); fold_metrics.append(metrics(y.iloc[te].to_numpy(),pp,np.full(len(te),base)))
    mask=np.isfinite(oos_cal)
    if int(mask.sum())<100: return {"horizon_sessions":horizon,"status":"INSUFFICIENT_OOS_DATA","reason":f"{int(mask.sum())} OOS predictions"}
    pos=np.where(mask)[0]; independent_pos=[]; last_bar=-10**12
    for p in pos:
        bi=int(bar_index[p])
        if bi>=last_bar+horizon: independent_pos.append(p); last_bar=bi
    ip=np.asarray(independent_pos,dtype=int); yi=y.to_numpy()[ip]; pi=oos_cal[ip]; bi_prob=oos_base[ip]; met=metrics(yi,pi,bi_prob); wins=int(((pi>=.5).astype(int)==yi).sum()); base_majority=float(np.mean(np.maximum(bi_prob,1-bi_prob))); pval=binomial_tail(wins,len(ip),base_majority); ci=wilson(wins,len(ip)); good=sum(1 for m in fold_metrics if (m.get("brier_skill") or -9)>0 and (m.get("balanced_accuracy") or 0)>.5); consistency=good/len(fold_metrics) if fold_metrics else 0.0
    th=verification_thresholds(horizon); gates={"oos_rows":int(mask.sum())>=th["min_oos"],"independent_samples":len(ip)>=th["min_independent"],"balanced_accuracy":met["balanced_accuracy"]>=th["min_balanced"],"auc":met["roc_auc"] is not None and met["roc_auc"]>=th["min_auc"],"brier_skill":met["brier_skill"]>=th["min_brier_skill"],"calibration_error":met["ece"]<=th["max_ece"],"significance":pval<=th["max_p_value"],"fold_stability":consistency>=th["min_fold_consistency"]}; verified=all(gates.values())
    calibrator=None
    if int(mask.sum())>=100 and len(np.unique(y.to_numpy()[mask]))==2: calibrator=LogisticRegression(C=1.0,max_iter=1500,solver="lbfgs",random_state=42).fit(oos_raw[mask].reshape(-1,1),y.to_numpy()[mask])
    tree,linear=classifier_pair(); tree.fit(X,y); linear.fit(X,y); Xc=features[cols].iloc[[-1]].astype(float); raw_now=float((tree.predict_proba(Xc)[0,1]+linear.predict_proba(Xc)[0,1])/2); p_up=float(calibrator.predict_proba(np.array([[raw_now]]))[0,1]) if calibrator is not None else raw_now
    qs={q:quantile_regressor(q).fit(X,yret) for q in (.20,.50,.80)}; qv={q:float(qs[q].predict(Xc)[0]) for q in qs}; bias="BULLISH" if p_up>=.60 else "CONSTRUCTIVE" if p_up>=.53 else "BEARISH" if p_up<=.40 else "CAUTIOUS" if p_up<=.47 else "BALANCED"
    return {"horizon_sessions":horizon,"status":"VERIFIED" if verified else "UNVERIFIED","edge_verified":verified,"prediction":bias if verified else f"UNVERIFIED {bias}","prob_up":p_up,"probability_label":"calibrated probability estimate; historical edge verified by current gates" if verified else "research probability estimate; historical edge NOT verified","expected_return_median":qv[.50],"expected_return_p20":qv[.20],"expected_return_p80":qv[.80],"metrics":met,"oos_rows":int(mask.sum()),"independent_samples":int(len(ip)),"walk_forward_splits":6,"purged_gap_sessions":horizon,"sequential_calibration":True,"fold_consistency":consistency,"p_value_vs_majority_baseline":pval,"accuracy_ci95":ci,"verification_gates":gates,"verification_thresholds":th,"reason":"cleared all predefined verification gates" if verified else "forecast remains visible for research, but at least one predefined verification gate failed"}


def model_for_snapshot(path: Path, benchmark_df: pd.DataFrame|None) -> Dict[str,Any]:
    obj=json.loads(path.read_text(encoding="utf-8")); symbol=str(obj.get("symbol") or path.stem).upper(); snap=bars_from_snapshot_obj(obj); full=public_history(symbol,"max"); df=full if full is not None and len(full)>len(snap) else snap; source="yfinance max adjusted history" if full is not None and len(full)>len(snap) else "committed snapshot history"; out={"schema_version":2,"model_version":MODEL_VERSION,"library":"scikit-learn","library_version":sklearn.__version__,"symbol":symbol,"generated_at":utc_now(),"source_snapshot_generated_at":obj.get("generated_at"),"history_source":source,"bars_used":int(len(df)),"closed_bars_only":True,"warnings":["UNVERIFIED forecasts are research estimates, not validated edges.","VERIFIED means the current historical tests cleared predefined gates; it is not a guarantee of future performance.","The model cannot know future news, earnings surprises, policy shocks, or hidden order flow."]}
    try:
        features,cols,bench=build_features(df,None if symbol=="SPY" else benchmark_df); out["features"]=cols; out["benchmark"]=bench; out["last_signal_date"]=df["date"].iloc[-1].date().isoformat(); out["horizons"]={f"{h}d":fit_horizon(features,df["close"],h,cols) for h in DEFAULT_HORIZONS}; statuses=[z.get("status") for z in out["horizons"].values()]; out["status"]="VERIFIED" if any(s=="VERIFIED" for s in statuses) else "UNVERIFIED" if any(s=="UNVERIFIED" for s in statuses) else "INSUFFICIENT_DATA"
    except Exception as exc: out["status"]="ERROR"; out["error"]=str(exc); out["horizons"]={}
    return out


def eligible_snapshot(path:Path)->bool:
    if path.parent.name=="ml" or path.name=="index.json": return False
    try: o=json.loads(path.read_text(encoding="utf-8")); return isinstance((o.get("candles") or {}).get("bars"),list)
    except Exception: return False


def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("--data-dir",default="docs/data"); ap.add_argument("--output-dir",default="docs/data/ml"); ap.add_argument("--symbol",action="append"); args=ap.parse_args(); data=Path(args.data_dir); outdir=Path(args.output_dir); outdir.mkdir(parents=True,exist_ok=True); wanted={s.upper() for s in (args.symbol or [])}; snaps=[p for p in sorted(data.glob("*.json")) if eligible_snapshot(p)]; snaps=[p for p in snaps if not wanted or p.stem.upper() in wanted]; spy=public_history("SPY","max"); results=[]
    for p in snaps:
        m=model_for_snapshot(p,spy); dest=outdir/f"{m['symbol']}.json"; dest.write_text(json.dumps(m,indent=2,allow_nan=False)+"\n",encoding="utf-8"); results.append({"symbol":m["symbol"],"status":m.get("status"),"file":dest.name}); print(f"{m['symbol']}: {m.get('status')} · bars {m.get('bars_used')}")
    idx={"schema_version":2,"model_version":MODEL_VERSION,"generated_at":utc_now(),"library":"scikit-learn","tickers":results}; (outdir/"index.json").write_text(json.dumps(idx,indent=2)+"\n",encoding="utf-8"); return 0

if __name__=="__main__": raise SystemExit(main())
