import time
from datetime import datetime, timezone
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Trading Bot 6.1 | Paper Scanner", layout="wide")
st.title("Trading Bot 6.1 – Top-100 Paper Scanner")
st.caption("Nur Simulation. Keine echten Orders, keine KI-API. USD-Konto. Keine Renditegarantie.")
START = 100.0
CG = "https://api.coingecko.com/api/v3/coins/markets"
KRAKEN = "https://api.kraken.com/0/public"
S = requests.Session()
S.headers.update({"User-Agent": "PaperScanner/6.1 (educational)"})

if "bot5" not in st.session_state:
    st.session_state.bot5 = dict(cash=START, positions={}, trades=[], checks=0, peak=START,
                                 last_candle={}, started=time.time(), stopped=False, errors=[], scan_cursor=0, snapshots={}, cooldown={}, ticks={}, last_signal={}, last_price_time={})
b = st.session_state.bot5

with st.sidebar:
    page = st.radio("Bereich", ["Backtest", "Paper-Trading"], index=0)
    st.header("Paper-Trading Einstellungen")
    enabled = st.toggle("Automatische Paper-Trades", value=False)
    leverage = st.slider("Hebel (Simulation)", 1, 5, 2)
    risk_pct = st.slider("Kursrisiko je Position (% Equity)", 0.25, 2.0, 0.5, 0.25)
    max_positions = st.slider("Maximal offene Positionen", 1, 3, 2)
    fee_pct = st.number_input("Gebühr je Seite (%)", 0.0, 1.0, 0.10, 0.01)
    slip_pct = st.number_input("Slippage je Seite (%)", 0.0, 1.0, 0.05, 0.01)
    max_dd = st.slider("Maximaler Drawdown (%)", 5, 40, 15)
    scan_n = st.slider("Coins im Scan-Pool", 20, 60, 50, 5)
    batch_n = st.slider("Coins je Scan-Runde", 3, 10, 5)
    max_minutes = st.slider("Maximale Haltedauer (Minuten)", 5, 120, 30, 5)
    cooldown_minutes = st.slider("Pause nach Schließung (Minuten)", 1, 30, 5)
    min_score = st.slider("Mindest-Score für Einstieg", 10, 100, 35, 5)
    use_5m = st.toggle("5-Minuten-Trendfilter", value=True)
    use_micro = st.toggle("30-Sekunden-Momentumfilter (Ticker-Samples)", value=True)
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

@st.cache_data(ttl=2, show_spinner=False)
def ticker(pair):
    r=requests.get(KRAKEN+"/Ticker",params={"pair":pair},timeout=8)
    r.raise_for_status()
    j=r.json()
    if j.get("error"):
        raise RuntimeError(str(j["error"]))
    item=next(iter(j["result"].values()))
    return float(item["c"][0])

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
    # Score is a heuristic, not a probability or profit prediction.
    trend = abs(float(f.iloc[-1] / s.iloc[-1] - 1)) * 10000
    vol_score = max(0., (vr - 0.8) * 22)
    momentum = abs(rsi - 50) * 0.9
    score = min(100., trend * 1.4 + vol_score + momentum)
    if side == "HOLD":
        score = min(score, 49.9)
    # Five-minute trend is calculated from completed one-minute bars.
    # Require 5m EMA9/21 agreement, not 200-bar history.
    five = d.assign(dt=pd.to_datetime(d["time"],unit="s",utc=True)).set_index("dt")["close"].resample("5min").last().dropna()
    # Drop potentially incomplete 5m group.
    if len(five) > 2 and int(d["time"].iloc[-1]) % 300 != 240:
        five = five.iloc[:-1]
    if len(five) >= 25 and use_5m:
        fast5 = five.ewm(span=9,adjust=False).mean()
        slow5 = five.ewm(span=21,adjust=False).mean()
        if side == "LONG" and fast5.iloc[-1] <= slow5.iloc[-1]:
            side = "HOLD"
        elif side == "SHORT" and fast5.iloc[-1] >= slow5.iloc[-1]:
            side = "HOLD"
    elif use_5m:
        side = "HOLD"
    if side == "HOLD":
        score = min(score, 49.9)
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
    b["cooldown"][coin] = time.time()
    b["trades"].insert(0,dict(time=datetime.now(timezone.utc).isoformat(timespec="seconds"),coin=coin,
                              side=p["side"],action="CLOSE",pnl=round(net,4),reason=reason))

def open_position(coin, side, px, atr, eq):
    if coin in b["positions"] or len(b["positions"])>=max_positions:
        return False
    if time.time() - b["cooldown"].get(coin, 0) < cooldown_minutes * 60:
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
    # Top-100 by market cap, but filter unsuitable/pegged assets.
    banned = {"USDT","USDC","DAI","FDUSD","USDE","TUSD","WBTC","WETH",
              "STETH","WEETH","WSTETH","USD1","PYUSD","USDD","USDS",
              "USDP","RLUSD","EURC","EURCV","FRAX","SUSDE","BUIDL"}
    shortlist = []
    for c in coins:
        sym = str(c.get("symbol", "")).upper()
        name = str(c.get("name", "")).lower()
        lookup = "XBT" if sym == "BTC" else sym
        if (sym in banned or lookup not in pairs or
            any(word in name for word in ("stablecoin", "wrapped", "bridged", "tokenized")) or
            float(c.get("total_volume") or 0) < 5_000_000):
            continue
        # Ignore likely USD-pegged assets; this also excludes some legitimate $1 coins.
        if ("usd" in name and "bitcoin" not in name) or "dollar" in name:
            continue
        shortlist.append((sym, pairs[lookup], c))
    shortlist.sort(key=lambda x: float(x[2].get("total_volume") or 0), reverse=True)
    selected = shortlist[:scan_n]
    pool = {sym: pair for sym, pair, _ in selected}
    # Keep open positions watched even if they drop out of the current pool.
    for coin in b["positions"]:
        if coin not in pool:
            pair = next((pair for sym, pair, _ in shortlist if sym == coin), None)
            if pair:
                pool[coin] = pair

    # Scan only a batch per refresh. This avoids bursts of 50 HTTP requests
    # and gradually covers the whole pool (~20s for 50 coins at 5 per 2s).
    selected_keys = [sym for sym, _, _ in selected]
    cursor = b["scan_cursor"] % max(1, len(selected_keys))
    batch = [selected_keys[(cursor + i) % len(selected_keys)]
             for i in range(min(batch_n, len(selected_keys)))]
    b["scan_cursor"] = (cursor + len(batch)) % max(1, len(selected_keys))
    watch = dict((sym, pool[sym]) for sym in batch)
    for coin in b["positions"]:
        if coin in pool:
            watch[coin] = pool[coin]

    prices, errors = {}, []
    now = time.time()
    for sym, pair in watch.items():
        try:
            df = ohlc(pair)
            # A fast sampled ticker price; NOT a true 30-second candle.
            px = ticker(pair)
            history = b["ticks"].setdefault(sym, [])
            history.append((time.time(), px))
            history[:] = [(t,p) for t,p in history if time.time()-t <= 100]
            sample = next((p for t,p in history if time.time()-t >= 25), None)
            micro = ((px/sample)-1) if sample and sample>0 else None
            side, rsi, atr, candle, score = signal(df)
            prices[sym] = px
            b["snapshots"][sym] = dict(coin=sym, price=px, RSI=round(rsi, 1),
                signal=side, score=round(score, 1), atr=atr, candle=candle,
                updated=now, micro=micro)
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:90]}")

    # Discard stale signals; stale entries must never trigger trades.
    b["snapshots"] = {sym: item for sym, item in b["snapshots"].items()
                      if sym in pool and now - item["updated"] < 180}
    fresh = [item for item in b["snapshots"].values()
             if now - item["updated"] < 75]
    rows = [dict(coin=x["coin"], price=round(x["price"], 5),
                 RSI=x["RSI"], signal=x["signal"], score=x["score"],
                 age_s=int(now-x["updated"]),
                 momentum30s=round(x["micro"]*100,3) if x.get("micro") is not None else None) for x in fresh]
    candidates = [(x["score"], x["coin"], x["signal"], x["price"],
                   x["atr"], x["candle"]) for x in fresh]
    if errors:
        st.warning("Marktdatenfehler: " + " | ".join(errors[:4]))
    if len(fresh) < len(selected):
        st.info(f"Scanner füllt sich: {len(fresh)} von {len(selected)} Coins aktuell bewertet.")
    # Mark all open positions with freshest available ticker (even outside scan batch).
    for coin,p in list(b["positions"].items()):
        if coin not in prices and coin in pool:
            try:
                prices[coin]=ticker(pool[coin])
            except Exception as e:
                errors.append(f"{coin} exit-price unavailable: {str(e)[:70]}")
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
                prev=b["last_signal"].get(coin)
                if prev and prev[0]==opposite[2] and prev[1]!=opposite[5]:
                    close_position(coin,px,"Bestätigtes Gegensignal")
                else:
                    b["last_signal"][coin]=(opposite[2],opposite[5])
            elif time.time()-p["opened"] >= max_minutes*60:
                close_position(coin,px,"Zeitlimit")
        if not b["stopped"]:
            # Only open on a coin scanned THIS round; old rankings are for display.
            for score,coin,side,px,atr,candle in sorted(candidates,reverse=True):
                snap=b["snapshots"].get(coin,{})
                micro=snap.get("micro")
                if (coin not in watch or coin not in prices or side=="HOLD" or
                    candle is None or b["last_candle"].get(coin)==candle or score<min_score):
                    continue
                # Wait until at least 25 seconds of actual ticker samples are available.
                if use_micro and (micro is None or (side=="LONG" and micro<=0.0001) or (side=="SHORT" and micro>=-0.0001)):
                    continue
                if open_position(coin,side,px,atr,equity(prices)):
                    b["last_candle"][coin]=candle
                if len(b["positions"])>=max_positions:
                    break
    eq=equity(prices)
    closed=[t["pnl"] for t in b["trades"] if t["action"]=="CLOSE"]
    last10=closed[:10]
    last20=closed[:20]
    last50=closed[:50]
    def stats(xs):
        if not xs:
            return "–"
        return f"{sum(xs):+.2f} USD | {100*sum(v>0 for v in xs)/len(xs):.0f}% Treffer"
    c1,c2,c3=st.columns(3)
    c1.metric("Equity (USD)",f"{eq:.2f}",f"{eq-START:+.2f}")
    c2.metric("Ziel 1.000 USD",f"{eq/10:.1f}%")
    c3.metric("Geschlossene Trades",str(len(closed)))
    st.write(f"Automatik: {'AN' if enabled else 'AUS'} · Prüfungen: {b['checks']} · Aktuell bewertet: {len(fresh)}/{len(selected)} · Letzte Prüfung: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    if b["stopped"]:
        st.error("Drawdown-Limit erreicht: keine neuen Positionen. Bestehende Positionen weiter überwachen.")
    st.write(f"Letzte 10 Trades ({len(last10)}): {stats(last10)}")
    st.write(f"Letzte 20 Trades ({len(last20)}): {stats(last20)}")
    st.write(f"Letzte 50 Trades ({len(last50)}): {stats(last50)}")
    st.caption("30-Sekunden-Filter nutzt gesammelte Ticker-Samples, keine echten 30-Sekunden-Kerzen. Nach Neustart braucht er Aufwärmzeit.")
    st.subheader("Signal-Ranking (Kraken-USD-Märkte)")
    if rows:
        ranking = pd.DataFrame(rows).sort_values(["signal", "score"], ascending=[True, False])
        active = ranking[ranking["signal"] != "HOLD"].sort_values("score", ascending=False)
        st.write(f"Handelssignale: {len(active)} · Scan-Pool: {len(selected)} Coins")
        st.table(active.head(10) if not active.empty else ranking.sort_values("score", ascending=False).head(10))
        with st.expander("Alle aktuell bewerteten Coins"):
            st.table(ranking.sort_values("score", ascending=False))
    else:
        st.info("Warte auf erste Marktdaten.")
    st.subheader("Offene Positionen")
    st.write(b["positions"] if b["positions"] else "Keine")
    st.subheader("Handelsprotokoll")
    st.table(pd.DataFrame(b["trades"][:100]) if b["trades"] else pd.DataFrame())
    if closed:
        st.write(f"Netto-PnL geschlossene Trades: {sum(closed):+.2f} USD · Trefferquote: {sum(x>0 for x in closed)/len(closed)*100:.1f}%")
    st.caption("Top-100-Liste alle 120 s; OHLC je Coin bis zu 60 s gecacht; Scanner prüft Coins in Teilgruppen. Keine echte Echtzeit- oder 24/7-Garantie. Nur liquide Kraken-USD-Märkte aus den Top 100 werden analysiert. Keine Funding-, Liquidations- oder vollständige Spread-Modellierung. Daten nur in Sitzung.")


def backtest_signals(df):
    """Indicators from completed 1-minute bars; never uses a future bar."""
    d = df.iloc[:-1].copy().reset_index(drop=True)
    c = d["close"]
    ema9 = c.ewm(span=9, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    ema200 = c.ewm(span=200, adjust=False).mean()
    diff = c.diff()
    gain = diff.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-diff.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = 100 - 100/(1 + gain/(loss+1e-12))
    prev = c.shift()
    tr = pd.concat([d["high"]-d["low"], (d["high"]-prev).abs(), (d["low"]-prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False).mean()
    plus_dm = (d["high"].diff()).where((d["high"].diff()>-d["low"].diff()) & (d["high"].diff()>0), 0)
    minus_dm = (-d["low"].diff()).where((-d["low"].diff()>d["high"].diff()) & (-d["low"].diff()>0), 0)
    atr_s = tr.ewm(alpha=1/14, adjust=False).mean().replace(0,float("nan"))
    plus_di = 100*plus_dm.ewm(alpha=1/14,adjust=False).mean()/atr_s
    minus_di = 100*minus_dm.ewm(alpha=1/14,adjust=False).mean()/atr_s
    dx = 100*(plus_di-minus_di).abs()/(plus_di+minus_di).replace(0,float("nan"))
    adx = dx.ewm(alpha=1/14,adjust=False).mean()
    rvol = d["volume"] / d["volume"].shift(1).rolling(20).mean().replace(0,float("nan"))
    high20 = d["high"].shift(1).rolling(20).max()
    low20 = d["low"].shift(1).rolling(20).min()
    # Four fixed rules, no parameter fitting on the test split.
    strategies = {
        "EMA/RSI/Volumen": (
            (ema9>ema21)&(ema9>ema9.shift())&(rsi>53)&(rsi<70)&(rvol>1.1),
            (ema9<ema21)&(ema9<ema9.shift())&(rsi>30)&(rsi<47)&(rvol>1.1)),
        "ADX Trend": (
            (ema50>ema200)&(ema21>ema50)&(adx>23)&(plus_di>minus_di)&(rvol>1.0),
            (ema50<ema200)&(ema21<ema50)&(adx>23)&(minus_di>plus_di)&(rvol>1.0)),
        "Volumen-Breakout": (
            (c>high20)&(rvol>1.5)&(rsi<80),
            (c<low20)&(rvol>1.5)&(rsi>20)),
        "Trend-Pullback": (
            (ema50>ema200)&(c>ema50)&(c.shift(1)<=ema9.shift(1))&(c>ema9)&(rsi>48)&(rsi<68),
            (ema50<ema200)&(c<ema50)&(c.shift(1)>=ema9.shift(1))&(c<ema9)&(rsi<52)&(rsi>32))
    }
    return d, atr, strategies

def simulate_strategy(d, atr, long_cond, short_cond, start, end, fee, slip, minutes, capital=100.0):
    """Next-bar open entry, pessimistic stop priority, next-open time exit."""
    cash = capital
    trades = []
    pos = None
    last_exit = -999
    stop_k = 1.5
    for i in range(max(start, 205), min(end, len(d)-1)):
        bar = d.iloc[i]
        nxt = d.iloc[i+1]
        if pos:
            side, entry, notional, margin, stop_dist, opened = pos
            sign = 1 if side=="LONG" else -1
            # Stops/targets use the next completed bar. If both touched, stop wins.
            stop_price = entry*(1-sign*stop_dist)
            target_price = entry*(1+sign*2*stop_dist)
            hit_stop = bar["low"]<=stop_price if sign==1 else bar["high"]>=stop_price
            hit_target = bar["high"]>=target_price if sign==1 else bar["low"]<=target_price
            reason = None
            if hit_stop:
                raw = min(bar["open"],stop_price) if sign==1 else max(bar["open"],stop_price)
                reason="Stop"
            elif hit_target:
                raw=target_price
                reason="Ziel"
            elif i-opened>=minutes:
                raw=float(nxt["open"])
                reason="Zeitlimit"
            if reason:
                exit_px=raw*(1-sign*slip)
                pnl=notional*sign*(exit_px/entry-1)-notional*fee*2
                cash+=pnl
                trades.append(dict(pnl=pnl, reason=reason, side=side))
                pos=None
                last_exit=i
                if cash<=0:
                    break
        if pos is None and i>last_exit+2 and i<end-1:
            side = "LONG" if bool(long_cond.iloc[i]) else ("SHORT" if bool(short_cond.iloc[i]) else None)
            if side and pd.notna(atr.iloc[i]) and atr.iloc[i]>0:
                # Fixed 1% risk, 3x cap, 50% of capital margin max.
                entry_raw=float(nxt["open"])
                stop_dist=max(.003,min(.02,1.5*float(atr.iloc[i])/entry_raw))
                notional=min(cash*.01/stop_dist, cash*3*.5)
                if notional<=0: continue
                sign=1 if side=="LONG" else -1
                entry=entry_raw*(1+sign*slip)
                pos=(side,entry,notional,notional/3,stop_dist,i+1)
    # Mark to market any still-open position at the final close, including exit costs.
    if pos and cash>0:
        side, entry, notional, margin, stop_dist, opened=pos
        sign=1 if side=="LONG" else -1
        exit_px=float(d.iloc[min(end-1,len(d)-1)]["close"])*(1-sign*slip)
        pnl=notional*sign*(exit_px/entry-1)-notional*fee*2
        cash+=pnl
        trades.append(dict(pnl=pnl, reason="Testende", side=side))
    wins=sum(t["pnl"]>0 for t in trades)
    gross_profit=sum(max(0,t["pnl"]) for t in trades)
    gross_loss=-sum(min(0,t["pnl"]) for t in trades)
    return dict(trades=len(trades), winrate=100*wins/len(trades) if trades else 0,
                net=round(cash-capital,3), final=round(cash,3),
                pf=round(gross_profit/gross_loss,2) if gross_loss>0 else (float("nan") if not trades else 99.0))

def show_backtest():
    st.header("Strategie-Backtest 6.1")
    st.warning("Kraken stellt hier nur ca. 720 Minutenkerzen pro Coin bereit (maximal rund 12 Stunden). Das ist ein Kurztest, kein Nachweis einer profitablen Strategie.")
    try:
        available=kraken_pairs()
    except Exception as exc:
        st.error(f"Kraken-Märkte nicht verfügbar: {exc}")
        return
    common={"BTC":"XBT","ETH":"ETH","SOL":"SOL","ADA":"ADA","XRP":"XRP","LINK":"LINK","AVAX":"AVAX","DOT":"DOT","LTC":"LTC","AAVE":"AAVE"}
    choices={coin:available[base] for coin,base in common.items() if base in available}
    selected=st.multiselect("Coins für historischen Vergleich",list(choices),default=list(choices)[:3],max_selections=6)
    hold=st.slider("Maximale Haltedauer (Minuten)",3,30,10)
    test_fraction=st.slider("Unberührter Testanteil (%)",20,50,30,5)
    st.caption("Annahmen: 3× maximaler Hebel, 1 % Kursrisiko je Position, 0,1 % Gebühren und 0,05 % Slippage pro Seite; Stop-Loss 1,5 ATR, Ziel 2R. Signale nach Kerzenschluss, Einstieg zur nächsten Kerzeneröffnung.")
    if not st.button("Vier Strategien historisch vergleichen",type="primary"):
        return
    if not selected:
        st.info("Bitte mindestens einen Coin auswählen.")
        return
    results=[]
    failures=[]
    with st.spinner("Lade historische Daten und berechne Strategien ..."):
        for coin in selected:
            try:
                data=ohlc(choices[coin])
                d,atr,strategies=backtest_signals(data)
                cut=int(len(d)*(1-test_fraction/100))
                if cut<220 or len(d)-cut<50:
                    failures.append(f"{coin}: Zu wenige Kerzen ({len(d)})")
                    continue
                for name,(longs,shorts) in strategies.items():
                    train=simulate_strategy(d,atr,longs,shorts,205,cut,.001,.0005,hold)
                    test=simulate_strategy(d,atr,longs,shorts,cut,len(d),.001,.0005,hold)
                    results.append(dict(Coin=coin,Strategie=name,
                        Training_USD=train["net"],Test_USD=test["net"],
                        Test_Trades=test["trades"],Test_Trefferquote=f'{test["winrate"]:.0f}%',
                        Test_Profitfaktor=test["pf"]))
            except Exception as exc:
                failures.append(f"{coin}: {type(exc).__name__}: {str(exc)[:110]}")
    if failures:
        st.warning(" | ".join(failures))
    if not results:
        st.error("Keine auswertbaren Ergebnisse. Bitte andere Coins oder später erneut versuchen.")
        return
    table=pd.DataFrame(results)
    st.subheader("Ergebnisse auf getrennten Zeitabschnitten")
    st.table(table.sort_values("Test_USD",ascending=False))
    grouped=table.groupby("Strategie",as_index=False).agg(
        Test_USD=("Test_USD","sum"),Test_Trades=("Test_Trades","sum"),
        Positive_Coins=("Test_USD",lambda x:int((x>0).sum())))
    st.subheader("Zusammenfassung der vier Strategien")
    st.table(grouped.sort_values("Test_USD",ascending=False))
    st.info("Die Ergebnisse sind ein explorativer Vergleich. Bei nur wenigen Trades oder wenigen Stunden Daten sind sie stark zufallsabhängig. Kein automatisches Umschalten auf die scheinbar beste Strategie: Das würde die Testdaten nachträglich zur Optimierung verwenden.")
    st.caption("Einschränkungen: vereinfachte Ausführung, kein Orderbuch-Spread, keine Funding-/Liquidationskosten, keine Portfolio-Korrelation. Gleichzeitiger Stop und Ziel wird pessimistisch als Stop gewertet. Keine echte 30-Sekunden-Strategie.")

if page == "Paper-Trading":
    live()
else:
    show_backtest()
