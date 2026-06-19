
import time
import requests
import json
import threading
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

# --- ZMIENNE GLOBALNE ---
bot_state = {
    "virtual_balance": 0.0,
    "trade_history": [],
    "logs": []
}
state_lock = threading.RLock()
poly_client = None

# --- LOGIKA BOTA ---
def add_log(message):
    with state_lock:
        bot_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

def init_mainnet_client():
    global poly_client
    key = os.environ.get("POLY_PRIVATE_KEY", "").replace("0x", "")
    if key:
        poly_client = ClobClient(host="https://clob.polymarket.com", key=key, chain_id=POLYGON)
        add_log("✅ Klient CLOB gotowy.")

def update_real_balance():
    global poly_client
    if poly_client:
        try:
            bal = poly_client.get_collateral_balance()
            val = float(bal.get("balance", 0)) if isinstance(bal, dict) else float(bal)
            with state_lock: bot_state["virtual_balance"] = val
        except Exception as e:
            add_log(f"Błąd salda: {e}")

def run_trading_strategy():
    init_mainnet_client()
    while True:
        update_real_balance()
        time.sleep(60)

# --- DASHBOARD (Obsługa strony) ---
class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            with state_lock:
                self.wfile.write(json.dumps(bot_state).encode('utf-8'))
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            # Wstaw tutaj swój kod HTML, który masz w poprzednich wersjach
            html = "<html><body><h1>Dashboard Bota</h1><p>Bot działa poprawnie.</p></body></html>"
            self.wfile.write(html.encode('utf-8'))

# --- START ---
if __name__ == "__main__":
    # Wątek tradingu
    threading.Thread(target=run_trading_strategy, daemon=True).start()
    
    # Serwer podtrzymujący proces (Render go potrzebuje)
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    print(f"🚀 Serwer wystartował na porcie {port}")
    server.serve_forever() # Ten proces nie pozwoli botowi się wyłączyć!
