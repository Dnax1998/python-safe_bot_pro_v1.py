import time
import requests
import json
import threading
import os
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Importy z oficjalnej biblioteki Polymarket CLOB v2
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

# =====================================================================
#  USTAWIENIA BOTA REALNEGO (Zarządzanie Ryzykiem i Pozycją)
# =====================================================================
USE_DYNAMIC_RISK = False     # True = bot ryzykuje % prawdziwego salda | False = stała kwota
RISK_PERCENT = 2.0           # Jaki % salda zaryzykować (gdy True)
FIXED_TRADE_AMOUNT = 5.0     # Prawdziwa kwota transakcji w USDC na jeden zakład (Bezpieczne $5 na testy)

PRICE_MARGIN = 15.0          # Wymagany dystans ceny BTC od średniej SMA (w USD)
STRIKE_MARGIN = 10.0         # Wymagany dystans ceny BTC od ceny początkowej świecy
# =====================================================================

# --- STAN GLOBALNY ---
bot_state = {
    "live_balance": 0.0,
    "current_price": 0.0,
    "sma": 0.0,
    "minutes_left": 0,
    "seconds_remain": 0,
    "current_candle_strike": 0.0,
    "active_trade": None,
    "trade_history": [],
    "logs": [],
    "market_name": "Szukanie aktywnego rynku BTC 15m..."
}

price_history = []
state_lock = threading.RLock()
clob_client = None

def add_log(message):
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 50:
            bot_state["logs"].pop(0)

# Inicjalizacja klienta Polymarket
def init_polymarket():
    global clob_client
    pk = os.environ.get("PRIVATE_KEY")
    funder = os.environ.get("FUNDER_ADDRESS")
    
    if not pk or not funder:
        add_log("🚨 BŁĄD: Brak zmiennych PRIVATE_KEY lub FUNDER_ADDRESS na Renderze! Uruchamiam tryb podglądu.")
        return

    try:
        # Konfiguracja klienta w standardzie sieci Polygon (Chain ID 137)
        clob_client = ClobClient(
            host="https://clob.polymarket.com",
            key=pk,
            chain_id=137,
            signature_type=1,  # 1 dla standardowych portfeli EOA (MetaMask)
            funder=funder
        )
        # Automatyczne odnowienie lub wyprowadzenie kluczy sesyjnych L2
        try:
            creds = clob_client.create_or_derive_api_credentials()
        except AttributeError:
            creds = clob_client.create_or_derive_api_key()
            
        clob_client.set_api_creds(creds)
        add_log("✅ Zalogowano do prawdziwego konta Polymarket przez API!")
    except Exception as e:
        add_log(f"🚨 Awaria autoryzacji portfela: {e}")

# Pobieranie realnego salda USDC z konta Polymarket
def update_real_balance():
    global clob_client
    if not clob_client:
        return
    try:
        resp = clob_client.get_balance_allowance()
        if isinstance(resp, dict) and "balance" in resp:
            with state_lock:
                bot_state["live_balance"] = float(resp["balance"])
    except Exception as e:
        pass

# Dynamiczne wyszukiwanie aktualnego Token ID dla zakładów BTC 15m
def get_active_btc_15m_tokens():
    try:
        # Odpytujemy Gamma API Polymarketu o aktywne rynki powiązane z Bitcoinem
        url = "https://gamma-api.polymarket.com/markets?limit=30&active=true&search=Bitcoin"
        res = requests.get(url, timeout=5).json()
        for market in res:
            question = market.get("question", "")
            if "15m" in question.lower() or "15-minute" in question.lower():
                clob_ids = json.loads(market.get("clobTokenIds", "[]"))
                if len(clob_ids) >= 2:
                    return {
                        "yes_id": clob_ids[0],
                        "no_id": clob_ids[1],
                        "question": question,
                        "strike": float(market.get("line", 0)) if market.get("line") else None
                    }
    except Exception as e:
        add_log(f"⚠️ Problem z odpytaniem bazy rynków Polymarket: {e}")
    return None

def get_btc_price():
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        return float(requests.get(url, timeout=5).json()['price'])
    except:
        return None

def run_trading_strategy():
    init_polymarket()
    update_real_balance()
    
    while True:
        try:
            current_price = get_btc_price()
            if not current_price:
                time.sleep(5)
                continue
            
            update_real_balance()
            
            # Logika czasu świecy 15m
            now = datetime.utcnow()
            minutes_passed = now.minute % 15
            seconds_passed = now.second
            total_seconds_left = (15 * 60) - (minutes_passed * 60 + seconds_passed)
            
            with state_lock:
                bot_state["minutes_left"] = total_seconds_left // 60
                bot_state["seconds_remain"] = total_seconds_left % 60
                bot_state["current_price"] = current_price
                price_history.append(current_price)
                if len(price_history) > 30: price_history.pop(0)
                bot_state["sma"] = sum(price_history) / len(price_history)
                
                if minutes_passed == 0 and seconds_passed < 10:
                    bot_state["current_candle_strike"] = current_price
            
            # Skanowanie i bicie rekordów rynkowych
            market_tokens = get_active_btc_15m_tokens()
            if market_tokens:
                with state_lock:
                    bot_state["market_name"] = market_tokens["question"]
                    if market_tokens["strike"]:
                        bot_state["current_candle_strike"] = market_tokens["strike"]
            
            # Sprawdzenie warunków wejścia w pozycję (Okno decyzyjne: 5-10 minut do końca świecy)
            with state_lock:
                m_left = bot_state["minutes_left"]
                active = bot_state["active_trade"]
                sma = bot_state["sma"]
                strike = bot_state["current_candle_strike"]
                balance = bot_state["live_balance"]

            if 5 <= m_left <= 10 and not active and sma > 0 and strike > 0 and market_tokens and clob_client:
                price_diff = current_price - strike
                
                # Ustalenie kwoty transakcji
                if USE_DYNAMIC_RISK:
                    investment = (balance * RISK_PERCENT) / 100.0
                    investment = min(balance, max(2.0, investment))
                else:
                    investment = min(balance, FIXED_TRADE_AMOUNT)

                # Sygnał na WZROST (Kupujemy token YES)
                if current_price > sma + PRICE_MARGIN and price_diff > STRIKE_MARGIN:
                    if investment >= 1.0:
                        add_log(f"🚀 [REALNY TRADE] Wykryto trend UP. Kupuję tokeny YES za {investment:.2f} USDC...")
                        try:
                            order_args = MarketOrderArgs(token_id=market_tokens["yes_id"], amount=investment, side=BUY)
                            resp = clob_client.create_and_post_market_order(order_args)
                            add_log(f"👍 Zlecenie wysłane! ID Odpowiedzi: {resp.get('orderID', 'Brak')}")
                            with state_lock:
                                bot_state["active_trade"] = {"direction": "UP", "cost": investment, "opened_at": now.strftime("%H:%M:%S")}
                        except Exception as e:
                            add_log(f"🚨 Transakcja odrzucona przez Polymarket: {e}")

                # Sygnał na SPADEK (Kupujemy token NO)
                elif current_price < sma - PRICE_MARGIN and price_diff < -STRIKE_MARGIN:
                    if investment >= 1.0:
                        add_log(f"🚀 [REALNY TRADE] Wykryto trend DOWN. Kupuję tokeny NO za {investment:.2f} USDC...")
                        try:
                            order_args = MarketOrderArgs(token_id=market_tokens["no_id"], amount=investment, side=BUY)
                            resp = clob_client.create_and_post_market_order(order_args)
                            add_log(f"👍 Zlecenie wysłane! ID Odpowiedzi: {resp.get('orderID', 'Brak')}")
                            with state_lock:
                                bot_state["active_trade"] = {"direction": "DOWN", "cost": investment, "opened_at": now.strftime("%H:%M:%S")}
                        except Exception as e:
                            add_log(f"🚨 Transakcja odrzucona przez Polymarket: {e}")

            # Reset pozycji na koniec świecy
            if minutes_passed == 14 and seconds_passed >= 55:
                with state_lock:
                    if bot_state["active_trade"]:
                        bot_state["trade_history"].append(bot_state["active_trade"])
                        bot_state["active_trade"] = None
                        add_log("🏁 Świeca zamknięta. Rozliczenie salda nastąpi automatycznie na blockchainie.")

        except Exception as e:
            add_log(f"🚨 Błąd krytyczny pętli strategii: {e}")
        time.sleep(4)

# --- PANEL DASHBOARD LIVE ---
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): return
    def do_GET(self):
        if self.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Connection', 'close')
            self.end_headers()
            with state_lock: self.wfile.write(json.dumps(bot_state).encode('utf-8'))
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Connection', 'close')
        self.end_headers()
        
        html = """
        <!DOCTYPE html>
        <html lang="pl">
        <head>
            <meta charset="UTF-8">
            <title>Krajekis Bot Panel - LIVE MODE</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        </head>
        <body class="bg-slate-950 text-slate-100 min-h-screen font-sans">
            <div class="max-w-7xl mx-auto px-4 py-8">
                <div class="flex flex-col md:flex-row md:items-center md:justify-between border-b border-slate-800 pb-6 mb-8 gap-4">
                    <div>
                        <div class="flex items-center gap-3">
                            <span class="flex h-3 w-3 relative">
                                <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-rose-400 opacity-75"></span>
                                <span class="relative inline-flex rounded-full h-3 w-3 bg-rose-500"></span>
                            </span>
                            <h1 class="text-2xl font-bold text-white">Krajekis Real-Trading Bot</h1>
                            <span class="bg-rose-500/10 text-rose-400 border border-rose-500/20 text-xs font-bold px-2.5 py-1 rounded-full">REAL FUNDS</span>
                        </div>
                        <p id="ui-market-name" class="text-sm text-slate-400 mt-2">Wczytywanie kontraktu z Polymarket...</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800 rounded-xl px-5 py-3 flex items-center gap-4">
                        <div>
                            <p class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Prawdziwe Saldo USDC</p>
                            <p id="ui-balance" class="text-xl font-bold text-emerald-400">$0.00 USDC</p>
                        </div>
                        <div class="p-2 bg-emerald-500/10 rounded-lg"><i class="fa-solid fa-wallet text-emerald-400 text-lg"></i></div>
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
                    <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Aktualna cena BTC</p>
                        <p id="ui-price" class="text-2xl font-extrabold mt-2 text-white">Wczytywanie...</p>
                        <p id="ui-sma" class="text-xs text-slate-500 mt-2">Średnia SMA: --</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Czas do końca świecy 15m</p>
                        <p id="ui-timer" class="text-2xl font-extrabold mt-2 text-amber-400">Wczytywanie...</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Cena Strike Świecy</p>
                        <p id="ui-strike" class="text-2xl font-extrabold mt-2 text-slate-200">Wczytywanie...</p>
                    </div>
                </div>

                <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6 mb-8 shadow-xl">
                    <h2 class="text-lg font-bold mb-4 text-white flex items-center gap-2"><i class="fa-solid fa-shopping-cart text-rose-400"></i> Aktywne zlecenie w grze</h2>
                    <div id="ui-active-box" class="text-slate-500 text-center py-2">Brak otwartej pozycji. Oczekiwanie na sygnał rynkowy.</div>
                </div>

                <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl flex flex-col h-[350px]">
                    <h2 class="text-lg font-bold mb-4 text-white flex items-center gap-2"><i class="fa-solid fa-terminal text-emerald-400"></i> Logi systemowe live</h2>
                    <div id="ui-logs" class="bg-slate-950 p-4 rounded-xl font-mono text-xs text-emerald-400 overflow-y-auto flex-1 space-y-1 border border-slate-800/40"></div>
                </div>
            </div>

            <script>
                async function updateDashboard() {
                    try {
                        const res = await fetch('/api/status');
                        const data = await res.json();
                        document.getElementById('ui-balance').innerText = '$' + data.live_balance.toFixed(2) + ' USDC';
                        document.getElementById('ui-market-name').innerText = data.market_name;
                        if (data.current_price > 0) document.getElementById('ui-price').innerText = '$' + data.current_price.toLocaleString();
                        document.getElementById('ui-sma').innerText = 'Średnia SMA: $' + data.sma.toLocaleString();
                        document.getElementById('ui-timer').innerText = data.minutes_left + 'm ' + (data.seconds_remain < 10 ? '0' : '') + data.seconds_remain + 's';
                        if (data.current_candle_strike > 0) document.getElementById('ui-strike').innerText = '$' + data.current_candle_strike.toLocaleString();

                        const activeBox = document.getElementById('ui-active-box');
                        if (data.active_trade) {
                            activeBox.innerHTML = `<div class="bg-rose-500/10 p-4 border border-rose-500/20 rounded-xl text-left">
                                <p class="text-xs text-slate-400">ZAKŁAD WYKRYTY</p>
                                <p class="text-lg font-bold text-white">Kierunek: ` + data.active_trade.direction + ` | Koszt: $` + data.active_trade.cost + ` USDC</p>
                            </div>`;
                        } else {
                            activeBox.innerHTML = `<p class="text-slate-500">Brak otwartej pozycji. Czekam na okno decyzyjne.</p>`;
                        }

                        document.getElementById('ui-logs').innerHTML = data.logs.slice().reverse().map(l => '<div>' + l + '</div>').join('');
                    } catch (e) {}
                }
                setInterval(updateDashboard, 3000);
                updateDashboard();
            </script>
        </body>
        </html>
        """
        self.wfile.write(html.encode('utf-8'))

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_trading_strategy)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    server.serve_forever()
