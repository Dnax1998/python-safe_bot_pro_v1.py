import time
import threading
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

# Wczytujemy tylko klucz prywatny i adres
POLY_ADDRESS = os.environ.get("POLY_ADDRESS", "").strip()
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "").strip()

bot_state = {"logs": ["SYSTEM: Uruchamiam tryb bezpośredni..."], "status": "Czekam..."}
state_lock = threading.RLock()

def add_log(msg):
    with state_lock:
        bot_state["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    print(msg)

def init_direct_client():
    if not POLY_PRIVATE_KEY:
        add_log("🚨 BŁĄD: Brak POLY_PRIVATE_KEY w Environment!")
        return None
    try:
        # Usuwamy 0x jeśli jest
        key = POLY_PRIVATE_KEY.replace("0x", "")
        # Łączymy się bezpośrednio kluczem, bez API Secret
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=key,
            chain_id=POLYGON
        )
        add_log("✅ Połączono bezpośrednio kluczem prywatnym!")
        return client
    except Exception as e:
        add_log(f"🚨 BŁĄD: {str(e)}")
        return None

def run_bot():
    client = init_direct_client()
    while True:
        if client:
            add_log("Monitoruję rynek (Tryb bezpośredni)...")
        time.sleep(60)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        with state_lock:
            self.wfile.write(f"<html><body><h1>Bot Status</h1><pre>{'<br>'.join(bot_state['logs'])}</pre></body></html>".encode())

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    ThreadingHTTPServer(('0.0.0.0', port), SimpleHandler).serve_forever()
