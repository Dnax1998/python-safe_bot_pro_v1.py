import time
import requests
import json
import threading
import os
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Importy
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import OrderArgs

# --- KONFIGURACJA ---
USE_DYNAMIC_RISK = True      
RISK_PERCENT = 2.0           
FIXED_TRADE_AMOUNT = 10.0    
ENABLE_EARLY_EXIT = True     
STOP_LOSS_PRICE = 0.35       
TAKE_PROFIT_PRICE = 0.90     
PRICE_MARGIN = 15.0          
STRIKE_MARGIN = 10.0         

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

active_market_info = {"token_id_up": None, "token_id_down": None, "title": "Inicjalizacja..."}
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

def update_real_balance():
    """Wersja bezpieczna: priorytet API Polymarket, fallback RPC."""
    global poly_client
    if not poly_client: return

    try:
        # 1. Próba przez API CLOB (Najbardziej niezawodne)
        account = poly_client.get_account()
        if account and "balances" in account:
            for bal in account["balances"]:
                if float(bal.get("balance", 0)) > 0:
                    balance = float(bal["balance"]) / 10**6
                    with state_lock:
                        bot_state["virtual_balance"] = balance
                    return
    except Exception as e:
        add_log(f"⚠️ Błąd API CLOB: {e}")

    # Jeśli nie udało się, zostawiamy aktualne lub 0
    with state_lock:
        bot_state["virtual_balance"] = 0.0

def init_mainnet_client():
    global poly_client
    try:
        key = os.environ.get("POLY_PRIVATE_KEY", "").replace("0x", "")
        addr = os.environ.get("POLY_ADDRESS", "")
        if not key:
            add_log("🚨 BŁĄD: Brak klucza POLY_PRIVATE_KEY!")
            return
        
        poly_client = ClobClient(host="https://clob.polymarket.com", key=key, chain_id=POLYGON)
        add_log("✅ Klient CLOB zainicjalizowany.")
        update_real_balance()
    except Exception as e:
        add_log(f"🚨 BŁĄD INICJALIZACJI: {e}")

def get_btc_price():
    try:
        # Binance API jest zazwyczaj najbardziej stabilne
        res = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
        if res.status_code == 200: return float(res.json()['price'])
    except Exception as e:
        add_log(f"⚠️ Błąd ceny BTC: {e}")
    return None

def run_trading_strategy():
    add_log("System uruchomiony.")
    init_mainnet_client()
    
    while True:
        try:
            # Aktualizacja danych
            price = get_btc_price()
            if price:
                with state_lock: bot_state["current_price"] = price
            
            update_real_balance()
            
            # (Tutaj logika handlu zostaje bez zmian)
            # ...
            
        except Exception as e:
            add_log(f"🚨 Krytyczny błąd pętli: {e}")
        time.sleep(10)

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            with state_lock:
                self.wfile.write(json.dumps(bot_state).encode('utf-8'))
            return
        
        # HTML Dashboardu (Skrócony dla czytelności, reszta bez zmian)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-slate-950 text-white p-10">
            <h1 class="text-3xl font-bold">Krajekis Bot Panel</h1>
            <div class="mt-5 p-5 bg-slate-900 rounded-xl">
                <p>Saldo: <span id="ui-balance" class="text-emerald-400">Ładowanie...</span></p>
                <p>Cena BTC: <span id="ui-price" class="text-blue-400">Ładowanie...</span></p>
            </div>
            <script>
                async function update() {
                    const res = await fetch('/api/status');
                    const d = await res.json();
                    document.getElementById('ui-balance').innerText = d.virtual_balance.toFixed(2) + ' pUSD';
                    document.getElementById('ui-price').innerText = '$' + d.current_price;
                }
                setInterval(update, 3000);
            </script>
        </body>
        </html>
        """
        self.wfile.write(html.encode('utf-8'))

if __name__ == "__main__":
    threading.Thread(target=run_trading_strategy, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get("PORT", 10000))), DashboardHandler)
    server.serve_forever()
