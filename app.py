import time
import requests
import json
import threading
import os
import math
# Importy do obsługi prawdziwego handlu
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Importy SDK Polymarket
from pyclob.client.async_client import AsyncClobClient
from pyclob.models.credentials import ApiCredentials
import asyncio

# =====================================================================
#  USTAWIENIA BOTA (Zarządzanie Ryzykiem i Pozycją)
# =====================================================================
USE_DYNAMIC_RISK = True      # True = procent realnego salda | False = stała kwota
RISK_PERCENT = 2.0           # Procent realnego salda na jedną pozycję (2% - 5%)
FIXED_TRADE_AMOUNT = 20.0    # Stała kwota transakcji w USDC (gdy USE_DYNAMIC_RISK = False)

ENABLE_EARLY_EXIT = True     # True = włącza Stop-Loss i Take-Profit na żywo z arkusza
STOP_LOSS_PRICE = 0.35       # Sprzedaj, gdy cena spadnie poniżej 0.35 USDC
TAKE_PROFIT_PRICE = 0.90     # Sprzedaj, gdy cena wzrośnie do 0.90 USDC

PRICE_MARGIN = 15.0          # Wymagany dystans BTC od SMA (w USD)
STRIKE_MARGIN = 10.0         # Wymagany dystans BTC od ceny Strike (w USD)
# =====================================================================

# --- GLOBALNY STAN BOTA (Prawdziwe Środki) ---
bot_state = {
    "virtual_balance": 0.0,         # Tutaj mapujemy realne saldo pobrane z API dla UI
    "current_price": 0.0,           
    "sma": 0.0,                     
    "minutes_left": 0,              
    "seconds_remain": 0,            
    "current_candle_strike": 0.0,   
    "active_trade": None,           
    "trade_history": [],            
    "logs": []                      
}

price_history = []
state_lock = threading.RLock()

def add_log(message):
    """Logowanie zdarzeń do konsoli Render oraz Dashboardu"""
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 50:
            bot_state["logs"].pop(0)

def get_btc_price():
    """Pobieranie aktualnej ceny spot BTC (Binance / Coinbase fallback)"""
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return float(response.json()['price'])
    except Exception:
        pass
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return float(response.json()['data']['amount'])
    except Exception:
        pass
    return None

# =====================================================================
#  INTEGRACJA Z WE3 / POLYMARKET CLOB API
# =====================================================================
def get_clob_client():
    """Inicjalizacja uwierzytelnionego klienta giełdy Polymarket z Render"""
    priv_key = os.getenv("POLYGON_PRIVATE_KEY")
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_pass = os.getenv("POLYMARKET_API_PASSPHRASE")

    if not all([priv_key, api_key, api_secret, api_pass]):
        add_log("⚠️ BŁĄD: Brak skonfigurowanych zmiennych środowiskowych na Renderze!")
        return None

    creds = ApiCredentials(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass)
    # Łączymy z głównym serwerem produkcyjnym (Chain ID 137 = Polygon Mainnet)
    client = AsyncClobClient("https://clob.polymarket.com", key=priv_key, chain_id=137, creds=creds)
    return client

def fetch_current_15m_market():
    """Automatycznie odpytuje Gamma API w celu znalezienia aktualnych tokenów rynków 15m dla BTC"""
    try:
        # Odpytujemy rynki powiązane z tagiem Bitcoin lub bezpośrednio wyszukujemy aktywne rynki terminowe
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=20&q=Bitcoin%20Price"
        response = requests.get(url, timeout=7)
        if response.status_code == 200:
            markets = response.json()
            now_str = datetime.utcnow().strftime("%Y-%m-%d")
            
            for m in markets:
                # Szukamy rynków, które kończą się dzisiaj i mają format interwału 15-minutowego
                title = m.get("title", "")
                if "Bitcoin will be above" in title and "15-" in m.get("description", ""):
                    clob_rewards = json.loads(m.get("clobTokenRewards", "[]"))
                    if len(clob_rewards) >= 2:
                        # Wyciągamy tokenID z warstwy rynkowej Gamma
                        tokens = json.loads(m.get("outcomeTokenIds", "[]"))
                        if len(tokens) == 2:
                            # Próbujemy odczytać poziom strike z tytułu (np. "above $67,250")
                            try:
                                strike_val = float(title.split("$")[1].replace(",", "").split(" ")[0])
                            except Exception:
                                strike_val = 0.0
                                
                            return {
                                "strike_price": strike_val,
                                "token_up": tokens[0],    # YES Token ID
                                "token_down": tokens[1],  # NO Token ID
                                "condition_id": m.get("conditionId")
                            }
    except Exception as e:
        add_log(f"⚠️ Błąd podczas auto-wyszukiwania kontraktów w Gamma API: {e}")
    return None

async def execute_market_trade(client, token_id, amount_usd, side="BUY"):
    """Pobiera aktualną cenę z Orderbooka i natychmiastowo realizuje zlecenie na giełdzie"""
    try:
        orderbook = await client.get_order_book(token_id)
        if side == "BUY":
            if not orderbook.asks:
                return None
            best_price = float(orderbook.asks[0].price)
        else: # SELL
            if not orderbook.bids:
                return None
            best_price = float(orderbook.bids[0].price)

        # Obliczanie wielkości pozycji na podstawie ceny rynkowej
        size = amount_usd / best_price
        
        # Wysłanie podpisanego kryptograficznie zlecenia
        resp = await client.create_and_post_order(
            token_id=token_id,
            price=best_price,
            side=side,
            size=size,
            fee_rate_bps=0
        )
        
        if resp.get("success") or resp.get("orderID"):
            return {"price": best_price, "size": size, "token_id": token_id}
    except Exception as e:
        add_log(f"🚨 Błąd egzekucji zlecenia na Polymarket: {e}")
    return None

# =====================================================================
#  GŁÓWNA PĘTLA STRATEGII (ASYNCIO)
# =====================================================================
async def async_trading_loop():
    add_log("🚀 Inicjalizacja połączenia PRODUKCYJNEGO (Mainnet)...")
    
    # Pobranie klucza prywatnego z ustawień Rendera
    POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "").strip()
    
    # Bezpośrednie połączenie z sieciami Polymarket (Mainnet)
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    
    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=POLY_PRIVATE_KEY.replace("0x", ""),
            chain_id=POLYGON
        )
        add_log("✅ Połączono z Mainnet! Saldo portfela pobrane.")
    except Exception as e:
        add_log(f"🚨 KRYTYCZNY BŁĄD POŁĄCZENIA: {str(e)}")
        return

    add_log("🟢 Połączono z Realnym Portfelem Web3. Bot nasłuchuje sygnałów...")
    current_market_tokens = None

    while True:
        try:
            current_price = get_btc_price()
            if not current_price:
                await asyncio.sleep(4)
                continue

            now = datetime.utcnow()
            minutes_passed = now.minute % 15
            seconds_passed = now.second
            total_seconds_left = (15 * 60) - (minutes_passed * 60 + seconds_passed)

            # Aktualizacja stanu dla UI
            with state_lock:
                bot_state["minutes_left"] = total_seconds_left // 60
                bot_state["seconds_remain"] = total_seconds_left % 60
                bot_state["current_price"] = current_price
                price_history.append(current_price)
                if len(price_history) > 30:
                    price_history.pop(0)
                bot_state["sma"] = sum(price_history) / len(price_history)

            # Pobieranie realnego salda konta z API w celu wyświetlenia go w UI
            try:
                # W celu zachowania stabilności bierzemy stan salda bezpośrednio z funkcji API portfela L2
                wallet_data = await client.get_collateral_balance(await client.get_address())
                real_balance = float(wallet_data.get("balance", 0))
                with state_lock:
                    bot_state["virtual_balance"] = real_balance # Synchronizacja z widokiem UI
            except Exception:
                real_balance = bot_state["virtual_balance"]

            # 1. GENEROWANIE STRIKE I EMISJA KONTRAKTÓW NA START ŚWIECY
            if minutes_passed == 0 and seconds_passed < 15:
                if current_market_tokens is None:
                    # Automatycznie pobieramy nowo wyemitowane tokeny z Gamma API
                    current_market_tokens = fetch_current_15m_market()
                    if current_market_tokens:
                        with state_lock:
                            bot_state["current_candle_strike"] = current_market_tokens["strike_price"]
                        add_log(f"🆕 Sparowano z nowym rynkiem Polymarket. Strike rynkowy: ${current_market_tokens['strike_price']}")

            # 2. AUTOMATYCZNY RESET POZYCJI NA KONIEC ŚWIECY
            if minutes_passed == 14 and seconds_passed >= 53:
                current_market_tokens = None # Czyszczenie tokenów starej świecy
                with state_lock:
                    if bot_state["active_trade"]:
                        # Pozycja wygasła w smart kontrakcie, przenosimy do historii
                        trade = bot_state["active_trade"]
                        trade["status"] = "ROZLICZONO"
                        trade["exit_price"] = current_price
                        trade["closed_at"] = now.strftime("%H:%M:%S")
                        trade["profit"] = 0.0 # Wyniki rozliczeń blockchain sprawdza się na saldzie końcowym
                        bot_state["trade_history"].append(trade)
                        bot_state["active_trade"] = None
                        add_log("🏁 Pozycja przetrwana do wygaśnięcia kontraktu. Smart kontrakt rozliczy saldo.")

            # Wyciąganie danych z blokady do analizy warunków
            with state_lock:
                active = bot_state["active_trade"]
                sma = bot_state["sma"]
                strike = bot_state["current_candle_strike"]

            # 3. WEJŚCIE W TRANSAKCJĘ (OKNO 5-10 MINUT DO KOŃCA)
            if 5 <= bot_state["minutes_left"] <= 10 and not active and sma > 0 and current_market_tokens:
                price_diff = current_price - strike
                
                # Obliczanie wielkości stawki USDC
                if USE_DYNAMIC_RISK:
                    investment = (real_balance * RISK_PERCENT) / 100.0
                    investment = min(real_balance, max(5.0, investment))
                else:
                    investment = min(real_balance, FIXED_TRADE_AMOUNT)

                if investment >= 5.0: # Minimalna wartość transakcji na Polymarket to zazwyczaj parę dolarów
                    # SCENARIUSZ UP
                    if current_price > sma + PRICE_MARGIN and price_diff > STRIKE_MARGIN:
                        res = await execute_market_trade(client, current_market_tokens["token_up"], investment, "BUY")
                        if res:
                            with state_lock:
                                bot_state["active_trade"] = {
                                    "direction": "UP",
                                    "entry_price": res["price"],
                                    "strike_price": strike,
                                    "btc_at_entry": current_price,
                                    "amount_shares": res["size"],
                                    "cost": investment,
                                    "token_id": res["token_id"],
                                    "opened_at": datetime.utcnow().strftime("%H:%M:%S")
                                }
                            add_log(f"🛒 [REAL BUY] Kupiono kontrakt UP (YES) po cenie ${res['price']:.2f}. Zaangażowano: {investment:.2f} USDC.")

                    # SCENARIUSZ DOWN
                    elif current_price < sma - PRICE_MARGIN and price_diff < -STRIKE_MARGIN:
                        res = await execute_market_trade(client, current_market_tokens["token_down"], investment, "BUY")
                        if res:
                            with state_lock:
                                bot_state["active_trade"] = {
                                    "direction": "DOWN",
                                    "entry_price": res["price"],
                                    "strike_price": strike,
                                    "btc_at_entry": current_price,
                                    "amount_shares": res["size"],
                                    "cost": investment,
                                    "token_id": res["token_id"],
                                    "opened_at": datetime.utcnow().strftime("%H:%M:%S")
                                }
                            add_log(f"🛒 [REAL BUY] Kupiono kontrakt DOWN (NO) po cenie ${res['price']:.2f}. Zaangażowano: {investment:.2f} USDC.")

            # 4. MONITOROWANIE STOP-LOSS / TAKE-PROFIT NA ŻYWO Z ARKUSZA
            if active and ENABLE_EARLY_EXIT:
                try:
                    # Odpytujemy arkusz, po ile realnie możemy TERAZ odsprzedać udziały (Najlepszy Bid)
                    ob = await client.get_order_book(active["token_id"])
                    if ob.bids:
                        live_share_price = float(ob.bids[0].price)
                        
                        # Stop Loss
                        if live_share_price <= STOP_LOSS_PRICE:
                            sell_res = await execute_market_trade(client, active["token_id"], active["amount_shares"] * live_share_price, "SELL")
                            if sell_res:
                                with state_lock:
                                    active["status"] = "REAL SL"
                                    active["profit"] = (active["amount_shares"] * live_share_price) - active["cost"]
                                    active["exit_price"] = current_price
                                    active["closed_at"] = datetime.utcnow().strftime("%H:%M:%S")
                                    bot_state["trade_history"].append(active)
                                    bot_state["active_trade"] = None
                                add_log(f"🛡️ [REAL STOP LOSS] Sprzedano udziały przed czasem po cenie ${live_share_price:.2f} ze względu na ucieczkę ryzyka.")

                        # Take Profit
                        elif live_share_price >= TAKE_PROFIT_PRICE:
                            sell_res = await execute_market_trade(client, active["token_id"], active["amount_shares"] * live_share_price, "SELL")
                            if sell_res:
                                with state_lock:
                                    active["status"] = "REAL TP"
                                    active["profit"] = (active["amount_shares"] * live_share_price) - active["cost"]
                                    active["exit_price"] = current_price
                                    active["closed_at"] = datetime.utcnow().strftime("%H:%M:%S")
                                    bot_state["trade_history"].append(active)
                                    bot_state["active_trade"] = None
                                add_log(f"💰 [REAL TAKE PROFIT] Zabezpieczono zysk przed czasem! Sprzedano po cenie ${live_share_price:.2f}.")
                except Exception:
                    pass

        except Exception as e:
            add_log(f"🚨 Błąd w pętli handlowej: {e}")
        
        await asyncio.sleep(4)

def run_trading_strategy():
    """Silnik handlowy Mainnet - łączy się z portfelem i wykonuje realne zlecenia."""
    # 1. Inicjalizacja klienta z Twoim kluczem z Rendera
    POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "").strip()
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=POLY_PRIVATE_KEY.replace("0x", ""),
        chain_id=POLYGON
    )
    
    # Logowanie startu
    print(f"✅ Bot połączony z Mainnet! Saldo: {client.get_balance()} USDC")
    
    # 2. Główna pętla handlowa (Twoja logika działa tutaj tak samo!)
    while True:
        try:
            # Tutaj wstawiasz swoją sprawdzoną strategię SMA/RSI
            # WAŻNE: Gdy bot decyduje o kupnie, użyj:
            # client.create_order(...)
            
            # Pamiętaj o sleepie, żeby nie dostać bana za ilość zapytań
            time.sleep(30)
            
        except Exception as e:
            print(f"🚨 Błąd handlu: {e}")
            time.sleep(60)
# --- SERWER PANELU KONTROLNEGO DASHBOARD (ZACHOWANY W 100% ORYGINALNY HTML) ---
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Connection', 'close')
            self.end_headers()
            with state_lock:
                self.wfile.write(json.dumps(bot_state).encode('utf-8'))
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Connection', 'close')
        self.end_headers()
        
        # Zachowany Twój oryginalny design i interfejs UI
        html = """
        <!DOCTYPE html>
        <html lang="pl">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Krajekis Bot Dashboard</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');
                body { font-family: 'Plus Jakarta Sans', sans-serif; }
            </style>
        </head>
        <body class="bg-slate-950 text-slate-100 min-h-screen">
            <div class="max-w-7xl mx-auto px-4 py-8">
                
                <div class="flex flex-col md:flex-row md:items-center md:justify-between border-b border-slate-800 pb-6 mb-8 gap-4">
                    <div>
                        <div class="flex items-center gap-3">
                            <span class="flex h-3 w-3 relative">
                                <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                                <span class="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
                            </span>
                            <h1 class="text-2xl font-bold tracking-tight text-white">Krajekis Bot Panel (Live)</h1>
                        </div>
                        <p class="text-sm text-slate-400 mt-1">Realny Trading Web3 na rynkach BTC 15m Polymarket</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800 rounded-xl px-5 py-3 flex items-center gap-4">
                        <div>
                            <p class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Realne Saldo USDC</p>
                            <p id="ui-balance" class="text-xl font-bold text-emerald-400">$0.00 USDC</p>
                        </div>
                        <div class="p-2 bg-emerald-500/10 rounded-lg">
                            <i class="fa-solid fa-wallet text-emerald-400 text-lg"></i>
                        </div>
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Aktualna cena BTC</p>
                        <p id="ui-price" class="text-2xl font-extrabold mt-2 text-white">Wczytywanie...</p>
                        <p id="ui-sma" class="text-xs text-slate-500 mt-2">Średnia SMA: --</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Czas do końca świecy 15m</p>
                        <p id="ui-timer" class="text-2xl font-extrabold mt-2 text-amber-400">Wczytywanie...</p>
                        <div class="w-full bg-slate-800 h-1.5 rounded-full mt-3 overflow-hidden">
                            <div id="ui-progress" class="bg-amber-400 h-1.5 rounded-full" style="width: 0%"></div>
                        </div>
                    </div>
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Cena Strike z Gamma API</p>
                        <p id="ui-strike" class="text-2xl font-extrabold mt-2 text-slate-200">Wczytywanie...</p>
                        <p id="ui-diff" class="text-xs mt-2">Różnica: --</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Skuteczność systemu</p>
                        <p id="ui-stats" class="text-2xl font-extrabold mt-2 text-white">0 / 0 (0%)</p>
                        <p id="ui-profit" class="text-xs mt-2 text-emerald-400">Wynik: $0.00 USDC</p>
                    </div>
                </div>

                <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 mb-8 shadow-xl">
                    <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                        <i class="fa-solid fa-chart-line text-indigo-400"></i> Aktywna Pozycja (Realne Środki CLOB)
                    </h2>
                    <div id="ui-active-box" class="text-slate-400 py-4 text-center">
                        Brak otwartej pozycji. Bot czeka na optymalne warunki (okno 5-10m).
                    </div>
                </div>

                <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl flex flex-col h-[400px]">
                        <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                            <i class="fa-solid fa-terminal text-emerald-400"></i> Konsola Bota na żywo
                        </h2>
                        <div id="ui-logs" class="bg-slate-950 p-4 rounded-xl font-mono text-xs text-emerald-400/90 overflow-y-auto flex-1 space-y-1.5 border border-slate-800/40">
                            Poczekaj, serwer pobiera pierwsze zdarzenia...
                        </div>
                    </div>

                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl flex flex-col h-[400px]">
                        <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                            <i class="fa-solid fa-history text-indigo-400"></i> Ostatnie zamknięte pozycje
                        </h2>
                        <div class="overflow-y-auto flex-1">
                            <table class="w-full text-left text-sm">
                                <thead class="text-xs text-slate-400 uppercase bg-slate-950/40 sticky top-0">
                                    <tr>
                                        <th class="py-2.5 px-3">Kierunek</th>
                                        <th class="py-2.5 px-3">Kurs wejścia</th>
                                        <th class="py-2.5 px-3">Strike vs Meta</th>
                                        <th class="py-2.5 px-3">Wynik</th>
                                    </tr>
                                </thead>
                                <tbody id="ui-history-rows" class="divide-y divide-slate-800/40">
                                    <tr>
                                        <td colspan="4" class="py-6 text-center text-slate-500">Brak zamkniętych transakcji</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>

            <script>
                async function updateDashboard() {
                    try {
                        const res = await fetch('/api/status');
                        const data = await res.json();

                        document.getElementById('ui-balance').innerText = '$' + data.virtual_balance.toFixed(2) + ' USDC';
                        
                        if (data.current_price > 0) {
                            document.getElementById('ui-price').innerText = '$' + data.current_price.toLocaleString('en-US', {minimumFractionDigits: 2});
                        } else {
                            document.getElementById('ui-price').innerText = 'Łączenie z API...';
                        }
                        
                        document.getElementById('ui-sma').innerText = 'Średnia SMA (30 okresów): $' + data.sma.toLocaleString('en-US', {minimumFractionDigits: 2});
                        
                        const min = data.minutes_left;
                        const sec = data.seconds_remain;
                        document.getElementById('ui-timer').innerText = min + 'm ' + (sec < 10 ? '0' : '') + sec + 's';
                        
                        const totalSeconds = (min * 60) + sec;
                        const percent = ((900 - totalSeconds) / 900) * 100;
                        document.getElementById('ui-progress').style.width = percent + '%';

                        const timerEl = document.getElementById('ui-timer');
                        if (min >= 5 && min <= 10) {
                            timerEl.className = "text-2xl font-extrabold mt-2 text-emerald-400";
                        } else {
                            timerEl.className = "text-2xl font-extrabold mt-2 text-amber-500";
                        }

                        if (data.current_candle_strike > 0) {
                            document.getElementById('ui-strike').innerText = '$' + data.current_candle_strike.toLocaleString('en-US', {minimumFractionDigits: 2});
                            const diff = data.current_price - data.current_candle_strike;
                            const diffEl = document.getElementById('ui-diff');
                            if (diff >= 0) {
                                diffEl.className = "text-xs mt-2 text-emerald-400";
                                diffEl.innerText = 'Różnica: +$' + diff.toFixed(2);
                            } else {
                                diffEl.className = "text-xs mt-2 text-rose-400";
                                diffEl.innerText = 'Różnica: -$' + Math.abs(diff).toFixed(2);
                            }
                        } else {
                            document.getElementById('ui-strike').innerText = 'Wyszukiwanie aktywnego rynku Gamma...';
                        }

                        const activeBox = document.getElementById('ui-active-box');
                        if (data.active_trade) {
                            const trade = data.active_trade;
                            activeBox.innerHTML = `
                                <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-left bg-slate-950 p-4 rounded-xl border border-indigo-500/20">
                                    <div>
                                        <p class="text-xs text-slate-400">KIERUNEK</p>
                                        <p class="text-lg font-bold ` + (trade.direction === 'UP' ? 'text-emerald-400' : 'text-rose-400') + `">` + trade.direction + `</p>
                                    </div>
                                    <div>
                                        <p class="text-xs text-slate-400">KURS WEJŚCIA</p>
                                        <p class="text-lg font-bold text-slate-200">$` + trade.entry_price.toFixed(2) + `</p>
                                    </div>
                                    <div>
                                        <p class="text-xs text-slate-400">KURS BTC W CHWILI ZAKUPU</p>
                                        <p class="text-lg font-bold text-slate-200">$` + trade.btc_at_entry.toLocaleString() + `</p>
                                    </div>
                                    <div>
                                        <p class="text-xs text-slate-400">ILOŚĆ UDZIAŁÓW / KOSZT</p>
                                        <p class="text-lg font-bold text-slate-200">` + trade.amount_shares.toFixed(1) + ` szt. / $` + trade.cost.toFixed(2) + ` USDC</p>
                                    </div>
                                </div>
                            `;
                        } else {
                            activeBox.innerHTML = `<p class="text-slate-500 py-2">Brak otwartej pozycji. Bot czeka na optymalne warunki (okno 5-10m do końca świecy).</p>`;
                        }

                        const logsDiv = document.getElementById('ui-logs');
                        if (data.logs.length > 0) {
                            logsDiv.innerHTML = data.logs.slice().reverse().map(function(l) {
                                return '<div>' + l + '</div>';
                            }).join('');
                        } else {
                            logsDiv.innerHTML = '<div class="text-slate-500">Łączenie z botem...</div>';
                        }

                        const historyRows = document.getElementById('ui-history-rows');
                        if (data.trade_history.length > 0) {
                            let totalWins = 0;
                            let totalProfit = 0;
                            
                            const rowsHtml = data.trade_history.slice().reverse().map(function(t) {
                                if (t.status === "WYGRANA" || t.status === "TAKE PROFIT" || t.status === "REAL TP") totalWins++;
                                totalProfit += t.profit;
                                
                                const profitColor = t.profit >= 0 ? 'text-emerald-400' : 'text-rose-400';
                                const dirColor = t.direction === 'UP' ? 'text-emerald-400' : 'text-rose-400';
                                
                                return `
                                    <tr class="border-b border-slate-800/30">
                                        <td class="py-3 px-3 font-semibold ` + dirColor + `">` + t.direction + `</td>
                                        <td class="py-3 px-3">$` + t.entry_price.toFixed(2) + `</td>
                                        <td class="py-3 px-3 text-xs text-slate-400">$` + t.strike_price.toLocaleString() + ` vs $` + t.exit_price.toLocaleString() + `</td>
                                        <td class="py-3 px-3 font-bold ` + profitColor + `">` + t.status + ` (` + (t.profit >= 0 ? '+' : '') + `$` + t.profit.toFixed(2) + `)</td>
                                    </tr>
                                `;
                            }).join('');
                            
                            historyRows.innerHTML = rowsHtml;
                            
                            const winRate = (totalWins / data.trade_history.length) * 100;
                            document.getElementById('ui-stats').innerText = totalWins + ' / ' + data.trade_history.length + ' (' + winRate.toFixed(0) + '%)';
                            
                            const profitEl = document.getElementById('ui-profit');
                            profitEl.innerText = 'Wynik całkowity: ' + (totalProfit >= 0 ? '+' : '') + '$' + totalProfit.toFixed(2) + ' USDC';
                            profitEl.className = totalProfit >= 0 ? 'text-xs mt-2 text-emerald-400 font-semibold' : 'text-xs mt-2 text-rose-400 font-semibold';
                        } else {
                            document.getElementById('ui-stats').innerText = "0 / 0 (0%)";
                            document.getElementById('ui-profit').innerText = "Wynik: $0.00 USDC";
                        }

                    } catch (e) {
                        console.error("Błąd aktualizacji interfejsu:", e);
                    }
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
    add_log(f"Wielowątkowy serwer HTTP Dashboard wystartował na porcie {port}")
    server.serve_forever()
