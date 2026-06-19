import time
import json
import threading
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

# Globalne zmienne dla dashboardu
bot_state = {"virtual_balance": 0.0, "logs": ["Bot uruchomiony..."]}
state_lock = threading.RLock()
poly_client = None

def init_mainnet_client():
    global poly_client
    # Pobierz klucz z Environment Variables
    key = os.environ.get("POLY_PRIVATE_KEY", "").replace("0x", "")
    if key:
        try:
            # Kluczowe: Autoryzacja bezpośrednio przez klucz prywatny (MetaMask)
            poly_client = ClobClient(host="https://clob.polymarket.com", key=key, chain_id=POLYGON)
            return True
        except Exception as e:
            print(f"Błąd inicjalizacji: {e}")
    return False

def update_balance_loop():
    """Wątek sprawdzający saldo co 30 sekund"""
    while True:
        if poly_client:
            try:
                bal = poly_client.get_collateral_balance()
                val = float(bal.get("balance", 0)) if isinstance(bal, dict) else float(bal)
                with state_lock:
                    bot_state["virtual_balance"] = val
            except Exception as e:
                print(f"Błąd salda: {e}")
        time.sleep(30)

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Endpoint dla dashboardu
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        # Prosty HTML pokazujący saldo
        html = f"""
        <html>
            <body style="font-family: sans-serif; padding: 20px;">
                <h1>Polymarket Bot Dashboard</h1>
                <div style="padding: 20px; background: #f0f0f0; border-radius: 10px;">
                    <h2>Aktualne Saldo: <span style="color: green;">${{bot_state['virtual_balance']:.2f}} USDC</span></h2>
                </div>
            </body>
        </html>
        """
        self.wfile.write(html.encode('utf-8'))

if __name__ == "__main__":
    if init_mainnet_client():
        threading.Thread(target=update_balance_loop, daemon=True).start()
    
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    print(f"🚀 Bot wystartował na porcie {port}")
    server.serve_forever()
