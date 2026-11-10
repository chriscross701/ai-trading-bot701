import time
from datetime import datetime, timezone
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Trading Bot 5.0 | Paper Scanner", layout="wide")
st.title("Trading Bot 5.0 – Top-100 Paper Scanner")
st.caption("Nur Simulation. Keine echten Orders, keine KI-API. USD-Konto. Keine Renditegarantie.")
START = 100.0
CG = "https://api.coingecko.com/api/v3/coins/markets"
KRAKEN = "https://api.kraken.com/0/public"
S = requests.Session()
S.headers.update({"User-Agent": "PaperScanner/5.0 (educational)"})

if "bot5" not in st.session_state:
    st.session_state.bot5 = dict(cash=START, positions={}, trades=[], checks=0, peak=START,
                                 last_candle={}, started=time.time(), stopped=False, errors=[])
b = st.session_state.bot5

with st.sidebar:
    st.header("Paper-Trading Einstellungen")
    enabled = st.toggle("Automatische Paper-Trades", value=False)
    leverage = st.slider("Hebel (Simulation)", 1, 5, 2)
    risk_pct = st.slider("Kursrisiko je Position (% Equity)", 0.25, 2.0, 0.5, 0.25)
    max_positions = st.slider("Maximal offene Positionen", 1, 3, 2)
    fee_pct = st.number_input("Gebühr je Seite (%)", 0.0, 1.0, 0.10, 0.01)
    slip_pct = st.number_input("Slippage je Seite (%)", 0.0, 1.0, 0.05, 0.01)
    max_dd = st.slider("Maximaler Drawdown (%)", 5, 40, 15)
    scan_n = st.slider("Kandidaten für Kerzenanalyse", 3, 12, 6)
    if st.button("Simulation zurücksetzen"):
        del st.session_state.bot5
        st.rerun()

@st.cache_data(ttl=120, show_spinner=False)
def top100():
    r = requests.get(CG, params={"vs_currency":"usd", "order":"market_cap_desc",
        "per_page":100, "page":1, "sparkline":"false"}, timeout=12)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=3600, show_spinner=False)
def kraken_pairs():
    r = requests.get(KRAKEN + "/AssetPairs", timeout=15)
    r.raise_for_status()
    payload = r.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    pairs = {}
    for key, v in payload["result"].items():
        ws = v.get("wsname", "")
        if ws.endswith("/USD") and v.get("status", "online") == "online":
            base = ws.split("/")[0].upper()
            pairs.setdefault(base, v.get("altname", key))
    return pairs

@st.cache_data(ttl=60, show_spinner=False)
def ohlc(pair):
    r = requests.get(KRAKEN + "/OHLC", params={"pair":pair,"interval":1}, timeout=12)
    r.raise_for_status()
    payload = r.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    key = next(k for k in payload["result"] if k != "last")
    df = pd.DataFrame(payload["result"][key], columns=["time","open","high","low","close","vwap","volume","count"])
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col])
    return df

def signal(df):
    d = df.iloc[:-1]
    if len(d) < 55:
        return "HOLD", 50., 0., None, 0.
    c = d["close"]
    f = c.ewm(span=9,adjust=False).mean()
    s = c.ewm(span=21,adjust=False).mean()
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    rsi = float((100 - 100/(1+up/(down+1e-12))).iloc[-1])
    prev = c.shift(1)
    tr = pd.concat([d["high"]-d["low"],(d["high"]-prev).abs(),(d["low"]-prev).abs()],axis=1).max(axis=1)
    atr = float(tr.ewm(alpha=1/14,adjust=False).mean().iloc[-1])
    vr = float(d["volume"].iloc[-1] / max(d["volume"].iloc[-21:-1].mean(), 1e-12))
    long = f.iloc[-1]>s.iloc[-1] and f.iloc[-1]>f.iloc[-2] and 53<rsi<72 and vr>1.05
    short = f.iloc[-1]<s.iloc[-1] and f.iloc[-1]<f.iloc[-2] and 28<rsi<47 and vr>1.05
    side = "LONG" if long else "SHORT" if short else "HOLD"
    score = min(100., max(0., (vr-1)*30 + abs(f.iloc[-1]/s.iloc[-1]-1)*10000)) if side != "HOLD" else 0.
    return side, rsi, atr, int(d["time"].iloc[-1]), score

def fill(price, side, entering):
    buy = (side=="LONG") == entering
    return price*(1+(slip_pct/100)*(1 if buy else -1))

def equity(prices):
    eq = b["cash"]
    for coin,p in b["positions"].items():
        px = prices.get(coin,p["last"])
        move = (px/p["entry"]-1)*(1 if p["side"]=="LONG" else -1)
        eq += p["margin"] + p["notional"]*move
    return eq

def close_position(coin, px, reason):
    p = b["positions"].pop(coin)
    executed = fill(px,p["side"],False)
    move = (executed/p["entry"]-1)*(1 if p["side"]=="LONG" else -1)
    exit_fee = p["notional"]*fee_pct/100
    net = p["notional"]*move-p["entry_fee"]-exit_fee
    b["cash"] += p["margin"]+p["notional"]*move-exit_fee
    b["trades"].insert(0,dict(time=datetime.now(timezone.utc).isoformat(timespec="seconds"),coin=coin,
                              side=p["side"],action="CLOSE",pnl=round(net,4),reason=reason))

def open_position(coin, side, px, atr, eq):
    if coin in b["positions"] or len(b["positions"])>=max_positions:
        return False
    stop = max(0.004,min(0.025,1.5*atr/px))
    notional = min(eq*risk_pct/100/stop, eq*leverage/max_positions, b["cash"]*leverage*0.75)
    margin = notional/leverage
    entry_fee = notional*fee_pct/100
    if notional < 5 or margin+entry_fee>b["cash"]:
        return False
    entry = fill(px,side,True)
    b["cash"] -= margin+entry_fee
    b["positions"][coin] = dict(side=side,entry=entry,last=px,margin=margin,notional=notional,
        entry_fee=entry_fee,stop=stop,best=0.,opened=time.time())
    b["trades"].insert(0,dict(time=datetime.now(timezone.utc).isoformat(timespec="seconds"),coin=coin,
                              side=side,action="OPEN",pnl=round(-entry_fee,4),reason="Ranked EMA/RSI/Volume"))
    return True

@st.fragment(run_every="2s")
def live():
    b["checks"] += 1
    errors=[]
    try:
        coins=top100()
        pairs=kraken_pairs()
    except Exception as e:
        st.error(f"Marktübersicht nicht verfügbar: {e}. Keine neuen Orders.")
        return
    # CoinGecko ranks by market cap; exclude stablecoins and wrapped tokens.
    banned={"USDT","USDC","DAI","FDUSD","USDE","TUSD","WBTC","WETH","STETH","WEETH","WSTETH"}
    shortlist=[]
    for c in coins:
        sym=c["symbol"].upper()
        lookup="XBT" if sym=="BTC" else sym
        if sym in banned or lookup not in pairs or not c.get("total_volume") or c["total_volume"]<5_000_000:
            continue
        # More active USD markets first; ranking is a shortlist, not proof of edge.
        shortlist.append((sym,pairs[lookup],c))
    shortlist.sort(key=lambda x: float(x[2].get("total_volume") or 0),reverse=True)
    selected=shortlist[:scan_n]
    # Always watch open positions, even if they fall out of the shortlist.
    watch={sym:pair for sym,pair,_ in selected}
    for coin in b["positions"]:
        if coin not in watch:
            for sym,pair,_ in shortlist:
                if sym==coin:
                    watch[coin]=pair
                    break
    prices={}; candidates=[]; rows=[]
    for sym,pair in watch.items():
        try:
            df=ohlc(pair)
            px=float(df["close"].iloc[-1])
            side,rsi,atr,candle,score=signal(df)
            prices[sym]=px
            rows.append(dict(coin=sym,price=round(px,5),RSI=round(rsi,1),signal=side,score=round(score,1)))
            candidates.append((score,sym,side,px,atr,candle))
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:90]}")
    if errors:
        st.warning("Einige Kurse fehlen. Keine neuen Einstiege bei Datenfehlern: "+" | ".join(errors[:4]))
    eq=equity(prices)
    b["peak"]=max(b["peak"],eq)
    if eq <= b["peak"]*(1-max_dd/100):
        b["stopped"]=True
    if enabled:
        for coin,p in list(b["positions"].items()):
            if coin not in prices:
                continue
            px=prices[coin]
            p["last"]=px
            signed=(px/p["entry"]-1)*(1 if p["side"]=="LONG" else -1)
            p["best"]=max(p["best"],signed)
            opposite=next((x for x in candidates if x[1]==coin),None)
            if signed <= -p["stop"]:
                close_position(coin,px,"Stop-Loss (polling)")
            elif signed >= 2*p["stop"]:
                close_position(coin,px,"Take-Profit")
            elif p["best"]>=p["stop"] and signed<=p["best"]-0.65*p["stop"]:
                close_position(coin,px,"Trailing-Stop")
            elif opposite and opposite[2] not in ("HOLD",p["side"]):
                close_position(coin,px,"Gegensignal")
        if not b["stopped"] and not errors:
            for score,coin,side,px,atr,candle in sorted(candidates,reverse=True):
                if side=="HOLD" or candle is None or b["last_candle"].get(coin)==candle:
                    continue
                if open_position(coin,side,px,atr,equity(prices)):
                    b["last_candle"][coin]=candle
                if len(b["positions"])>=max_positions:
                    break
    eq=equity(prices)
    closed=[t["pnl"] for t in b["trades"] if t["action"]=="CLOSE"]
    c1,c2,c3=st.columns(3)
    c1.metric("Equity (USD)",f"{eq:.2f}",f"{eq-START:+.2f}")
    c2.metric("Ziel 1.000 USD",f"{eq/10:.1f}%")
    c3.metric("Geschlossene Trades",str(len(closed)))
    st.write(f"Automatik: {'AN' if enabled else 'AUS'} · Prüfungen: {b['checks']} · Kandidaten: {len(watch)} · Letzte Prüfung: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    if b["stopped"]:
        st.error("Drawdown-Limit erreicht: keine neuen Positionen. Bestehende Positionen weiter überwachen.")
    st.subheader("Signal-Ranking (Kraken-USD-Märkte)")
    st.table(pd.DataFrame(rows).sort_values("score",ascending=False) if rows else pd.DataFrame())
    st.subheader("Offene Positionen")
    st.write(b["positions"] if b["positions"] else "Keine")
    st.subheader("Handelsprotokoll")
    st.table(pd.DataFrame(b["trades"][:100]) if b["trades"] else pd.DataFrame())
    if closed:
        st.write(f"Netto-PnL geschlossene Trades: {sum(closed):+.2f} USD · Trefferquote: {sum(x>0 for x in closed)/len(closed)*100:.1f}%")
    st.caption("Top-100-Liste alle 120 s, OHLC alle 60 s; Oberfläche ca. alle 2 s. Kein Tickfeed. Nur ein Teil der Top 100 wird mit Kerzendaten analysiert. Keine Funding-, Liquidations- oder vollständige Spread-Modellierung. Daten nur in Sitzung.")

live()
