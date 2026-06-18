import time
import requests
import json
import threading
import os
import math
import asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Import bibliotek handlowych Polymarket
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

# =====================================================================
#  USTAWIENIA BOTA (MAINNET)
# =====================================================================
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "").strip()

# Parametry handlowe
FIXED_TRADE_AMOUNT = 20.0
BTC_MARKET_ID = "0x4b78635443423730303130303030303030303030303030303030303030303030"

bot_state = {"logs": ["SYSTEM: Uruchamianie Mainnet..."], "balance": 0.0}
state_lock = threading.RLock()

def add_log(msg):
    with state_lock:
        bot_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        if len(bot_state["logs"]) > 30: bot_state["logs"].pop(0)

async def async_trading_loop(client):
    add_log("🚀 Pętla handlowa LIVE wystartowała.")
    while True:
        try:
            # Tu wstawiasz swoją sprawdzoną logikę (SMA/RSI)
            # Przykład użycia klienta:
            # order = client.create_order(...)
            add_log("Analiza rynku... (Tryb LIVE)")
            await asyncio.sleep(30)
        except Exception as e:
            add_log(f"⚠️ Błąd: {str(e)}")
            await asyncio.sleep(60)

def run_trading_strategy():
    """Silnik połączenia z portfelem Mainnet"""
    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=POLY_PRIVATE_KEY.replace("0x", ""),
            chain_id=POLYGON
        )
        balance = client.get_balance()
        bot_state["balance"] = float(balance)
        add_log(f"✅ Połączono! Saldo: {balance} USDC")
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(async_trading_loop(client))
    except Exception as e:
        add_log(f"🚨 BŁĄD MAINNET: {str(e)}")

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        with state_lock:
            logs = "<br>".join(bot_state["logs"])
            html = f"<html><body style='background:#0f172a; color:#fff;'><h1>Bot Pro LIVE</h1><p>Saldo: {bot_state['balance']} USDC</p><pre>{logs}</pre></body></html>"
            self.wfile.write(html.encode())

if __name__ == "__main__":
    threading.Thread(target=run_trading_strategy, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler).serve_forever()
