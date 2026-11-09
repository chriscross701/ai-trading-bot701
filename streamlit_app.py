import json
import urllib.request
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

st.set_page_config(page_title='AI Trading Bot 2.0', page_icon='📈', layout='wide')
st.title('AI TRADING BOT 2.0')
st.caption('Paper-Trading | Regelbasierte Signale | Kein Echtgeldhandel')

SYMBOLS = {'BTC': 'XBTEUR', 'SOL': 'SOLEUR', 'ETH': 'ETHEUR'}
FEE_RATE = 0.008  # conservative simulated taker fee per side

@st.cache_data(ttl=60)
def get_candles(pair):
    url = f'https://api.kraken.com/0/public/OHLC?pair={pair}&interval=60'
    with urllib.request.urlopen(url, timeout=12) as response:
        payload = json.load(response)
    if payload.get('error'):
        raise RuntimeError(str(payload['error']))
    key = next(k for k in payload['result'] if k != 'last')
    rows = payload['result'][key]
    df = pd.DataFrame(rows, columns=['time','open','high','low','close','vwap','volume','count'])
    df['close'] = pd.to_numeric(df['close'])
    df['time'] = pd.to_numeric(df['time'])
    return df.iloc[:-1].copy()  # exclude currently forming candle

def signal_for(df):
    closes = df['close']
    ema20 = closes.ewm(span=20, adjust=False).mean()
    ema50 = closes.ewm(span=50, adjust=False).mean()
    delta = closes.diff()
    gains = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    loss = float(losses.iloc[-1])
    gain = float(gains.iloc[-1])
    rsi = 100 if loss == 0 and gain > 0 else (50 if loss == 0 else 100 - 100 / (1 + gain / loss))
    trend_up = float(ema20.iloc[-1]) > float(ema50.iloc[-1])
    if trend_up and 50 <= rsi <= 70:
        signal = 'BUY'
    elif not trend_up or rsi >= 75:
        signal = 'SELL'
    else:
        signal = 'HOLD'
    return {'price': float(closes.iloc[-1]), 'rsi': round(rsi, 1), 'ema20': float(ema20.iloc[-1]), 'ema50': float(ema50.iloc[-1]), 'signal': signal, 'candle_time': int(df['time'].iloc[-1])}

if 'cash' not in st.session_state:
    st.session_state.cash = 100.0
    st.session_state.positions = {}
    st.session_state.trades = []
    st.session_state.last_processed = {}
    st.session_state.enabled = False

st.warning('Demo: Trades und Guthaben werden nur in dieser Sitzung gespeichert. Keine 24/7-Ausführung.')
st.session_state.enabled = st.toggle('Automatische Paper-Trades erlauben', value=st.session_state.enabled)
st.caption('Der Bot prüft Signale nur bei einem Klick auf „Signale prüfen und handeln“.')

try:
    market = {coin: signal_for(get_candles(pair)) for coin, pair in SYMBOLS.items()}
except Exception as exc:
    st.error(f'Marktdaten konnten nicht geladen werden: {exc}')
    st.stop()

portfolio = st.session_state.cash + sum(pos['qty'] * market[coin]['price'] for coin, pos in st.session_state.positions.items())
c1, c2, c3 = st.columns(3)
c1.metric('Portfolio', f'{portfolio:.2f} €', f'{portfolio - 100:.2f} €')
c2.metric('Freies Guthaben', f'{st.session_state.cash:.2f} €')
c3.metric('Offene Positionen', len(st.session_state.positions))
st.subheader('Marktanalyse (1-Stunden-Kerzen)')
st.dataframe(pd.DataFrame([{'Coin': c, 'Preis EUR': round(m['price'], 2), 'RSI': m['rsi'], 'EMA20': round(m['ema20'], 2), 'EMA50': round(m['ema50'], 2), 'Signal': m['signal']} for c, m in market.items()]), hide_index=True, use_container_width=True)

MAX_POSITION_EUR = 20.0
STOP_LOSS_PCT = 0.03
MAX_DAILY_LOSS = 2.0

def log_trade(coin, action, amount, reason):
    st.session_state.trades.append({'Zeit UTC': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'Coin': coin, 'Aktion': action, 'Betrag EUR': round(amount, 2), 'Grund': reason})

def sell(coin, price, reason):
    pos = st.session_state.positions.pop(coin)
    proceeds = pos['qty'] * price * (1 - FEE_RATE)
    st.session_state.cash += proceeds
    log_trade(coin, 'SELL', proceeds, reason)

def daily_pnl():
    # Simplified closed-trade P&L tracked per sale for daily circuit breaker
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(t.get('PnL EUR', 0) for t in st.session_state.trades if t['Zeit UTC'].startswith(today))

st.subheader('Handelssteuerung')
if st.button('Signale prüfen und handeln', type='primary'):
    if not st.session_state.enabled:
        st.info('Automatische Paper-Trades sind ausgeschaltet.')
    else:
        actions = []
        for coin, m in market.items():
            price = m['price']
            pos = st.session_state.positions.get(coin)
            if pos and (price <= pos['entry'] * (1 - STOP_LOSS_PCT) or m['signal'] == 'SELL'):
                entry_cost = pos['cost']
                sell(coin, price, 'Stop-Loss' if price <= pos['entry'] * (1 - STOP_LOSS_PCT) else 'Verkaufssignal')
                st.session_state.trades[-1]['PnL EUR'] = round(st.session_state.trades[-1]['Betrag EUR'] - entry_cost, 2)
                actions.append(f'{coin}: virtueller Verkauf')
            elif not pos and m['signal'] == 'BUY' and st.session_state.last_processed.get(coin) != m['candle_time']:
                if daily_pnl() <= -MAX_DAILY_LOSS:
                    actions.append(f'{coin}: Tagesverlustlimit erreicht')
                    continue
                amount = min(MAX_POSITION_EUR, st.session_state.cash)
                if amount >= 10:
                    qty = amount * (1 - FEE_RATE) / price
                    st.session_state.cash -= amount
                    st.session_state.positions[coin] = {'qty': qty, 'entry': price, 'cost': amount}
                    log_trade(coin, 'BUY', amount, 'EMA/RSI-Kaufsignal')
                    actions.append(f'{coin}: virtueller Kauf über {amount:.2f} €')
            st.session_state.last_processed[coin] = m['candle_time']
        if actions:
            st.success(' | '.join(actions))
        else:
            st.info('Keine neuen Trades. Bestehende Positionen bleiben unverändert.')

st.subheader('Offene Positionen')
if st.session_state.positions:
    st.dataframe(pd.DataFrame([{'Coin': c, 'Menge': p['qty'], 'Einstieg EUR': p['entry'], 'Wert EUR': round(p['qty'] * market[c]['price'], 2)} for c, p in st.session_state.positions.items()]), hide_index=True, use_container_width=True)
else:
    st.write('Keine offenen Positionen')
st.subheader('Handelsprotokoll')
if st.session_state.trades:
    st.dataframe(pd.DataFrame(st.session_state.trades), hide_index=True, use_container_width=True)
else:
    st.write('Noch keine Trades')
if st.button('Paper-Konto zurücksetzen'):
    st.session_state.cash = 100.0
    st.session_state.positions = {}
    st.session_state.trades = []
    st.session_state.last_processed = {}
    st.session_state.enabled = False
    st.rerun()
st.caption('Simulation ohne Slippage, mit 0,8 % Gebühren je Seite. Kein belastbarer Backtest oder Live-Trading-System.')