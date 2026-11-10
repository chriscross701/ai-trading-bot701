import json
import urllib.request
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

st.set_page_config(page_title="AI Trading Bot 3.0", page_icon="📈", layout="wide")
st.title("AI Trading Bot 3.0 – Paper Scalping")
st.caption("Nur Simulation: Long / Short, 1–5x Hebel. Keine echten Orders und keine KI-API.")

PAIRS = {"BTC": "XXBTZEUR", "SOL": "SOLEUR", "ETH": "XETHZEUR"}
FEE_RATE = 0.0008  # Annahme: 0,08 %? Nein: 0,08 % pro Seite; anpassen an Broker
# Für konservative Simulation 0,8 % pro Seite (Taker-Basisannahme)
FEE_RATE = 0.008
START = 100.0

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "PaperTradingDemo/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data["result"]

def get_market(coin):
    pair = PAIRS[coin]
    data = fetch_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1")
    key = next(k for k in data if k != "last")
    rows = data[key]
    # Kraken liefert die letzte, noch laufende Kerze: nicht für Signale verwenden.
    closed = rows[:-1]
    if len(closed) < 30:
        raise RuntimeError("Zu wenige abgeschlossene Kerzen")
    df = pd.DataFrame(closed, columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"])
    for c in ("close", "volume"):
        df[c] = pd.to_numeric(df[c])
    close = df["close"]
    df["ema9"] = close.ewm(span=9, adjust=False).mean()
    df["ema21"] = close.ewm(span=21, adjust=False).mean()
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = up / down.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))
    last, prev = df.iloc[-1], df.iloc[-2]
    vol_avg = df["volume"].iloc[-21:-1].mean()
    volume_ok = last["volume"] > vol_avg * 1.1
    long_signal = prev["ema9"] <= prev["ema21"] and last["ema9"] > last["ema21"] and 50 < last["rsi"] < 75 and volume_ok
    short_signal = prev["ema9"] >= prev["ema21"] and last["ema9"] < last["ema21"] and 25 < last["rsi"] < 50 and volume_ok
    signal = "LONG" if long_signal else "SHORT" if short_signal else "HOLD"
    return {"coin": coin, "price": float(last["close"]), "rsi": float(last["rsi"]), "signal": signal, "candle": int(last["time"]), "volume_ok": bool(volume_ok)}

for k, v in {"cash": START, "position": None, "trades": [], "last_signal": None, "enabled": False, "day": datetime.now(timezone.utc).date().isoformat(), "day_start": START, "halted": False}.items():
    if k not in st.session_state:
        st.session_state[k] = v

with st.sidebar:
    st.header("Einstellungen")
    st.toggle("Automatische Paper-Trades", key="enabled")
    leverage = st.slider("Hebel (Simulation)", 1, 5, 2)
    margin = st.slider("Margin pro Position (€)", 2, 10, 5)
    stop_pct = st.slider("Stop-Loss: Kursbewegung (%)", 0.5, 3.0, 1.0, 0.5)
    take_pct = st.slider("Take-Profit: Kursbewegung (%)", 1.0, 6.0, 2.0, 0.5)
    st.caption("Gebührenannahme: 0,8 % pro Orderseite auf den Positionswert. Kein Funding, kein Spread/Slippage modelliert.")
    if st.button("Simulation zurücksetzen"):
        for k in ("cash", "position", "trades", "last_signal", "halted"):
            del st.session_state[k]
        st.rerun()

@st.fragment(run_every="10s")
def monitor():
    today = datetime.now(timezone.utc).date().isoformat()
    if st.session_state.day != today:
        st.session_state.day = today
        st.session_state.day_start = st.session_state.cash
        st.session_state.halted = False
    results = []
    errors = []
    for coin in PAIRS:
        try:
            results.append(get_market(coin))
        except Exception as exc:
            errors.append(f"{coin}: {exc}")
    if errors:
        st.warning("Marktdatenfehler – keine neuen Trades bei unvollständigen Daten: " + "; ".join(errors))
    prices = {x["coin"]: x["price"] for x in results}
    pos = st.session_state.position
    if pos and pos["coin"] in prices:
        price = prices[pos["coin"]]
        direction = 1 if pos["side"] == "LONG" else -1
        gross = direction * (price - pos["entry"]) / pos["entry"] * pos["notional"]
        equity = st.session_state.cash + pos["margin"] + gross - pos["notional"] * FEE_RATE
    else:
        equity = st.session_state.cash + (pos["margin"] if pos else 0)
    st.metric("Geschätztes Kontoguthaben (Equity)", f"{equity:.2f} €", f"{equity-START:+.2f} €")
    st.write(f"Freies Guthaben: **{st.session_state.cash:.2f} €** · Automatik: **{'AN' if st.session_state.enabled else 'AUS'}**")
    if results:
        st.dataframe(pd.DataFrame(results)[["coin", "price", "rsi", "signal", "volume_ok"]], hide_index=True, use_container_width=True)
    if pos and pos["coin"] in prices:
        price = prices[pos["coin"]]
        direction = 1 if pos["side"] == "LONG" else -1
        movement = direction * (price / pos["entry"] - 1) * 100
        gross = pos["notional"] * movement / 100
        exit_fee = pos["notional"] * FEE_RATE
        # Stop/TP auch ohne Automatik als simulierte Sicherheitslogik prüfen.
        reason = "STOP" if movement <= -pos["stop"] else "TAKE" if movement >= pos["take"] else None
        if reason:
            st.session_state.cash += pos["margin"] + gross - exit_fee
            st.session_state.trades.append({"Zeit UTC": datetime.now(timezone.utc).isoformat(timespec="seconds"), "Coin": pos["coin"], "Aktion": f"CLOSE {pos['side']} ({reason})", "PnL €": round(gross - pos["entry_fee"] - exit_fee, 3)})
            st.session_state.position = None
            st.info(f"Simulierte Position geschlossen: {reason}")
        else:
            st.info(f"Offen: {pos['side']} {pos['coin']} | Einstieg {pos['entry']:.2f} € | Bewegung {movement:+.2f}% | Brutto-PnL {gross:+.3f} €")
    if st.session_state.cash <= st.session_state.day_start - 2:
        st.session_state.halted = True
    if st.session_state.halted:
        st.error("Tagesverlustlimit erreicht: keine neuen Positionen.")
    # Nur auf neuer abgeschlossener Kerze und nur mit vollständigen Marktdaten.
    if st.session_state.enabled and not errors and not st.session_state.position and not st.session_state.halted:
        for x in results:
            if x["signal"] == "HOLD":
                continue
            key = (x["coin"], x["candle"])
            if st.session_state.last_signal == key:
                continue
            notional = margin * leverage
            # 0,5 € geplantes Kursrisiko pro Trade, exklusive Gebühren.
            if notional * stop_pct / 100 > 0.50 or st.session_state.cash < margin + notional * FEE_RATE:
                continue
            entry_fee = notional * FEE_RATE
            st.session_state.cash -= margin + entry_fee
            st.session_state.position = {"coin": x["coin"], "side": x["signal"], "entry": x["price"], "margin": margin, "notional": notional, "entry_fee": entry_fee, "stop": stop_pct, "take": take_pct}
            st.session_state.last_signal = key
            st.session_state.trades.append({"Zeit UTC": datetime.now(timezone.utc).isoformat(timespec="seconds"), "Coin": x["coin"], "Aktion": f"OPEN {x['signal']} {leverage}x", "PnL €": -round(entry_fee, 3)})
            st.success(f"Virtuelle {x['signal']}-Position eröffnet: {x['coin']}, {leverage}x")
            break
    st.subheader("Handelsprotokoll (nur aktuelle Sitzung)")
    st.dataframe(pd.DataFrame(st.session_state.trades), hide_index=True, use_container_width=True)
    st.caption("Alle 10 Sekunden nur bei aktiver Sitzung; iPhone-Sperre/Cloud-Sleep unterbrechen die Prüfung. Keine echte Liquidations-, Funding- oder Spread-Simulation. Nicht für Echtgeld geeignet.")

monitor()
