import time
import requests
import json
import threading
import os
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Wstaw tu swój adres portfela (zaczynający się od 0x...)
# Możesz też dodać to jako zmienną środowiskową na Renderze
WALLET_ADDRESS = "TWÓJ_ADRES_0X..." 

bot_state = {
    "virtual_balance": 0.0,
    "logs": []
}

def add_log(msg):
    bot_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    if len(bot_state["logs"]) > 20: bot_state["logs"].pop(0)

def update_real_balance():
    """Pobiera saldo pUSD bezpośrednio przez oficjalny API Polymarket"""
    try:
        # Ten endpoint jest publiczny dla każdego portfela i czyta pUSD
        url = f"https://gamma-api.polymarket.com/balance/{WALLET_ADDRESS}"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            total = 0.0
            for item in data:
                # Szukamy zarówno USDC jak i pUSD
                if item.get("token") in ["pUSD", "USDC"]:
                    total += float(item.get("balance", 0))
            
            bot_state["virtual_balance"] = total
            add_log(f"💰 Odczytano saldo: {total:.2f} USDC")
        else:
            add_log(f"Błąd API: {response.status_code}")
    except Exception as e:
        add_log(f"Błąd sieci: {str(e)}")

def bot_loop():
    while True:
        update_real_balance()
        time.sleep(60)

class Dashboard(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        html = f"""
        <html>
            <body style="background:#0f172a; color:#fff; font-family:sans-serif; padding:20px;">
                <h1>Bot Polymarket Live</h1>
                <div style="font-size:40px; color:#10b981;">{bot_state['virtual_balance']:.2f} USDC</div>
                <h3>Logi:</h3>
                <ul>{''.join([f'<li>{l}</li>' for l in bot_state['logs']])}</ul>
                <script>setTimeout(() => location.reload(), 5000);</script>
            </body>
        </html>
        """
        self.wfile.write(html.encode())

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    ThreadingHTTPServer(('0.0.0.0', port), Dashboard).serve_forever()
