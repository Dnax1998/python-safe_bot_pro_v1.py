import time
import json
import threading
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

# Globalny stan bota
bot_state = {"virtual_balance": 0.0, "logs": ["Bot wystartował..."]}
state_lock = threading.RLock()
poly_client = None

def run_trading_strategy():
    global poly_client
    # Inicjalizacja klienta
    key = os.environ.get("POLY_PRIVATE_KEY", "").replace("0x", "")
    if key:
        poly_client = ClobClient(host="https://clob.polymarket.com", key=key, chain_id=POLYGON)
    
    while True:
        try:
            if poly_client:
                bal = poly_client.get_collateral_balance()
                val = float(bal.get("balance", 0)) if isinstance(bal, dict) else float(bal)
                with state_lock: bot_state["virtual_balance"] = val
        except Exception as e:
            with state_lock: bot_state["logs"].append(f"Błąd salda: {str(e)}")
        time.sleep(30)

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 1. API Status (dla skryptu JS)
        if self.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            with state_lock:
                self.wfile.write(json.dumps(bot_state).encode('utf-8'))
        # 2. Główna strona Dashboardu
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            # Wklej tutaj CAŁY swój kod HTML z pliku tekst 3.txt lub app.py
            html = """<html><body><h1>Dashboard Bota</h1><p>Bot działa.</p></body></html>"""
            self.wfile.write(html.encode('utf-8'))

if __name__ == "__main__":
    threading.Thread(target=run_trading_strategy, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    server.serve_forever()
