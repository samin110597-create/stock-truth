<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Truth — pipeline dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Archivo:wght@400;500;600;700&display=swap');
:root{
  --bg:#121417; --panel:#1A1D22; --edge:#2A2E36; --ink:#E7E4DC;
  --dim:#8B8F98; --faint:#5C606A; --live:#E8A33D; --up:#4CAF7D;
  --down:#E05252; --blue:#7A9BC4;
  --mono:'IBM Plex Mono',ui-monospace,Menlo,monospace;
  --sans:'Archivo',system-ui,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  padding:20px clamp(12px,4vw,40px) 60px}
main{max-width:1100px;margin:0 auto;display:grid;gap:14px}
h1{font-size:20px;margin:0}
.sub{font-family:var(--mono);font-size:11px;color:var(--faint)}
.panel{background:var(--panel);border:1px solid var(--edge);border-radius:6px;padding:16px}
.panel h2{margin:0 0 12px;font-size:12px;font-weight:600;letter-spacing:.14em;
  color:var(--dim);text-transform:uppercase;display:flex;justify-content:space-between;gap:8px;align-items:center}
.tag{font-family:var(--mono);font-size:10px;letter-spacing:.08em;color:var(--live);
  border:1px solid rgba(232,163,61,.27);padding:2px 6px;border-radius:3px;
  background:rgba(232,163,61,.07);white-space:nowrap}
.tag.bad{color:var(--down);border-color:rgba(224,82,82,.27);background:rgba(224,82,82,.07)}
.header-box{border:1px solid rgba(232,163,61,.4);background:rgba(232,163,61,.05);
  border-radius:6px;padding:14px;font-family:var(--mono);font-size:12px;line-height:1.8}
.header-box .hd{color:var(--live);font-size:10px;letter-spacing:.18em;margin-bottom:6px}
.err{color:var(--down)}
.grid{display:grid;gap:14px}
.g2{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.stats{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(130px,1fr))}
.stat .l{font-size:11px;color:var(--faint);margin-bottom:3px}
.stat .v{font-family:var(--mono);font-size:15px;font-weight:500}
.stat .s{font-family:var(--mono);font-size:10px;color:var(--faint);margin-top:2px}
.price{font-family:var(--mono);font-size:34px;font-weight:600}
.up{color:var(--up)}.down{color:var(--down)}.blue{color:var(--blue)}.amber{color:var(--live)}
.tabs{display:flex;gap:8px;flex-wrap:wrap}
.tabs button{font-family:var(--mono);font-size:13px;padding:7px 14px;border-radius:4px;
  border:1px solid var(--edge);background:var(--panel);color:var(--dim);cursor:pointer}
.tabs button.on{border-color:var(--live);color:var(--live);background:rgba(232,163,61,.07)}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
th{font-family:var(--sans);font-size:10px;color:var(--faint);text-align:left;
  text-transform:uppercase;letter-spacing:.1em;padding:5px 8px 5px 0}
td{padding:6px 8px 6px 0;border-top:1px solid var(--edge)}
input[type=text],input[type=password]{background:var(--bg);border:1px solid var(--edge);
  border-radius:4px;color:var(--ink);font-family:var(--mono);font-size:13px;padding:8px 10px}
input:focus{outline:none;border-color:var(--live)}
input[type=range]{accent-color:var(--live);width:100%}
.slider .row{display:flex;justify-content:space-between;font-size:11px}
.slider .row span:first-child{color:var(--dim)}
.slider .row span:last-child{font-family:var(--mono);color:var(--live)}
.note{font-size:11px;color:var(--faint);margin:10px 0 0}
.mono{font-family:var(--mono)}
.pulse{animation:pulse 1.6s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){.pulse{animation:none}}
a{color:var(--blue)}
</style>
</head>
<body>
<main>
  <header style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap">
    <h1>Stock Truth — Pipeline</h1>
    <span class="sub">cached by GitHub Actions · live quote optional · nothing simulated</span>
  </header>

  <div id="app"><div class="panel sub">Loading data…</div></div>
</main>

<script>
"use strict";
/* Reads ./data/index.json + ./data/{SYM}.json produced by the Actions
   pipeline. Every block carries its own source + fetched_at; errors are
   shown, never papered over. The optional Finnhub key (for a live quote
   on top of the cached snapshot) is stored in this browser's
   localStorage only — fine here because this page runs on YOUR site. */

const $ = s => document.querySelector(s);
const fmt = (v,dp=2)=> (v==null||Number.isNaN(+v)||!isFinite(+v)) ? "—"
  : (+v).toLocaleString("en-US",{minimumFractionDigits:dp,maximumFractionDigits:dp});
const big = v => v==null?"—":Math.abs(v)>=1e12?fmt(v/1e12)+"T":Math.abs(v)>=1e9?fmt(v/1e9)+"B"
  :Math.abs(v)>=1e6?fmt(v/1e6,1)+"M":fmt(v,0);
const pc = (v,dp=1)=> v==null||!isFinite(v)?"—":fmt(v*100,dp)+"%";
const ago = iso => { if(!iso) return "unknown";
  const m=(Date.now()-Date.parse(iso))/60000;
  return m<2?"just now":m<60?Math.round(m)+" min ago":m<1440?fmt(m/60,1)+" h ago":fmt(m/1440,1)+" d ago"; };
const esc = s => String(s??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tag = (txt,ok=true)=>`<span class="tag${ok?"":" bad"}">${esc(txt)}</span>`;
const stat=(l,v,cls="",s="")=>`<div class="stat"><div class="l">${esc(l)}</div>
  <div class="v ${cls}">${v}</div>${s?`<div class="s">${s}</div>`:""}</div>`;

let INDEX=null, CUR=null, chart=null, pollTimer=null;
const A={growth:8,discount:10,terminal:2.5,mos:25};

async function boot(){
  try{
    INDEX = await (await fetch("./data/index.json",{cache:"no-store"})).json();
  }catch(e){
    $("#app").innerHTML = `<div class="panel">
      <h2>No data yet ${tag("SETUP",false)}</h2>
      <p class="note">./data/index.json isn't there. Run the workflow once
      (Actions → Fetch market data → Run workflow), wait for the commit,
      and reload. If Pages isn't enabled yet: Settings → Pages → deploy
      from branch, folder <span class="mono">/docs</span>.</p></div>`;
    return;
  }
  await pick(INDEX.tickers[0]);
}

async function pick(sym){
  try{ CUR = await (await fetch(`./data/${sym}.json`,{cache:"no-store"})).json(); }
  catch(e){ CUR = {symbol:sym, load_error:String(e)}; }
  render();
}

function render(){
  const ix=INDEX, t=CUR, tech=t.technicals||{}, f=t.fundamentals||{},
    q=t.quote||{}, eh=t.earnings_history||{}, ye=t.yahoo_extras||{},
    ms=ix.market_status||{}, mac=ix.macro||{};
  const price = q.error? tech.price_used : q.current;
  const conf = tech.data_confidence||"NONE";
  const confCls = conf==="HIGH"?"up":conf==="LOW"?"down":"amber";
  const chg = (!q.error && q.change!=null)? q.change : null;

  $("#app").innerHTML = `
  <div class="grid">
    <div class="tabs">${ix.tickers.map(s=>
      `<button class="${s===t.symbol?"on":""}" onclick="pick('${esc(s)}')">${esc(s)}</button>`).join("")}
    </div>

    <div class="header-box">
      <div class="hd">DATA HEADER</div>
      <div>SNAPSHOT AGE   : ${ago(t.generated_at)} (pipeline run ${esc(t.generated_at||"—")})</div>
      <div>MARKET STATUS  : ${esc(ms.market||"unknown")} ${ms.error?`<span class="err">(${esc(ms.error)})</span>`:`· polygon exchange status`}</div>
      <div>PRICE USED     : ${price!=null?`$${fmt(price)}`:"unavailable"} — ${q.error?`last close from candles (quote error: ${esc(q.error)})`:`Finnhub quote at pipeline run`} <span id="liveNote"></span></div>
      <div>CANDLE CONFIDENCE : <span class="${confCls}">${conf}</span>
        ${tech.cross_verification? ` — ${esc(tech.cross_verification.status)}, max deviation ${fmt(tech.cross_verification.max_deviation_pct,3)}% over ${tech.cross_verification.checked} shared closes (Twelve Data vs Polygon)` : " — single source, no cross-check possible"}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:4px">SOURCES :
        ${tag(`QUOTE ${q.error?"FAIL":"OK"}`,!q.error)}
        ${tag(`CANDLES ${tech.error?"FAIL":`OK (${tech.candle_source}, ${tech.n_bars} bars, latest ${tech.latest_bar})`}`,!tech.error)}
        ${tag(`FMP ${f.error?"FAIL":"OK"}`,!f.error)}
        ${tag(`AV EARNINGS ${eh.error?"FAIL":"OK"}`,!eh.error)}
        ${tag(`YFINANCE ${ye.error?"FAIL":"OK"}`,!ye.error)}
      </div>
      ${[q,tech,f,eh,ye].filter(b=>b.error).map(b=>`<div class="err">⚠ ${esc(b.source)}: ${esc(b.error)}</div>`).join("")}
      <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input type="password" id="fhKey" placeholder="Finnhub key → live quote (optional)"
          value="${esc(localStorage.getItem("fh_key")||"")}" style="width:260px">
        <button class="tabs-btn" style="font-family:var(--sans);font-weight:600;font-size:12px;
          background:var(--live);color:#1A1408;border:none;border-radius:4px;padding:8px 14px;cursor:pointer"
          onclick="toggleLive()"><span id="liveBtn">${pollTimer?"Stop live":"Go live (10s)"}</span></button>
      </div>
    </div>

    ${price!=null?`<div class="panel">
      <h2>${esc(t.symbol)}${f.name?" — "+esc(f.name):""} ${tag(q.error?"LAST CLOSE":"QUOTE AT RUN")}</h2>
      <div style="display:flex;align-items:baseline;gap:16px;flex-wrap:wrap">
        <span class="price" id="pxNow">$${fmt(price)}</span>
        ${chg!=null?`<span class="mono" id="pxChg" style="font-size:16px" class="${chg>=0?"up":"down"}">
          <span class="${chg>=0?"up":"down"}">${chg>=0?"+":""}${fmt(chg)} (${chg>=0?"+":""}${fmt(q.change_pct)}%)</span></span>`:""}
      </div>
      ${tech.trend?`<div class="stats" style="margin-top:16px">
        ${["daily","weekly","monthly"].map(k=>{
          const tr=tech.trend[k], cls=tr.label==="BULLISH"?"up":tr.label==="BEARISH"?"down":"blue";
          return stat(k+" trend",esc(tr.label),cls,esc(tr.detail));}).join("")}
        ${stat("52-wk range",`$${fmt(tech.lo_52wk)}–$${fmt(tech.hi_52wk)}`,"",
          `${fmt((price-tech.hi_52wk)/tech.hi_52wk*100,1)}% from high`)}
      </div>`:""}
    </div>`:""}

    ${tech.chart?`<div class="panel">
      <h2>Daily close + EMA 20/50/200 ${tag("COMPUTED FROM "+ (tech.candle_source||"").toUpperCase()+" CANDLES")}</h2>
      <div style="height:320px"><canvas id="cv"></canvas></div>
    </div>`:""}

    <div class="grid g2">
      ${tech.rsi14!=null?`<div class="panel"><h2>Indicators ${tag("COMPUTED")}</h2>
        <div class="stats" style="grid-template-columns:1fr 1fr">
          ${stat("RSI (14)",fmt(tech.rsi14,1),tech.rsi14>70?"down":tech.rsi14<30?"up":"",
            tech.rsi14>70?"overbought zone":tech.rsi14<30?"oversold zone":"neutral zone")}
          ${stat("MACD / signal",`${fmt(tech.macd)} / ${fmt(tech.macd_signal)}`,
            tech.macd>tech.macd_signal?"up":"down",
            tech.macd>tech.macd_signal?"bullish cross state":"bearish cross state")}
          ${stat("ATR (14)","$"+fmt(tech.atr14),"",fmt(tech.atr14/price*100,1)+"% daily range")}
          ${stat("EMA 20/50/200",`${fmt(tech.ema20)} / ${fmt(tech.ema50)} / ${fmt(tech.ema200)}`,"",
            price>tech.ema200?"price above EMA 200":"price below EMA 200")}
          ${stat("Vol vs 20d avg",fmt(tech.volume_last/tech.volume_avg20,2)+"×",
            tech.volume_last>=tech.volume_avg20?"up":"")}
        </div></div>`:""}

      ${tech.levels?`<div class="panel"><h2>Support / resistance ${tag("5-BAR FRACTALS")}</h2>
        <div class="mono" style="font-size:13px;line-height:2">
          <div style="color:var(--faint);font-size:11px;font-family:var(--sans)">Resistance above</div>
          ${tech.levels.resistance.length?tech.levels.resistance.map(v=>
            `<div class="down">▲ $${fmt(v)} <span style="color:var(--faint)">(${fmt((v-price)/price*100,1)}%)</span></div>`).join("")
            :`<div style="color:var(--faint)">none in history — at/near highs</div>`}
          <div style="color:var(--faint);font-size:11px;font-family:var(--sans);margin-top:8px">Support below</div>
          ${tech.levels.support.length?tech.levels.support.map(v=>
            `<div class="up">▼ $${fmt(v)} <span style="color:var(--faint)">(${fmt((v-price)/price*100,1)}%)</span></div>`).join("")
            :`<div style="color:var(--faint)">none in history</div>`}
        </div></div>`:""}
    </div>

    ${renderValuation(f,price)}

    ${!eh.error&&eh.quarters?`<div class="panel">
      <h2>Earnings history — estimate vs actual ${tag("ALPHA VANTAGE · as of "+ago(eh.fetched_at))}</h2>
      <table><tr><th>Fiscal period</th><th>Reported</th><th>EPS est.</th><th>EPS actual</th><th>Surprise</th></tr>
      ${eh.quarters.map(r=>{
        const sp=parseFloat(r.surprise_pct);
        return `<tr><td>${esc(r.period)}</td><td>${esc(r.reported||"—")}</td>
          <td>${esc(r.eps_estimate??"—")}</td><td>${esc(r.eps_actual??"—")}</td>
          <td class="${isFinite(sp)?(sp>=0?"up":"down"):""}">${isFinite(sp)?(sp>=0?"+":"")+fmt(sp,1)+"%":"—"}</td></tr>`;}).join("")}
      </table>
      <p class="note">Beats in shown quarters: ${esc(eh.beats_of_last||"—")}. Estimate-vs-actual is
      the one dataset only Alpha Vantage provides free — refreshed weekly to protect its 25-call/day limit.</p>
    </div>`:""}

    ${!ye.error?`<div class="panel">
      <h2>Positioning & analyst view ${tag("YFINANCE · as of "+ago(ye.fetched_at))}</h2>
      <div class="stats" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">
        ${stat("Analyst mean target",ye.analyst_target_mean!=null?"$"+fmt(ye.analyst_target_mean):"—","",
          ye.analyst_count?`${ye.analyst_count} analysts · low $${fmt(ye.analyst_target_low)} / high $${fmt(ye.analyst_target_high)}`:"")}
        ${stat("Consensus",esc(ye.recommendation||"—"))}
        ${stat("Forward P/E",fmt(ye.forward_pe),"",ye.forward_eps!=null?`fwd EPS ${fmt(ye.forward_eps)}`:"")}
        ${stat("Short % of float",ye.short_pct_of_float!=null?pc(ye.short_pct_of_float):"—","",
          ye.short_ratio_days_to_cover!=null?`${fmt(ye.short_ratio_days_to_cover,1)} days to cover`:"")}
        ${stat("Institutions / insiders",`${pc(ye.institutions_pct)} / ${pc(ye.insiders_pct)}`)}
        ${stat("Next earnings",ye.next_earnings_unix?new Date(ye.next_earnings_unix*1000).toLocaleDateString():"—")}
      </div>
      <p class="note">${esc(ye.note||"")}${ye.short_interest_asof_unix?` Short interest as of ${new Date(ye.short_interest_asof_unix*1000).toLocaleDateString()}.`:""}</p>
    </div>`:""}

    ${!mac.error&&(mac.cpi_yoy_latest||mac.fed_funds_rate)?`<div class="panel">
      <h2>Macro regime ${tag("ALPHA VANTAGE · as of "+ago(mac.fetched_at))}</h2>
      <div class="stats" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">
        ${mac.cpi_yoy_latest?stat("CPI YoY",mac.cpi_yoy_latest.yoy_pct!=null?fmt(mac.cpi_yoy_latest.yoy_pct,1)+"%":"—","",mac.cpi_yoy_latest.date):""}
        ${mac.fed_funds_rate?stat("Fed funds rate",fmt(mac.fed_funds_rate.value)+"%","",mac.fed_funds_rate.date):""}
        ${mac.treasury_10y?stat("10-yr treasury",fmt(mac.treasury_10y.value)+"%","",mac.treasury_10y.date):""}
      </div>
    </div>`:""}
  </div>`;

  if(tech.chart) drawChart(tech.chart);
  document.getElementById("fhKey").addEventListener("change",e=>
    localStorage.setItem("fh_key",e.target.value.trim()));
}

function renderValuation(f,price){
  if(f.error||f.is_etf===undefined) return "";
  if(f.is_etf) return `<div class="panel"><h2>Fund detected ${tag("HONESTY",false)}</h2>
    <p class="note">${esc(CUR.symbol)} is an ETF/fund — statements, DCF, and Graham don't apply.
    A fund's intrinsic value is its NAV; holdings data isn't on free tiers, so nothing is shown
    rather than something invented.</p></div>`;
  const g=A.growth/100,d=A.discount/100,tg=A.terminal/100;
  let dcf=null;
  if(f.fcf_base>0&&f.shares&&d>tg){
    let pv=0,fc=f.fcf_base;
    for(let y=1;y<=5;y++){fc*=1+g;pv+=fc/Math.pow(1+d,y);}
    pv+=(fc*(1+tg))/(d-tg)/Math.pow(1+d,5);
    dcf=pv/f.shares;
  }
  const graham=f.eps_ttm_diluted>0&&f.book_value_per_share>0
    ?Math.sqrt(22.5*f.eps_ttm_diluted*f.book_value_per_share):null;
  const lg=f.eps_cagr!=null?Math.min(Math.max(f.eps_cagr*100,0),25):null;
  const lynch=f.eps_ttm_diluted>0&&lg>0?f.eps_ttm_diluted*lg:null;
  const models=[["DCF (your assumptions)",dcf],["Graham Number",graham],["Lynch fair value",lynch]]
    .filter(m=>m[1]!=null&&m[1]>0);
  const fair=models.length?models.reduce((s,m)=>s+m[1],0)/models.length:null;
  const gap=fair&&price?(price-fair)/fair:null;
  const ideal=fair?fair*(1-A.mos/100):null;
  const verdict=gap==null?null:gap<=-0.3?["DEEP DISCOUNT to blended fair value","up"]
    :gap<=-0.1?["MODEST DISCOUNT to blended fair value","up"]
    :gap<0.1?["NEAR blended fair value","blue"]
    :gap<0.3?["MODEST PREMIUM to blended fair value","down"]
    :["SIGNIFICANT PREMIUM to blended fair value","down"];
  const slider=(label,key,min,max,step)=>`<div class="slider">
    <div class="row"><span>${label}</span><span>${fmt(A[key],step<1?1:0)}%</span></div>
    <input type="range" min="${min}" max="${max}" step="${step}" value="${A[key]}"
      oninput="A.${key}=+this.value;render()"></div>`;
  return `<div class="grid g2">
    <div class="panel"><h2>Intrinsic value ${tag("VISIBLE MATH · FMP "+ago(f.fetched_at))}</h2>
      ${fair?`<div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap">
        <span class="mono" style="font-size:24px">fair ≈ $${fmt(fair)}</span>
        <span class="mono ${gap>=0?"down":"up"}" style="font-size:14px">price ${gap>=0?"+":""}${fmt(gap*100,1)}% vs fair</span>
      </div>
      ${verdict?`<div class="mono ${verdict[1]}" style="margin-top:8px;font-size:14px">● ${verdict[0]}</div>`:""}
      <div class="stats" style="margin-top:14px;grid-template-columns:1fr 1fr">
        ${stat(`Buy price @ ${A.mos}% margin of safety`,"$"+fmt(ideal),"amber","fair × (1 − MoS)")}
        ${stat("Models used",models.length+"/3","",models.map(m=>m[0].split(" ")[0]).join(" · "))}
      </div>
      <table style="margin-top:10px"><tr><th>Model</th><th>Value</th></tr>
        ${[["DCF (your assumptions)",dcf],["Graham Number",graham],["Lynch fair value",lynch]].map(m=>
          `<tr><td>${m[0]}</td><td>${m[1]?"$"+fmt(m[1]):"N/A"}</td></tr>`).join("")}
      </table>`:`<p class="note">Not enough positive inputs (EPS/FCF/book) to run any model — shown as N/A, not guessed.</p>`}
      <p class="note">Model output from the assumptions on the right — a starting point for your
      judgment, not a recommendation. Statements: annual, fiscal ${esc(f.period||"—")}.</p>
    </div>
    <div class="panel"><h2>Assumptions ${tag("ADJUSTABLE")}</h2>
      <div class="grid" style="gap:14px">
        ${slider("FCF growth, years 1–5","growth",-5,30,1)}
        ${slider("Discount rate","discount",6,16,0.5)}
        ${slider("Terminal growth","terminal",0,4,0.5)}
        ${slider("Margin of safety","mos",0,50,5)}
      </div>
      <div class="stats" style="margin-top:16px;grid-template-columns:1fr 1fr">
        ${stat("Rev CAGR 3y / 5y",`${pc(f.rev_cagr_3y)} / ${pc(f.rev_cagr_5y)}`)}
        ${stat("EPS / FCF CAGR",`${pc(f.eps_cagr)} / ${pc(f.fcf_cagr)}`)}
        ${stat("Margins G/O/N",`${pc(f.gross_margin,0)} / ${pc(f.op_margin,0)} / ${pc(f.net_margin,0)}`)}
        ${stat("ROE · D/E · IntCov",`${pc(f.roe,0)} · ${fmt(f.debt_to_equity)} · ${f.interest_coverage?fmt(f.interest_coverage,1)+"×":"—"}`)}
        ${stat("Net cash (debt)",big(f.net_cash),f.net_cash>=0?"up":"down")}
        ${stat("P/E · PEG (TTM)",`${fmt(f.pe_ttm)} · ${fmt(f.peg_ttm)}`)}
      </div>
    </div>
  </div>`;
}

function drawChart(rows){
  const el=document.getElementById("cv");
  if(!el||typeof Chart==="undefined")return;
  if(chart)chart.destroy();
  const last=rows.slice(-180);
  chart=new Chart(el,{type:"line",
    data:{labels:last.map(r=>r.date.slice(5)),
      datasets:[
        {label:"Close",data:last.map(r=>r.close),borderColor:"#E7E4DC",borderWidth:1.6,pointRadius:0},
        {label:"EMA 20",data:last.map(r=>r.ema20),borderColor:"#4CAF7D",borderWidth:1,pointRadius:0},
        {label:"EMA 50",data:last.map(r=>r.ema50),borderColor:"#7A9BC4",borderWidth:1,pointRadius:0},
        {label:"EMA 200",data:last.map(r=>r.ema200),borderColor:"#E8A33D",borderWidth:1.2,pointRadius:0},
      ]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
      plugins:{legend:{labels:{color:"#8B8F98",font:{family:"IBM Plex Mono",size:10}}}},
      scales:{x:{ticks:{color:"#5C606A",font:{family:"IBM Plex Mono",size:9},maxTicksLimit:12},
          grid:{color:"#2A2E36"}},
        y:{ticks:{color:"#5C606A",font:{family:"IBM Plex Mono",size:9}},grid:{color:"#2A2E36"}}}}});
}

async function toggleLive(){
  if(pollTimer){clearInterval(pollTimer);pollTimer=null;
    document.getElementById("liveBtn").textContent="Go live (10s)";
    document.getElementById("liveNote").innerHTML="";return;}
  const key=(document.getElementById("fhKey").value||"").trim();
  if(!key){alert("Paste a Finnhub key first — it stays in this browser only.");return;}
  localStorage.setItem("fh_key",key);
  const tick=async()=>{
    try{
      const j=await(await fetch(`https://finnhub.io/api/v1/quote?symbol=${CUR.symbol}&token=${key}`)).json();
      if(!j.c)throw new Error("no quote");
      const px=document.getElementById("pxNow");
      if(px)px.textContent="$"+fmt(j.c);
      const ch=document.getElementById("pxChg");
      if(ch)ch.innerHTML=`<span class="${j.d>=0?"up":"down"}">${j.d>=0?"+":""}${fmt(j.d)} (${j.d>=0?"+":""}${fmt(j.dp)}%)</span>`;
      document.getElementById("liveNote").innerHTML=
        `<span class="up pulse">● LIVE $${fmt(j.c)} @ ${new Date().toLocaleTimeString()}</span>`;
    }catch(e){
      clearInterval(pollTimer);pollTimer=null;
      document.getElementById("liveBtn").textContent="Go live (10s)";
      document.getElementById("liveNote").innerHTML=`<span class="err">live quote failed: ${esc(e.message||e)}</span>`;
    }};
  await tick();
  pollTimer=setInterval(tick,10000);
  document.getElementById("liveBtn").textContent="Stop live";
}

window.pick=pick;window.toggleLive=toggleLive;window.A=A;window.render=render;
boot();
</script>
</body>
</html>
