import time
from datetime import datetime, timezone
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title='Paper Scalping 4.0', layout='wide')
st.title('AI Trading Bot 4.0 – 7-Tage-Challenge')
st.caption('Nur Paper-Trading. Keine echten Orders. 100 → 1.000 USD Ziel, keine Gewinnzusage.')

START = 100.0
SYMBOLS = {'BTC': 'XBTUSD', 'SOL': 'SOLUSD', 'ETH': 'ETHUSD'}
if 'bot' not in st.session_state:
    st.session_state.bot = dict(cash=START, positions={}, trades=[], started=time.time(), checks=0,
                                last_candle={}, last_check=None, peak=START, stopped=False)
b = st.session_state.bot

with st.sidebar:
    st.header('Einstellungen')
    enabled = st.toggle('Automatische Paper-Trades', value=False)
    leverage = st.slider('Maximaler Hebel', 1, 5, 3)
    risk_pct = st.slider('Risiko pro Position (% Equity)', 0.25, 3.0, 1.0, 0.25)
    max_positions = st.slider('Gleichzeitige Positionen', 1, 3, 2)
    fee_pct = st.number_input('Gebühr je Seite (%)', min_value=0.0, max_value=1.0, value=0.10, step=0.01)
    slippage_pct = st.number_input('Slippage je Seite (%)', min_value=0.0, max_value=1.0, value=0.05, step=0.01)
    max_drawdown = st.slider('Maximaler Drawdown (%)', 5, 50, 20)
    if st.button('Simulation zurücksetzen'):
        del st.session_state.bot
        st.rerun()

@st.cache_data(ttl=2, show_spinner=False)
def market(coin):
    pair = SYMBOLS[coin]
    r = requests.get('https://api.kraken.com/0/public/OHLC', params={'pair': pair, 'interval': 1}, timeout=8)
    r.raise_for_status()
    payload = r.json()
    if payload.get('error'):
        raise RuntimeError(str(payload['error']))
    key = next(k for k in payload['result'] if k != 'last')
    raw = payload['result'][key]
    df = pd.DataFrame(raw, columns=['time','open','high','low','close','vwap','volume','count'])
    for col in ('open','high','low','close','volume'):
        df[col] = pd.to_numeric(df[col])
    return df

def analyze(df):
    d = df.iloc[:-1].copy()  # Completed candles only
    if len(d) < 55:
        return 'HOLD', 50.0, 0.0, None
    close = d['close']
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = float((100 - 100/(1 + up/(down + 1e-12))).iloc[-1])
    prev = close.shift(1)
    tr = pd.concat([(d['high']-d['low']), (d['high']-prev).abs(), (d['low']-prev).abs()], axis=1).max(axis=1)
    atr = float(tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1])
    volume_ok = bool(d['volume'].iloc[-1] > d['volume'].iloc[-21:-1].mean() * 1.1)
    trend_up = ema9.iloc[-1] > ema21.iloc[-1] and ema9.iloc[-1] > ema9.iloc[-2]
    trend_down = ema9.iloc[-1] < ema21.iloc[-1] and ema9.iloc[-1] < ema9.iloc[-2]
    side = 'LONG' if volume_ok and trend_up and 53 < rsi < 70 else ('SHORT' if volume_ok and trend_down and 30 < rsi < 47 else 'HOLD')
    return side, round(rsi, 1), atr, int(d['time'].iloc[-1])

def fill(price, side, entering):
    # Adverse execution on both entry and exit
    buy = (side == 'LONG') == entering
    return price * (1 + (slippage_pct/100) * (1 if buy else -1))

def value(prices):
    total = b['cash']
    for coin, p in b['positions'].items():
        price = prices.get(coin, p['last'])
        move = (price/p['entry'] - 1) * (1 if p['side']=='LONG' else -1)
        total += p['margin'] + p['notional']*move
    return total

def close_trade(coin, price, why):
    p = b['positions'].pop(coin)
    executed = fill(price, p['side'], False)
    move = (executed/p['entry'] - 1) * (1 if p['side']=='LONG' else -1)
    exit_fee = p['notional']*fee_pct/100
    net = p['notional']*move - p['entry_fee'] - exit_fee
    b['cash'] += p['margin'] + p['notional']*move - exit_fee
    b['trades'].insert(0, dict(time=datetime.now(timezone.utc).isoformat(timespec='seconds'), coin=coin,
                              side=p['side'], action='CLOSE', pnl=round(net, 4), reason=why))

def open_trade(coin, side, price, atr, eq):
    if coin in b['positions'] or len(b['positions']) >= max_positions:
        return
    stop_fraction = max(0.003, min(0.02, 1.5*atr/price))
    # Limit notional based on equity at risk; cap leverage exposure and cash margin
    notional = min(eq*(risk_pct/100)/stop_fraction, eq*leverage/max_positions, b['cash']*leverage*0.8)
    if notional < 2:
        return
    margin = notional/leverage
    entry_fee = notional*fee_pct/100
    if margin + entry_fee > b['cash']:
        return
    executed = fill(price, side, True)
    b['cash'] -= margin + entry_fee
    b['positions'][coin] = dict(side=side, entry=executed, last=price, notional=notional, margin=margin,
                                entry_fee=entry_fee, stop_fraction=stop_fraction, best=0.0)
    b['trades'].insert(0, dict(time=datetime.now(timezone.utc).isoformat(timespec='seconds'), coin=coin,
                              side=side, action='OPEN', pnl=round(-entry_fee, 4), reason='EMA/RSI/Volume'))

@st.fragment(run_every='2s')
def live():
    b['checks'] += 1
    b['last_check'] = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
    prices, candidates, rows, errors = {}, [], [], []
    for coin in SYMBOLS:
        try:
            df = market(coin)
            # Last OHLC row is forming; its close is a near-live estimate, not a tick feed
            price = float(df['close'].iloc[-1])
            signal, rsi, atr, candle = analyze(df)
            prices[coin] = price
            rows.append(dict(coin=coin, price=round(price, 3), RSI=rsi, signal=signal))
            candidates.append((coin, signal, price, atr, candle))
        except Exception as e:
            errors.append(f'{coin}: {type(e).__name__}: {e}')
    if errors:
        st.warning('Marktdatenfehler. Betroffene Positionen können ohne Kurs nicht geschlossen werden. ' + ' | '.join(errors))
    eq = value(prices)
    b['peak'] = max(b['peak'], eq)
    if eq <= b['peak']*(1-max_drawdown/100):
        b['stopped'] = True
    if enabled:
        for coin, p in list(b['positions'].items()):
            if coin not in prices:
                continue
            price = prices[coin]
            p['last'] = price
            signed = (price/p['entry']-1)*(1 if p['side']=='LONG' else -1)
            p['best'] = max(p['best'], signed)
            stop = p['stop_fraction']
            if signed <= -stop:
                close_trade(coin, price, 'Stop-Loss (Polling)')
            elif signed >= 2*stop:
                close_trade(coin, price, 'Take-Profit')
            elif p['best'] >= stop and signed <= p['best'] - stop*0.65:
                close_trade(coin, price, 'Trailing-Stop')
        if not b['stopped'] and not errors:
            for coin, side, price, atr, candle in candidates:
                if side != 'HOLD' and candle is not None and b['last_candle'].get(coin) != candle:
                    if coin not in b['positions'] and len(b['positions']) < max_positions:
                        open_trade(coin, side, price, atr, value(prices))
                        if coin in b['positions']:
                            b['last_candle'][coin] = candle
    eq = value(prices)
    elapsed = (time.time()-b['started'])/86400
    c1,c2,c3 = st.columns(3)
    c1.metric('Equity (USD)', f'{eq:.2f}', f'{eq-START:+.2f}')
    c2.metric('Ziel', '1.000 USD', f'{eq/1000*100:.1f}% erreicht')
    c3.metric('Zeit verbleibend', f'{max(0,7-elapsed):.2f} Tage')
    st.progress(min(1.0, max(0.0, eq/1000)))
    st.write(f'Automatik: {"AN" if enabled else "AUS"} · Prüfungen: {b["checks"]} · Letzte Prüfung: {b["last_check"]}')
    if b['stopped']:
        st.error('Drawdown-Grenze erreicht. Neue Einstiege gesperrt; bestehende Positionen werden bei eingeschalteter Automatik weiter überwacht.')
    if elapsed >= 7:
        st.info('Die sieben Tage sind abgelaufen. Die Simulation läuft weiter, solange sie geöffnet ist.')
    st.table(pd.DataFrame(rows))
    st.subheader('Offene Positionen')
    st.table(pd.DataFrame([dict(coin=c, **p) for c,p in b['positions'].items()]), hide_index=True, use_container_width=True)
    st.subheader('Handelsprotokoll')
    st.table(pd.DataFrame(b['trades'])
    closed = [t['pnl'] for t in b['trades'] if t['action']=='CLOSE']
    st.write(f'Geschlossene Trades: {len(closed)} · Trefferquote: {100*sum(x>0 for x in closed)/len(closed):.1f}%' if closed else 'Noch keine geschlossenen Trades')
    st.caption('Daten von Kraken OHLC (1-Minute), kein Tickfeed. Aktualisierung ~2 Sekunden, nicht garantiert. Keine Funding-, Liquidations- oder vollständige Spread-Modellierung. Sitzungsdaten können verloren gehen. Keine echte KI oder Renditegarantie.')

live()

