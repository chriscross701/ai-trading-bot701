import time
from datetime import datetime, timezone
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title='Paper Scalping Bot 4.0', layout='wide')
st.title('Paper Scalping Bot 4.0')
st.caption('Simulation only · Kraken USD market data · no real orders · 1-second target, not guaranteed')

COINS = {'BTC': 'XBTUSD', 'SOL': 'SOLUSD', 'ETH': 'ETHUSD'}
START_BALANCE = 100.0

if 'account' not in st.session_state:
    st.session_state.account = dict(cash=START_BALANCE, positions={}, trades=[], checks=0, day_start=START_BALANCE, day=datetime.now(timezone.utc).date().isoformat(), last_entry={}, last_closed={}, last_check=None)
a = st.session_state.account

with st.sidebar:
    st.header('Paper-Einstellungen')
    enabled = st.toggle('Automatische Paper-Trades', value=False)
    leverage = st.slider('Hebel', 1, 5, 2)
    margin = st.slider('Margin je Position (USD)', 1.0, 10.0, 5.0)
    stop_pct = st.slider('Stop-Loss: Kursbewegung (%)', 0.2, 3.0, 0.7, 0.1)
    take_pct = st.slider('Take-Profit: Kursbewegung (%)', 0.2, 5.0, 1.2, 0.1)
    fee_pct = st.number_input('Gebühr pro Seite (%)', min_value=0.0, max_value=2.0, value=0.10, step=0.01)
    spread_pct = st.number_input('Simulierter Spread gesamt (%)', min_value=0.0, max_value=2.0, value=0.05, step=0.01)
    slippage_pct = st.number_input('Slippage pro Seite (%)', min_value=0.0, max_value=2.0, value=0.03, step=0.01)
    max_loss = st.slider('Maximaler Tagesverlust (USD)', 1.0, 20.0, 3.0)
    cooldown = st.slider('Pause nach Positionsschluss (Sek.)', 0, 300, 30)
    if st.button('Simulation zurücksetzen'):
        st.session_state.pop('account', None)
        st.rerun()

@st.cache_data(ttl=1, show_spinner=False)
def ticker(pair):
    r = requests.get('https://api.kraken.com/0/public/Ticker', params={'pair': pair}, timeout=5)
    r.raise_for_status()
    payload = r.json()
    if payload.get('error'):
        raise ValueError(str(payload['error']))
    return float(next(iter(payload['result'].values()))['c'][0])

@st.cache_data(ttl=15, show_spinner=False)
def candles(pair):
    r = requests.get('https://api.kraken.com/0/public/OHLC', params={'pair': pair, 'interval': 1}, timeout=5)
    r.raise_for_status()
    payload = r.json()
    if payload.get('error'):
        raise ValueError(str(payload['error']))
    key = next(k for k in payload['result'] if k != 'last')
    rows = payload['result'][key][-100:]
    df = pd.DataFrame(rows)
    df['close'] = pd.to_numeric(df[4])
    df['volume'] = pd.to_numeric(df[6])
    df['timestamp'] = pd.to_numeric(df[0])
    return df

def get_signal(df):
    d = df.iloc[:-1].copy()  # Do not use an unfinished 1-minute candle.
    if len(d) < 35:
        return 'HOLD', None, False, None
    close, volume = d['close'], d['volume']
    fast = close.ewm(span=9, adjust=False).mean()
    slow = close.ewm(span=21, adjust=False).mean()
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = float((100 - 100 / (1 + up / (down + 1e-12))).iloc[-1])
    volume_ok = bool(volume.iloc[-1] > volume.iloc[-21:-1].mean())
    direction = 'HOLD'
    if volume_ok and fast.iloc[-1] > slow.iloc[-1] and 52 < rsi < 72:
        direction = 'LONG'
    elif volume_ok and fast.iloc[-1] < slow.iloc[-1] and 28 < rsi < 48:
        direction = 'SHORT'
    return direction, round(rsi, 1), volume_ok, int(d['timestamp'].iloc[-1])

def fill_price(price, side, opening):
    # Long buy/short cover costs more; short sell/long exit receives less.
    buy = (side == 'LONG') == opening
    adjustment = (spread_pct / 2 + slippage_pct) / 100
    return price * (1 + adjustment if buy else 1 - adjustment)

def unrealized(p, price):
    move = (price / p['entry'] - 1) * (1 if p['side'] == 'LONG' else -1)
    return p['notional'] * move

def account_equity(prices):
    total = a['cash']
    for coin, p in a['positions'].items():
        total += p['margin'] + unrealized(p, prices.get(coin, p['entry']))
    return total

def close_position(coin, price, reason):
    p = a['positions'].pop(coin)
    execution = fill_price(price, p['side'], False)
    gross = unrealized(p, execution)
    exit_fee = p['notional'] * fee_pct / 100
    net = gross - p['entry_fee'] - exit_fee
    a['cash'] += max(0.0, p['margin'] + gross - exit_fee)
    a['last_closed'][coin] = time.time()
    a['trades'].insert(0, dict(time=datetime.now(timezone.utc).isoformat(timespec='seconds'), coin=coin, side=p['side'], entry=round(p['entry'], 5), exit=round(execution, 5), net_pnl=round(net, 4), reason=reason))

def open_position(coin, side, price, candle_id):
    if coin in a['positions'] or len(a['positions']) >= 3:
        return
    notional = margin * leverage
    entry_fee = notional * fee_pct / 100
    if a['cash'] < margin + entry_fee:
        return
    entry = fill_price(price, side, True)
    a['cash'] -= margin + entry_fee
    a['positions'][coin] = dict(coin=coin, side=side, entry=entry, margin=margin, notional=notional, entry_fee=entry_fee, opened=time.time())
    a['last_entry'][coin] = candle_id

@st.fragment(run_every='1s')
def live():
    a['checks'] += 1
    a['last_check'] = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
    prices, signals, errors = {}, [], []
    for coin, pair in COINS.items():
        try:
            price = ticker(pair)
            direction, rsi, vol_ok, candle_id = get_signal(candles(pair))
            prices[coin] = price
            signals.append(dict(coin=coin, price=round(price, 4), rsi=rsi, volume_ok=vol_ok, signal=direction, candle_id=candle_id))
        except Exception as exc:
            errors.append(f'{coin}: {type(exc).__name__}: {exc}')

    today = datetime.now(timezone.utc).date().isoformat()
    if a['day'] != today:
        a['day'], a['day_start'] = today, account_equity(prices)

    # Manage existing exposure even when automation is switched off.
    for coin, p in list(a['positions'].items()):
        if coin not in prices:
            continue
        current = prices[coin]
        signed_move = (current / p['entry'] - 1) * (1 if p['side'] == 'LONG' else -1) * 100
        opposite = next((s for s in signals if s['coin'] == coin), None)
        if signed_move <= -stop_pct:
            close_position(coin, current, 'STOP (polling)')
        elif signed_move >= take_pct:
            close_position(coin, current, 'TAKE PROFIT')
        elif opposite and opposite['signal'] not in ('HOLD', p['side']):
            close_position(coin, current, 'OPPOSITE SIGNAL')
        elif signed_move <= -100 / leverage:
            close_position(coin, current, 'SIMULATED MARGIN EXHAUSTION')

    eq = account_equity(prices)
    halted = eq <= a['day_start'] - max_loss
    if halted:
        for coin in list(a['positions']):
            if coin in prices:
                close_position(coin, prices[coin], 'DAILY LOSS LIMIT')
        eq = account_equity(prices)

    if enabled and not halted and not errors:
        for s in signals:
            coin = s['coin']
            if s['signal'] == 'HOLD' or coin in a['positions'] or s['candle_id'] is None:
                continue
            if a['last_entry'].get(coin) == s['candle_id']:
                continue
            if time.time() - a['last_closed'].get(coin, 0) < cooldown:
                continue
            open_position(coin, s['signal'], prices[coin], s['candle_id'])

    eq = account_equity(prices)
    st.metric('Geschätzte Equity (USD)', f'{eq:.2f}', delta=f'{eq-START_BALANCE:+.2f}')
    st.write(f'Automatik: {"AN" if enabled else "AUS"} · Prüfungen: {a["checks"]} · Letzte Prüfung: {a["last_check"]}')
    st.caption('USD, keine EUR-Umrechnung. 1-Minuten-Kerzen erzeugen keine neuen abgeschlossenen Signale jede Sekunde.')
    if errors:
        st.warning('Marktdatenfehler: ' + ' | '.join(errors))
    if halted:
        st.error('Tagesverlustlimit erreicht. Keine neuen Positionen bis zum nächsten UTC-Tag.')
    if signals:
        st.dataframe(pd.DataFrame(signals).drop(columns='candle_id'), hide_index=True, use_container_width=True)
    st.subheader('Offene Positionen')
    if a['positions']:
        st.dataframe(pd.DataFrame(list(a['positions'].values())), hide_index=True, use_container_width=True)
    else:
        st.write('Keine offenen Positionen')
    st.subheader('Handelsprotokoll')
    if a['trades']:
        trades = pd.DataFrame(a['trades'])
        st.dataframe(trades, hide_index=True, use_container_width=True)
        closed = trades['net_pnl']
        c1, c2, c3 = st.columns(3)
        c1.metric('Geschlossene Trades', len(closed))
        c2.metric('Gewinnquote', f'{(closed.gt(0).mean()*100):.1f}%')
        c3.metric('Realisierter PnL (USD)', f'{closed.sum():+.2f}')
    else:
        st.write('Noch keine geschlossenen Trades')
    st.caption('Technischer Paper-Test: keine garantierte 1-s-Frequenz, kein echter WebSocket-Tickfeed, keine echten Börsenfills. Daten und Positionen sind nur sitzungsgebunden.')

live()