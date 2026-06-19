import time
import requests
import json
import threading
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- KONFIGURACJA ---
IS_LIVE = os.environ.get("WALLET_PRIVATE_KEY") is not None
FIXED_TRADE_AMOUNT = 5.0 # Kwota na jeden zakład

# --- STAN BOTA ---
bot_state = {
    "active_trade": None,
    "logs": []
}
state_lock = threading.RLock()

def add_log(message):
    with state_lock:
        bot_state["logs"].append(f"[{datetime.utcnow().strftime('%H:%M:%S')}] {message}")

def execute_order(token_id, amount, side):
    url = "https://clob.polymarket.com/order"
    headers = {"x-api-key": os.environ.get("POLY_API_KEY", ""), "Content-Type": "application/json"}
    payload = {"token_id": token_id, "amount": amount, "side": side, "account": os.environ.get("WALLET_ADDRESS")}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        return res.status_code in [200, 201], res.text
    except Exception as e:
        return False, str(e)

def run_trading_strategy():
    add_log("🚀 BOT AGRESYWNY: Startuję!")
    while True:
        try:
            # 1. Sprawdź czy mamy pozycję
            with state_lock:
                has_active = bot_state["active_trade"] is not None

            if not has_active:
                # 2. Pobierz rynek
                res = requests.get("https://gamma-api.polymarket.com/markets?slug=bitcoin&limit=1", timeout=5).json()
                if res and len(res) > 0:
                    tokens = json.loads(res[0].get("clobTokenIds", "[]"))
                    token_up = tokens[0] # Bierzemy pierwszy lepszy token
                    
                    # 3. Kupuj natychmiast
                    add_log("🔥 Próba kupna...")
                    success, msg = execute_order(token_up, FIXED_TRADE_AMOUNT, "BUY")
                    
                    if success:
                        with state_lock:
                            bot_state["active_trade"] = {"direction": "UP", "token": token_up}
                        add_log("✅ KUPIONO!")
                    else:
                        add_log(f"❌ BŁĄD: {msg}")
            else:
                # Jeśli mamy pozycję, udawajmy że ją "zamykamy" po 20 sekundach żeby kupić znowu
                time.sleep(20)
                with state_lock:
                    bot_state["active_trade"] = None
                    add_log("🔄 Zamknięto pozycję, przygotowanie do kolejnego zakupu.")
            
        except Exception as e:
            add_log(f"🚨 Błąd: {e}")
        time.sleep(2)

# --- SERWER ---
class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        with state_lock:
            self.wfile.write(json.dumps(bot_state).encode())

if __name__ == "__main__":
    threading.Thread(target=run_trading_strategy, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler).serve_forever()
