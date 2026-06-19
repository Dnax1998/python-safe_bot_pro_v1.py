import time
import json
import threading
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

# Globalne zmienne
bot_state = {"virtual_balance": 0.0, "logs": ["System startuje..."]}
state_lock = threading.RLock()
poly_client = None

def init_client():
    global poly_client
    key = os.environ.get("POLY_PRIVATE_KEY", "").replace("0x", "")
    if key:
        poly_client = ClobClient(host="https://clob.polymarket.com", key=key, chain_id=POLYGON)
        return True
    return False

def update_loop():
    while True:
        if poly_client:
            try:
                bal = poly_client.get_collateral_balance()
                val = float(bal.get("balance", 0)) if isinstance(bal, dict) else float(bal)
                with state_lock: bot_state["virtual_balance"] = val
            except Exception as e:
                with state_lock: bot_state["logs"].append(f"Błąd: {e}")
        time.sleep(30)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        with state_lock:
            html = f"<html><body><h1>Bot Status</h1><p>Saldo: ${bot_state['virtual_balance']:.2f} USDC</p></body></html>"
        self.wfile.write(html.encode('utf-8'))

if __name__ == "__main__":
    init_client()
    threading.Thread(target=update_loop, daemon=True).start()
    
    # Render wymaga, aby proces nasłuchiwał na porcie zdefiniowanym w ENV
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), SimpleHandler)
    print(f"Server start na porcie {port}")
    server.serve_forever()
