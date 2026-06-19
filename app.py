import time
import requests
import json
import threading
import os
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Importy dla prawdziwego handlu
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import OrderArgs, SignatureType

# =====================================================================
#  USTAWIENIA BOTA
# =====================================================================
USE_DYNAMIC_RISK = True
RISK_PERCENT = 2.0
FIXED_TRADE_AMOUNT = 20.0
ENABLE_EARLY_EXIT = True
STOP_LOSS_PRICE = 0.35
TAKE_PROFIT_PRICE = 0.90
PRICE_MARGIN = 15.0
STRIKE_MARGIN = 10.0

# =====================================================================
#  STAN BOTA
# =====================================================================
bot_state = {
    "virtual_balance": 0.0,
    "current_price": 0.0,
    "sma": 0.0,
    "minutes_left": 0,
    "seconds_remain": 0,
    "current_candle_strike": 0.0,
    "active_trade": None,
    "trade_history": [],
    "logs": []
}

active_market_info = {"token_id_up": None, "token_id_down": None, "title": "Szukam rynków..."}
price_history = []
state_lock = threading.RLock()
poly_client = None

def add_log(message):
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 50: bot_state["logs"].pop(0)

def auto_discover_btc_tokens():
    global active_market_info
    try:
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&q=Bitcoin"
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            for m in r.json():
                title = m.get("title", "")
                tokens = m.get("clobTokenIds")
                if tokens and len(tokens) >= 2 and "above" in title.lower() and "15m" in title.lower():
                    active_market_info.update({"token_id_up": tokens[0], "token_id_down": tokens[1], "title": title})
                    return
    except: pass

def update_real_balance():
    global poly_client
    addr = os.environ.get("POLY_ADDRESS", "")
    if not addr and poly_client: addr = poly_client.get_address()
    if not addr: return
    try:
        res = requests.get(f"https://gamma-api.polymarket.com/balance/{addr}", timeout=10)
        if res.status_code == 200:
            total = sum(float(item.get("balance", 0)) for item in res.json() if item.get("token") in ["pUSD", "USDC"])
            with state_lock: bot_state["virtual_balance"] = total
    except:
        if poly_client:
            try:
                bal = poly_client.get_collateral_balance(addr)
                with state_lock: bot_state["virtual_balance"] = float(bal.get("balance", 0) if isinstance(bal, dict) else bal)
            except: pass

def init_mainnet_client():
    global poly_client
    key = os.environ.get("POLY_PRIVATE_KEY", "").replace("0x", "")
    addr = os.environ.get("POLY_ADDRESS", "")
    if key:
        try:
            kwargs = {"host": "https://clob.polymarket.com", "key": key, "chain_id": POLYGON}
            if addr: kwargs.update({"funder": addr, "signature_type": SignatureType.POLY_GNOSIS_SAFE})
            poly_client = ClobClient(**kwargs)
            update_real_balance()
            add_log(f"✅ Połączono z Mainnet. Saldo: {bot_state['virtual_balance']:.2f} USDC")
        except Exception as e: add_log(f"🚨 Błąd połączenia: {e}")

def get_btc_price():
    for url in ["https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", "https://api.coinbase.com/v2/prices/BTC-USD/spot"]:
        try:
            r = requests.get(url, timeout=5).json()
            return float(r.get('price') or r['data']['amount'])
        except: continue
    return None

def update_candle_logic(current_price):
    global price_history
    now = datetime.utcnow()
    m_p, s_p = now.minute % 15, now.second
    with state_lock:
        bot_state.update({"current_price": current_price, "minutes_left": (14 - m_p), "seconds_remain": 60 - s_p})
        price_history = (price_history + [current_price])[-30:]
        bot_state["sma"] = sum(price_history) / len(price_history)
        if m_p == 0 and s_p < 10: bot_state["current_candle_strike"] = current_price
        
        # Rozliczenie pasywne
        if m_p == 14 and s_p >= 55 and bot_state["active_trade"]:
            trade = bot_state["active_trade"]
            won = (trade["direction"] == "UP" and current_price > trade["strike_price"]) or \
                  (trade["direction"] == "DOWN" and current_price < trade["strike_price"])
            add_log(f"Settlement: {'WYGRANA' if won else 'PRZEGRANA'}")
            bot_state["active_trade"] = None
            update_real_balance()

def run_trading_strategy():
    init_mainnet_client()
    while True:
        auto_discover_btc_tokens()
        update_real_balance()
        price = get_btc_price()
        if price: update_candle_logic(price)
        time.sleep(5)

# --- WEB DASHBOARD (Uprawnienia do DashboardHandler pozostały bez zmian) ---
# [Wstaw tutaj swoją klasę DashboardHandler z pliku app.py]

if __name__ == "__main__":
    threading.Thread(target=run_trading_strategy, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    server.serve_forever()
