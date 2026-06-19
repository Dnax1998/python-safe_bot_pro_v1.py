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

# =====================================================================
#  USTAWIENIA BOTA
# =====================================================================
USE_DYNAMIC_RISK = True
RISK_PERCENT = 2.0
FIXED_TRADE_AMOUNT = 20.0
ENABLE_EARLY_EXIT = True
STOP_LOSS_PRICE = 0.35
TAKE_PROFIT_PRICE = 0.90

bot_state = {
    "virtual_balance": 0.0,
    "active_trade": None,
    "logs": []
}
state_lock = threading.RLock()
poly_client = None

def add_log(message):
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 50: bot_state["logs"].pop(0)

def init_mainnet_client():
    global poly_client
    # Pamiętaj: w Environment Variables klucz musi być BEZ "0x"
    key = os.environ.get("POLY_PRIVATE_KEY", "").replace("0x", "")
    
    if not key:
        add_log("🚨 BŁĄD: Brak POLY_PRIVATE_KEY w środowisku!")
        return None
    
    try:
        # Czysta inicjalizacja dla MetaMask (EOA)
        poly_client = ClobClient(
            host="https://clob.polymarket.com",
            key=key,
            chain_id=POLYGON
        )
        add_log("✅ Klient CLOB zainicjalizowany.")
        return poly_client
    except Exception as e:
        add_log(f"🚨 Błąd inicjalizacji klienta: {e}")
        return None

def update_real_balance():
    global poly_client
    if not poly_client:
        return
    try:
        # Pobieramy saldo z depozytu (collateral)
        balance = poly_client.get_collateral_balance()
        val = float(balance.get("balance", 0)) if isinstance(balance, dict) else float(balance)
        with state_lock:
            bot_state["virtual_balance"] = val
        add_log(f"💰 Aktualne saldo: {val:.2f} USDC")
    except Exception as e:
        add_log(f"⚠️ Błąd pobierania salda: {e}")

def run_trading_strategy():
    """Wątek roboczy bota"""
    init_mainnet_client()
    while True:
        update_real_balance()
        time.sleep(60) # Sprawdzaj saldo co minutę

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        html = "<html><body><h1>Bot Polymarket działa!</h1><p>Sprawdź logi w konsoli Render.</p></body></html>"
        self.wfile.write(html.encode('utf-8'))

if __name__ == "__main__":
    # 1. Start wątku tradingu
    threading.Thread(target=run_trading_strategy, daemon=True).start()
    
    # 2. Start serwera HTTP, który podtrzymuje proces (wymagane przez Render)
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    add_log(f"🚀 Serwer wystartował na porcie {port}")
    server.serve_forever()
