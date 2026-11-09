
import streamlit as st
import urllib.request
import json
from datetime import datetime

st.set_page_config(
    page_title="AI Trading Bot",
    page_icon="📈",
    layout="wide"
)

st.title("AI TRADING BOT")
st.caption("Version 1.0 | Paper Trading")

if "cash" not in st.session_state:
    st.session_state.cash = 100.0
    st.session_state.positions = {}
    st.session_state.trades = []

def get_prices():
    url = (
        "https://api.kraken.com/0/public/"
        "Ticker?pair=XBTEUR,SOLEUR,ETHEUR"
    )
    with urllib.request.urlopen(
        url, timeout=10
    ) as response:
        data = json.load(response)

    if data.get("error"):
        raise RuntimeError(str(data["error"]))

    result = data["result"]
    prices = {}

    for name, key in [
        ("BTC", "XXBTZEUR"),
        ("SOL", "SOLEUR"),
        ("ETH", "XETHZEUR")
    ]:
        prices[name] = float(result[key]["c"][0])

    return prices

try:
    prices = get_prices()
except Exception as e:
    st.error(f"Kursdatenfehler: {e}")
    st.stop()

cash = st.session_state.cash
positions = st.session_state.positions

portfolio = cash + sum(
    amount * prices[coin]
    for coin, amount in positions.items()
)

st.metric(
    "Portfolio",
    f"{portfolio:.2f} EUR",
    f"{portfolio - 100:.2f} EUR"
)

st.metric("Verfuegbar", f"{cash:.2f} EUR")

st.subheader("Live-Kurse")

for coin, price in prices.items():
    st.metric(coin, f"{price:,.2f} EUR")

st.subheader("Paper Trading")

coin = st.selectbox(
    "Coin auswaehlen",
    list(prices.keys())
)

amount_eur = st.number_input(
    "Betrag in EUR",
    min_value=1.0,
    max_value=100.0,
    value=10.0
)

if st.button("Virtuell kaufen"):
    if amount_eur <= st.session_state.cash:
        quantity = amount_eur / prices[coin]
        st.session_state.cash -= amount_eur
        st.session_state.positions[coin] = (
            st.session_state.positions.get(
                coin, 0
            ) + quantity
        )
        st.session_state.trades.append({
            "Zeit": str(datetime.now()),
            "Coin": coin,
            "Aktion": "BUY",
            "EUR": round(amount_eur, 2)
        })
        st.rerun()
    else:
        st.warning("Nicht genug Guthaben")

if st.button("Position verkaufen"):
    quantity = st.session_state.positions.get(
        coin, 0
    )
    if quantity > 0:
        proceeds = quantity * prices[coin]
        st.session_state.cash += proceeds
        del st.session_state.positions[coin]
        st.session_state.trades.append({
            "Zeit": str(datetime.now()),
            "Coin": coin,
            "Aktion": "SELL",
            "EUR": round(proceeds, 2)
        })
        st.rerun()
    else:
        st.warning("Keine Position vorhanden")

st.subheader("Offene Positionen")
st.write(st.session_state.positions)

st.subheader("Trade-Historie")
st.dataframe(
    st.session_state.trades,
    use_container_width=True
)

if st.button("Simulation zuruecksetzen"):
    st.session_state.cash = 100.0
    st.session_state.positions = {}
    st.session_state.trades = []
    st.rerun()
