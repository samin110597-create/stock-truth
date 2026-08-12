#!/usr/bin/env python3
"""Immutable-ish GitHub forward ledger for Stock Truth ML snapshots.

Each scheduled run records the model output *before* the future outcome exists.
Later runs resolve entries after the required number of trading sessions using
committed daily bars. Git history makes later edits auditable.
"""
from __future__ import annotations
import argparse, json
from datetime import datetime, timezone
from pathlib import Path


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')


def load_json(p: Path, default):
    try: return json.loads(p.read_text(encoding='utf-8'))
    except Exception: return default


def latest_bar(data):
    bars=((data.get('candles') or {}).get('bars') or [])
    return bars[-1] if bars else None


def resolve(entries, data_dir: Path):
    cache={}
    for e in entries:
        if e.get('outcome_status')=='RESOLVED': continue
        sym=e.get('symbol'); horizon=int(e.get('horizon_sessions') or 0)
        if not sym or horizon<=0: continue
        if sym not in cache:
            data=load_json(data_dir/f'{sym}.json',{})
            cache[sym]=((data.get('candles') or {}).get('bars') or [])
        bars=cache[sym]; dates=[str(b.get('date',''))[:10] for b in bars]
        try: i=dates.index(str(e.get('signal_date',''))[:10])
        except ValueError: continue
        j=i+horizon
        if j>=len(bars): continue
        start=float(e.get('reference_price') or 0); end=float(bars[j].get('close') or 0)
        if start<=0 or end<=0: continue
        actual=end/start-1; p=e.get('prob_up'); pred_up=(float(p)>=.5) if p is not None else None
        e.update({'outcome_status':'RESOLVED','resolved_at':now_iso(),'outcome_date':dates[j],'actual_price':end,'actual_return':actual,'actual_up':actual>0,'direction_correct':(actual>0)==pred_up if pred_up is not None else None})
    return entries


def record(entries, data_dir: Path, ml_dir: Path):
    keys={e.get('key') for e in entries}
    for p in sorted(ml_dir.glob('*.json')):
        if p.name in ('index.json','forward-ledger.json','forward-summary.json'): continue
        m=load_json(p,{}); sym=str(m.get('symbol') or p.stem).upper(); data=load_json(data_dir/f'{sym}.json',{}); lb=latest_bar(data)
        if not lb: continue
        signal_date=str(lb.get('date',''))[:10]
        try: ref=float(lb.get('close'))
        except Exception: continue
        for name,h in (m.get('horizons') or {}).items():
            hs=int(h.get('horizon_sessions') or 0)
            if hs<=0 or h.get('prob_up') is None: continue
            key=f"{sym}|{m.get('model_version')}|{hs}|{signal_date}"
            if key in keys: continue
            entries.append({'key':key,'symbol':sym,'model_version':m.get('model_version'),'signal_date':signal_date,'created_at':now_iso(),'horizon_sessions':hs,'horizon_key':name,'reference_price':ref,'status_at_signal':h.get('status'),'edge_verified_at_signal':bool(h.get('edge_verified')),'prediction_at_signal':h.get('prediction'),'prob_up':h.get('prob_up'),'expected_return_median':h.get('expected_return_median'),'expected_return_p20':h.get('expected_return_p20'),'expected_return_p80':h.get('expected_return_p80'),'outcome_status':'PENDING'})
            keys.add(key)
    return entries


def summary(entries):
    groups={}
    for e in entries:
        if e.get('outcome_status')!='RESOLVED': continue
        key=(int(e.get('horizon_sessions') or 0), 'VERIFIED' if e.get('edge_verified_at_signal') else 'UNVERIFIED')
        groups.setdefault(key,[]).append(e)
    out=[]
    for (h,status),rows in sorted(groups.items()):
        corr=[r for r in rows if r.get('direction_correct') is not None]
        acc=sum(bool(r['direction_correct']) for r in corr)/len(corr) if corr else None
        rets=sorted(float(r['actual_return']) for r in rows if r.get('actual_return') is not None)
        out.append({'horizon_sessions':h,'signal_status':status,'resolved_n':len(rows),'direction_accuracy':acc,'median_actual_return':rets[len(rets)//2] if rets else None})
    return {'generated_at':now_iso(),'note':'Forward ledger statistics only. Daily signals overlap; use independent-sample analysis before treating these as evidence of edge.','groups':out}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--data-dir',default='docs/data'); ap.add_argument('--ml-dir',default='docs/data/ml'); args=ap.parse_args(); data=Path(args.data_dir); ml=Path(args.ml_dir); ledger=ml/'forward-ledger.json'; entries=load_json(ledger,{'entries':[]}).get('entries',[]); entries=resolve(entries,data); entries=record(entries,data,ml); ledger.write_text(json.dumps({'schema_version':1,'generated_at':now_iso(),'entries':entries},indent=2,allow_nan=False)+'\n',encoding='utf-8'); (ml/'forward-summary.json').write_text(json.dumps(summary(entries),indent=2,allow_nan=False)+'\n',encoding='utf-8'); print(f'forward ledger: {len(entries)} entries')

if __name__=='__main__': main()
