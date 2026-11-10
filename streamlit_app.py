import time
from datetime import datetime, timezone
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title='AI Trading Bot 3.1', layout='wide')
st.title('AI Trading Bot 3.1 – Paper Scalping')
st.caption('Simulation only. No exchange orders, no API keys. Refresh target: 1 second while session is active.')

if 'cash' not in st.session_state:
    st.session_state.cash = 100.0
    st.session_state.position = None
    st.session_state.trades = []
    st.session_state.checks = 0
    st.session_state.last_check = None
    st.session_state.test_started = False
    st.session_state.test_completed = False
    st.session_state.last_signal_candle = {}
    st.session_state.day_start = 100.0

with st.sidebar:
    st.header('Einstellungen')
    enabled = st.toggle('Automatische Paper-Trades', value=False)
    leverage = st.slider('Hebel (Simulation)', 1, 5, 2)
    margin = st.slider('Margin pro Position (€)', 1.0, 10.0, 5.0)
    stop_pct = st.slider('Stop-Loss: Kursbewegung (%)', 0.2, 3.0, 1.0, 0.1)
    take_pct = st.slider('Take-Profit: Kursbewegung (%)', 0.2, 5.0, 2.0, 0.1)
    fee_pct = st.number_input('Gebühr pro Orderseite (%)', 0.0, 2.0, 0.1, 0.01)
    test_mode = st.toggle('Einmaligen Testtrade ausführen', value=False, help='Öffnet eine kleine virtuelle BTC-Long-Position und schließt sie nach 30 Sekunden. Nur bei eingeschalteter Automatik.')
    if st.button('Simulation zurücksetzen'):
        for k in ('cash','position','trades','checks','last_check','test_started','test_completed','last_signal_candle','day_start'):
            st.session_state.pop(k, None)
        st.rerun()

@st.cache_data(ttl=1, show_spinner=False)
def ticker(symbol):
    response = requests.get('https://api.binance.com/api/v3/ticker/price', params={'symbol':symbol}, timeout=5)
    response.raise_for_status()
    return float(response.json()['price'])

@st.cache_data(ttl=15, show_spinner=False)
def candles(symbol):
    response = requests.get('https://api.binance.com/api/v3/klines', params={'symbol':symbol,'interval':'1m','limit':80}, timeout=5)
    response.raise_for_status()
    rows = response.json()
    df = pd.DataFrame(rows)
    df['close'] = pd.to_numeric(df[4]); df['volume'] = pd.to_numeric(df[5])
    return df

def signal(df):
    # Only completed candles: never act on the still-forming candle.
    d = df.iloc[:-1].copy()
    close = d['close']; vol = d['volume']
    fast = close.ewm(span=9, adjust=False).mean()
    slow = close.ewm(span=21, adjust=False).mean()
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = 100 - 100/(1 + up/(down + 1e-12))
    volume_ok = bool(vol.iloc[-1] > vol.iloc[-21:-1].mean())
    direction = 'HOLD'
    if volume_ok and fast.iloc[-1] > slow.iloc[-1] and 52 < rsi.iloc[-1] < 72:
        direction = 'LONG'
    elif volume_ok and fast.iloc[-1] < slow.iloc[-1] and 28 < rsi.iloc[-1] < 48:
        direction = 'SHORT'
    return direction, round(float(rsi.iloc[-1]),1), volume_ok, int(d.iloc[-1,0])

def equity(prices):
    p = st.session_state.position
    if not p:
        return st.session_state.cash
    current = prices.get(p['coin'],p['entry'])
    change = (current/p['entry']-1) * (1 if p['side']=='LONG' else -1)
    return st.session_state.cash + p['margin'] + p['notional']*change

def open_position(coin, side, price, amount, lev, fee, reason):
    if st.session_state.position is not None:
        return
    notional = amount*lev
    entry_fee = notional*fee/100
    if amount+entry_fee > st.session_state.cash:
        return
    st.session_state.cash -= amount+entry_fee
    st.session_state.position = dict(coin=coin,side=side,entry=price,margin=amount,notional=notional,entry_fee=entry_fee,opened=time.time(),reason=reason)
    st.session_state.trades.insert(0,dict(time=datetime.now(timezone.utc).isoformat(timespec='seconds'),coin=coin,action='OPEN '+side,price=round(price,5),pnl=round(-entry_fee,4),reason=reason))

def close_position(price, fee, reason):
    p = st.session_state.position
    if p is None:
        return
    move = (price/p['entry']-1)*(1 if p['side']=='LONG' else -1)
    exit_fee = p['notional']*fee/100
    pnl = p['notional']*move-p['entry_fee']-exit_fee
    st.session_state.cash += p['margin'] + p['notional']*move-exit_fee
    st.session_state.trades.insert(0,dict(time=datetime.now(timezone.utc).isoformat(timespec='seconds'),coin=p['coin'],action='CLOSE '+p['side'],price=round(price,5),pnl=round(pnl,4),reason=reason))
    st.session_state.position = None

@st.fragment(run_every='1s')
def live():
    st.session_state.checks += 1
    st.session_state.last_check = datetime.now().strftime('%H:%M:%S')
    prices = {}; signals = []; errors = []
    for coin in ('BTC','SOL','ETH'):
        try:
            price = ticker(coin+'USDT')
            df = candles(coin+'USDT')
            side, rsi, vol_ok, candle_id = signal(df)
            prices[coin] = price
            signals.append(dict(coin=coin,price=round(price,4),rsi=rsi,volume_ok=vol_ok,signal=side,candle_id=candle_id))
        except Exception as exc:
            errors.append(f'{coin}: {type(exc).__name__}: {exc}')
    if errors:
        st.warning('Marktdaten teilweise nicht verfügbar. Keine neuen Orders bei fehlenden Daten. ' + ' | '.join(errors))
    current_equity = equity(prices)
    st.metric('Geschätztes Guthaben (USDT, näherungsweise €)',f'{current_equity:.2f}',delta=f'{current_equity-100:+.2f}')
    st.caption('Kurse sind USDT, nicht EUR. Währungsumrechnung fehlt. Werte nur für technischen Paper-Test.')
    st.write(f'Automatik: {"AN" if enabled else "AUS"} · Prüfungen: {st.session_state.checks} · Letzte Prüfung: {st.session_state.last_check}')
    if signals:
        st.dataframe(pd.DataFrame(signals).drop(columns='candle_id'),hide_index=True,use_container_width=True)
    p = st.session_state.position
    if enabled and p and p['coin'] in prices:
        current = prices[p['coin']]
        signed_move = (current/p['entry']-1)*(1 if p['side']=='LONG' else -1)*100
        if signed_move <= -stop_pct:
            close_position(current,fee_pct,'Stop-Loss (polling; no guaranteed fill)')
        elif signed_move >= take_pct:
            close_position(current,fee_pct,'Take-Profit')
        elif p['reason']=='TECH_TEST' and time.time()-p['opened']>=30:
            close_position(current,fee_pct,'Testtrade nach 30 Sekunden geschlossen')
    if enabled and equity(prices) <= st.session_state.day_start-2:
        st.error('Tagesverlustlimit von 2 Einheiten erreicht: keine neuen Positionen.')
    elif enabled and st.session_state.position is None:
        if test_mode and not st.session_state.test_started and 'BTC' in prices:
            open_position('BTC','LONG',prices['BTC'],min(1.0,margin),1,fee_pct,'TECH_TEST')
            st.session_state.test_started = True
        elif not test_mode:
            for s in signals:
                if s['signal']=='HOLD':
                    continue
                if st.session_state.last_signal_candle.get(s['coin']) == s['candle_id']:
                    continue
                open_position(s['coin'],s['signal'],prices[s['coin']],margin,leverage,fee_pct,'EMA/RSI/VOLUME')
                st.session_state.last_signal_candle[s['coin']] = s['candle_id']
                break
    st.subheader('Offene Position')
    if st.session_state.position:
        st.json(st.session_state.position)
    else:
        st.write('Keine')
    st.subheader('Handelsprotokoll')
    if st.session_state.trades:
        st.dataframe(pd.DataFrame(st.session_state.trades),hide_index=True,use_container_width=True)
    else:
        st.write('Noch keine Trades')
    st.caption('Keine persistente Speicherung. Keine Liquidations-, Funding- oder Slippage-Modellierung. Streamlit und API können das Intervall verzögern.')

live()
