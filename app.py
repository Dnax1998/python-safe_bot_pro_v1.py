import time
import requests
import json
import threading
import os
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from eth_account import Account
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds, OrderArgs

# --- KONFIGURACJA Z RENDER ---
POLY_ADDRESS = os.environ.get("POLY_ADDRESS", "").strip()
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "").strip()
POLY_API_KEY = os.environ.get("POLY_API_KEY", "").strip()
POLY_API_SECRET = os.environ.get("POLY_API_SECRET", "").strip()
POLY_API_PASSPHRASE = os.environ.get("POLY_API_PASSPHRASE", "").strip()

bot_state = {"logs": ["SYSTEM: Czekam na klucze API..."], "active_trade": None, "trade_history": [], "real_balance": 0.0}
state_lock = threading.RLock()

def add_log(msg):
    with state_lock:
        bot_state["logs"].append(f"[{datetime.utcnow().strftime('%H:%M:%S')}] {msg}")
        if len(bot_state["logs"]) > 20: bot_state["logs"].pop(0)
    print(msg)

def init_client():
    if not POLY_API_KEY or not POLY_API_SECRET or not POLY_API_PASSPHRASE:
        add_log("🚨 BŁĄD: BRAK KLUCZY API! Dodaj w Render: POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE")
        return None
    
    try:
        clean_key = POLY_PRIVATE_KEY.replace("0x", "")
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=clean_key,
            chain_id=POLYGON,
            api_keys=ApiCreds(key=POLY_API_KEY, secret=POLY_API_SECRET, passphrase=POLY_API_PASSPHRASE)
        )
        add_log("✅ Pomyślnie wczytano klucze API. Bot aktywny.")
        return client
    except Exception as e:
        add_log(f"🚨 BŁĄD AUTORYZACJI: {str(e)}")
        return None

def run_trading_strategy():
    add_log("Silnik startuje...")
    client = init_client()
    while True:
        if client:
            add_log("Monitoruję rynek (Tryb LIVE)...")
        time.sleep(30)

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        with state_lock:
            logs_html = "<br>".join(bot_state["logs"])
            self.wfile.write(f"<html><body><h1>Bot Status</h1><pre>{logs_html}</pre></body></html>".encode())

if __name__ == "__main__":
    threading.Thread(target=run_trading_strategy, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler).serve_forever()
