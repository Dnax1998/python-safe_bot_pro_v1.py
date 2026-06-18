import time
import requests
import json
import threading
import os
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Importy potrzebne do integracji z siecią Polygon i Polymarket CLOB API
try:
    from eth_account import Account
    from web3 import Web3
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    from py_clob_client.clob_types import ApiKeys, OrderArgs
    HAS_LIVE_LIBS = True
except ImportError:
    HAS_LIVE_LIBS = False

# =====================================================================
#  BEZPIECZNA KONFIGURACJA ZMIENNYCH ŚRODOWISKOWYCH (Render Env)
# =====================================================================
LIVE_TRADING = os.environ.get("LIVE_TRADING", "False").lower() == "true"

# Pobieranie kluczy z panelu Render (bezpieczne przed wyciekiem na GitHub)
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "")
POLY_ADDRESS = os.environ.get("POLY_ADDRESS", "")
POLY_API_KEY = os.environ.get("POLY_API_KEY", "")
POLY_API_SECRET = os.environ.get("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.environ.get("POLY_API_PASSPHRASE", "")

# Zarządzanie ryzykiem i pozycją
USE_DYNAMIC_RISK = os.environ.get("USE_DYNAMIC_RISK", "True").lower() == "true"
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "2.0"))
FIXED_TRADE_AMOUNT = float(os.environ.get("FIXED_TRADE_AMOUNT", "20.0"))

ENABLE_EARLY_EXIT = os.environ.get("ENABLE_EARLY_EXIT", "True").lower() == "true"
STOP_LOSS_PRICE = float(os.environ.get("STOP_LOSS_PRICE", "0.35"))
TAKE_PROFIT_PRICE = float(os.environ.get("TAKE_PROFIT_PRICE", "0.90"))

PRICE_MARGIN = 15.0          # Wymagany dystans BTC od SMA (w USD)
STRIKE_MARGIN = 10.0         # Wymagany dystans BTC od ceny Strike (w USD)
# =====================================================================

# --- GLOBALNY STAN BOTA ---
bot_state = {
    "live_mode": LIVE_TRADING,
    "virtual_balance": 1000.0,      # Używane w trybie Paper Trading
    "real_balance": 0.0,            # Pobierane na żywo z portfela Polygon
    "current_price": 0.0,           # Aktualna cena BTC (Coinbase/Binance)
    "sma": 0.0,                     # Średnia krocząca (SMA)
    "minutes_left": 0,              # Minuty do końca świecy
    "seconds_remain": 0,            # Sekundy do końca świecy
    "current_candle_strike": 0.0,   # Cena początkowa świecy 15m (Strike)
    "active_trade": None,           # Szczegóły aktywnej pozycji
    "trade_history": [],            # Historia rozliczonych transakcji
    "logs": []                      # Logi bota wyświetlane w konsoli panelu
}

price_history = []
state_lock = threading.RLock()
clob_client = None

def add_log(message):
    """Zapisuje log w konsoli z dokładnym czasem UTC"""
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 50:
            bot_state["logs"].pop(0)

# --- WYKONYWANIE ZAPYTAŃ Z EXPONENTIAL BACKOFF (Zgodnie z wymaganiami systemu) ---
def safe_api_request(url, method="GET", payload=None, headers=None, max_retries=5):
    """Wykonuje zapytanie HTTP z mechanizmem wykładniczego opóźnienia w razie błędu sieci"""
    delay = 1
    for attempt in range(max_retries):
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=5)
            else:
                response = requests.post(url, json=payload, headers=headers, timeout=5)
                
            if response.status_code == 200:
                return response.json()
        except Exception:
            pass
        
        if attempt < max_retries - 1:
            time.sleep(delay)
            delay *= 2  # Wykładnicze opóźnienie: 1s, 2s, 4s, 8s, 16s
            
    return None

def init_polymarket_client():
    """Inicjalizuje oficjalnego klienta Polymarket CLOB API jeśli dane są podane"""
    global clob_client
    if not LIVE_TRADING:
        add_log("⚙️ Bot działa w bezpiecznym trybie Paper Trading (Symulacja).")
        return False

    if not HAS_LIVE_LIBS:
        add_log("🚨 Błąd: Brak wymaganych bibliotek (web3, eth-account, py-clob-client).")
        return False

    if not all([POLY_PRIVATE_KEY, POLY_ADDRESS, POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE]):
        add_log("🚨 Błąd: Brak skonfigurowanych zmiennych środowiskowych na Renderze dla trybu LIVE!")
        return False

    try:
        # Inicjalizacja klienta CLOB Polymarket na sieci Polygon
        clob_client = ClobClient(
            host="https://clob.polymarket.com",
            key=POLY_PRIVATE_KEY,
            chain_id=POLYGON,
            api_keys=ApiKeys(
                key=POLY_API_KEY,
                secret=POLY_API_SECRET,
                passphrase=POLY_API_PASSPHRASE
            )
        )
        add_log("🔐 [LIVE] Autoryzacja z Polymarket CLOB API zakończona sukcesem!")
        update_real_balance()
        return True
    except Exception as e:
        add_log(f"🚨 [LIVE] Błąd połączenia z API Polymarket: {e}")
        return False

def update_real_balance():
    """Pobiera realny stan konta USDC z portfela Polymarket"""
    global clob_client
    if not clob_client:
        return
    try:
        # Pobieranie salda portfela
        balance_info = clob_client.get_balance()
        with state_lock:
            bot_state["real_balance"] = float(balance_info.get("balance", 0.0))
    except Exception as e:
        print(f"Błąd pobierania salda portfela Polygon: {e}")

# --- DYNAMICZNE WYSZUKIWANIE AKTUALNEGO RYNKU 15M ---
def get_active_polymarket_15m_tokens():
    """Wyszukuje aktywny rynek BTC 15m na Polymarket i zwraca Token ID dla UP i DOWN"""
    url = "https://gamma-api.polymarket.com/markets?active=true&limit=50&query=Bitcoin%20Price%20Interval"
    data = safe_api_request(url)
    if not data:
        return None

    now = datetime.utcnow()
    # Szukamy rynku 15-minutowego, który kończy się w bieżącym kwadransie
    for market in data:
        title = market.get("title", "")
        # Sprawdzamy czy tytuł pasuje do rynków czasowych BTC (np. "Bitcoin Price at 10:15 PM")
        if "Bitcoin Price at" in title and market.get("active") is True:
            clob_token_ids = market.get("clobTokenIds")
            if clob_token_ids:
                try:
                    tokens = json.loads(clob_token_ids)
                    # Zwraca słownik z potrzebnymi danymi rynkowymi
                    return {
                        "market_title": title,
                        "yes_token": tokens[0],  # UP (YES)
                        "no_token": tokens[1],   # DOWN (NO)
                        "strike_price": float(market.get("strike", 0.0))
                    }
                except Exception:
                    continue
    return None

def get_btc_price():
    """Bezpieczne pobieranie ceny BTC z obsługą fallbacków (Binance -> Coinbase -> Kraken)"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    # Metoda 1: Binance
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return float(response.json()['price'])
    except Exception:
        pass

    # Metoda 2: Coinbase (Fallback)
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return float(response.json()['data']['amount'])
    except Exception:
        pass

    # Metoda 3: Kraken (Fallback)
    try:
        url = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            result = response.json().get('result', {})
            pair_key = list(result.keys())[0] if result else None
            if pair_key:
                return float(result[pair_key]['c'][0])
    except Exception:
        pass

    return None

def update_candle_logic(current_price):
    """Zarządza logiką 15-minutowych rynków oraz rozliczaniem pozycji na koniec świecy"""
    global price_history
    now = datetime.utcnow()
    
    # Obliczanie czasu do końca świecy 15m
    minutes_passed = now.minute % 15
    seconds_passed = now.second
    total_seconds_left = (15 * 60) - (minutes_passed * 60 + seconds_passed)
    
    with state_lock:
        bot_state["minutes_left"] = total_seconds_left // 60
        bot_state["seconds_remain"] = total_seconds_left % 60
        bot_state["current_price"] = current_price

        # Aktualizacja historii średniej SMA
        price_history.append(current_price)
        if len(price_history) > 30:
            price_history.pop(0)
        bot_state["sma"] = sum(price_history) / len(price_history)

        # Pobieranie parametrów aktywnego rynku z API Polymarket
        market_info = get_active_polymarket_15m_tokens()
        if market_info:
            bot_state["current_candle_strike"] = market_info["strike_price"]
        else:
            # Rejestracja ceny startowej w trybie Paper Trading
            if minutes_passed == 0 and seconds_passed < 10:
                if bot_state["current_candle_strike"] != current_price:
                    bot_state["current_candle_strike"] = current_price
                    add_log(f"🆕 Nowa świeca 15m (Paper). Strike: ${current_price:,.2f}")

        # ROZSTRZYGNIĘCIE TRANSAKCJI NA KOŃCU ŚWIECY (ostatnie 5 sekund świecy)
        if minutes_passed == 14 and seconds_passed >= 55:
            if bot_state["active_trade"]:
                trade = bot_state["active_trade"]
                
                # W trybie LIVE transakcje na koniec świecy rozlicza blockchain Polymarket
                if LIVE_TRADING:
                    add_log(f"🔔 [LIVE] Rozstrzygnięcie rynkowe dla transakcji {trade['direction']}. Aktualizacja salda portfela...")
                    update_real_balance()
                    trade["status"] = "ZAKOŃCZONA"
                    trade["exit_price"] = current_price
                    trade["closed_at"] = now.strftime("%H:%M:%S")
                    bot_state["trade_history"].append(trade)
                    bot_state["active_trade"] = None
                else:
                    # Rozliczenie w trybie Paper Trading
                    strike = trade["strike_price"]
                    final_price = current_price
                    direction = trade["direction"]
                    
                    won = False
                    if direction == "UP" and final_price > strike:
                        won = True
                    elif direction == "DOWN" and final_price < strike:
                        won = True
                    
                    cost = trade["entry_price"] * trade["amount_shares"]
                    if won:
                        payout = 1.0 * trade["amount_shares"]
                        profit = payout - cost
                        bot_state["virtual_balance"] += payout
                        status = "WYGRANA"
                        add_log(f"🎉 Sukces! Transakcja {direction} na koniec świecy zyskowna. Zysk: +${profit:.2f} USDC")
                    else:
                        profit = -cost
                        status = "PRZEGRANA"
                        add_log(f"📉 Porażka. Transakcja {direction} stratna. Strata: -${cost:.2f} USDC")
                    
                    trade["exit_price"] = final_price
                    trade["status"] = status
                    trade["profit"] = profit
                    trade["closed_at"] = now.strftime("%H:%M:%S")
                    
                    bot_state["trade_history"].append(trade)
                    bot_state["active_trade"] = None

def run_trading_strategy():
    """Główna pętla handlowa bota oparta o timing i trend Krajekisa"""
    add_log("System analizy rynkowej uruchomiony pomyślnie.")
    
    # Próba autoryzacji z Polymarket na starcie
    init_polymarket_client()

    init_price = get_btc_price()
    if init_price:
        with state_lock:
            bot_state["current_candle_strike"] = init_price
            bot_state["current_price"] = init_price
        add_log(f"🟢 Połączono z serwerem cenowym! Początkowe BTC: ${init_price:,.2f}")
    else:
        add_log("⚠️ Brak połączenia z giełdami przy starcie. Bot podejmie próbę za chwilę...")

    error_count = 0
    while True:
        try:
            current_price = get_btc_price()
            if not current_price:
                error_count += 1
                if error_count % 6 == 0:
                    add_log("❌ Problem z pobraniem ceny BTC z giełd. Sprawdź status sieci...")
                time.sleep(5)
                continue
            
            error_count = 0
            update_candle_logic(current_price)
            
            with state_lock:
                m_left = bot_state["minutes_left"]
                active = bot_state["active_trade"]
                sma = bot_state["sma"]
                strike = bot_state["current_candle_strike"]
                balance = bot_state["real_balance"] if LIVE_TRADING else bot_state["virtual_balance"]

            # -----------------------------------------------------------------
            # AKTYWNE MONITOROWANIE I ZAMYKANIE POZYCJI W TRAKCIE ŚWIECY (SIGMOIDA)
            # -----------------------------------------------------------------
            if active and ENABLE_EARLY_EXIT:
                price_diff = current_price - active["strike_price"]
                volatility_denominator = 5.0 + (m_left * 2.0)
                
                try:
                    if active["direction"] == "UP":
                        sim_share_price = 1.0 / (1.0 + math.exp(-price_diff / volatility_denominator))
                    else: # DOWN
                        sim_share_price = 1.0 / (1.0 + math.exp(price_diff / volatility_denominator))
                    
                    sim_share_price = min(0.98, max(0.02, sim_share_price))
                except OverflowError:
                    sim_share_price = 0.98 if price_diff > 0 else 0.02

                # A. STOP-LOSS
                if sim_share_price <= STOP_LOSS_PRICE:
                    recovered_amount = active["amount_shares"] * sim_share_price
                    loss = recovered_amount - active["cost"]
                    
                    if LIVE_TRADING and clob_client:
                        try:
                            # Sprzedaż udziałów na prawdziwym rynku za pomocą zlecenia rynkowego
                            token_to_sell = active["token_id"]
                            # Wysłanie transakcji sprzedaży na Polymarket
                            order_args = OrderArgs(
                                price=sim_share_price,
                                size=active["amount_shares"],
                                side="SELL",
                                token_id=token_to_sell
                            )
                            # Podpisywanie i wysyłanie transakcji na blockchain Polygon
                            signed_order = clob_client.create_order(order_args)
                            clob_client.post_order(signed_order)
                            update_real_balance()
                        except Exception as e:
                            add_log(f"🚨 [LIVE] Błąd wysyłania Stop-Lossa na Polymarket: {e}")

                    with state_lock:
                        if not LIVE_TRADING:
                            bot_state["virtual_balance"] += recovered_amount
                        active["status"] = "STOP LOSS"
                        active["profit"] = loss
                        active["exit_price"] = current_price
                        active["closed_at"] = datetime.utcnow().strftime("%H:%M:%S")
                        bot_state["trade_history"].append(active)
                        bot_state["active_trade"] = None
                    add_log(f"🛡️ [STOP LOSS] Uruchomiono ucieczkę z pozycji {active['direction']}. Odzyskano: {recovered_amount:.2f} USDC (Strata ograniczona do: {loss:.2f} USDC)")
                    time.sleep(5)
                    continue

                # B. TAKE-PROFIT
                elif sim_share_price >= TAKE_PROFIT_PRICE:
                    secured_amount = active["amount_shares"] * sim_share_price
                    profit = secured_amount - active["cost"]
                    
                    if LIVE_TRADING and clob_client:
                        try:
                            token_to_sell = active["token_id"]
                            order_args = OrderArgs(
                                price=sim_share_price,
                                size=active["amount_shares"],
                                side="SELL",
                                token_id=token_to_sell
                            )
                            signed_order = clob_client.create_order(order_args)
                            clob_client.post_order(signed_order)
                            update_real_balance()
                        except Exception as e:
                            add_log(f"🚨 [LIVE] Błąd wysyłania Take-Profita na Polymarket: {e}")

                    with state_lock:
                        if not LIVE_TRADING:
                            bot_state["virtual_balance"] += secured_amount
                        active["status"] = "TAKE PROFIT"
                        active["profit"] = profit
                        active["exit_price"] = current_price
                        active["closed_at"] = datetime.utcnow().strftime("%H:%M:%S")
                        bot_state["trade_history"].append(active)
                        bot_state["active_trade"] = None
                    add_log(f"💰 [TAKE PROFIT] Zabezpieczono zyski przed czasem! Kierunek {active['direction']}. Zysk: +{profit:.2f} USDC!")
                    time.sleep(5)
                    continue

            # -----------------------------------------------------------------
            # OTWIERANIE NOWYCH POZYCJI (Zasada Krajekisa)
            # -----------------------------------------------------------------
            if 5 <= m_left <= 10 and not active and sma > 0 and strike > 0:
                price_diff = current_price - strike
                
                # Wyliczanie stawki inwestycji
                if USE_DYNAMIC_RISK:
                    investment = (balance * RISK_PERCENT) / 100.0
                    investment = min(balance, max(2.0, investment))
                else:
                    investment = min(balance, FIXED_TRADE_AMOUNT)

                # Bezpiecznik: nie otwieramy pozycji jeśli mamy za małe saldo
                if investment < 2.0:
                    time.sleep(10)
                    continue

                # Pobieranie aktualnych Token ID z API
                market_info = get_active_polymarket_15m_tokens()
                token_to_buy = None
                direction = None
                
                # Scenariusz 1: Trend wzrostowy
                if current_price > sma + PRICE_MARGIN and price_diff > STRIKE_MARGIN:
                    direction = "UP"
                    if market_info:
                        token_to_buy = market_info["yes_token"]
                        strike = market_info["strike_price"]

                # Scenariusz 2: Trend spadkowy
                elif current_price < sma - PRICE_MARGIN and price_diff < -STRIKE_MARGIN:
                    direction = "DOWN"
                    if market_info:
                        token_to_buy = market_info["no_token"]
                        strike = market_info["strike_price"]

                if direction:
                    share_price = min(0.90, max(0.55, 0.50 + (abs(price_diff) / 100)))
                    shares = investment / share_price

                    if LIVE_TRADING and clob_client and token_to_buy:
                        try:
                            add_log(f"🛒 [LIVE] Wysyłanie zlecenia zakupu {direction} na Polymarket...")
                            # Przygotowanie zlecenia zakupu na blockchainie Polygon
                            order_args = OrderArgs(
                                price=share_price,
                                size=shares,
                                side="BUY",
                                token_id=token_to_buy
                            )
                            signed_order = clob_client.create_order(order_args)
                            clob_client.post_order(signed_order)
                            update_real_balance()
                        except Exception as e:
                            add_log(f"🚨 [LIVE] Nie udało się otworzyć pozycji na giełdzie: {e}")
                            time.sleep(10)
                            continue

                    with state_lock:
                        bot_state["active_trade"] = {
                            "direction": direction,
                            "entry_price": share_price,
                            "strike_price": strike,
                            "btc_at_entry": current_price,
                            "amount_shares": shares,
                            "cost": investment,
                            "token_id": token_to_buy if token_to_buy else "PAPER_TOKEN",
                            "opened_at": datetime.utcnow().strftime("%H:%M:%S")
                        }
                        if not LIVE_TRADING:
                            bot_state["virtual_balance"] -= investment
                            
                    add_log(f"🛒 [OTWARCIE] Zakupiono kontrakt {direction} po cenie ${share_price:.2f}. Koszt: {investment:.2f} USDC.")

        except Exception as e:
            add_log(f"🚨 Nieoczekiwany błąd w pętli handlowej bota: {e}")
            
        time.sleep(5)

# --- WEB PANEL SERWERA ---
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
        
        # Generowanie statusu dla interfejsu (Paper vs Live)
        mode_label = "LIVE TRADING (Prawdziwe Pieniądze)" if LIVE_TRADING else "PAPER TRADING (Symulacja)"
        mode_color = "bg-rose-500/10 border-rose-500/20 text-rose-400" if LIVE_TRADING else "bg-emerald-500/10 border-emerald-500/20 text-emerald-400"
        
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
                
                <!-- NAGŁÓWEK -->
                <div class="flex flex-col md:flex-row md:items-center md:justify-between border-b border-slate-800 pb-6 mb-8 gap-4">
                    <div>
                        <div class="flex items-center gap-3">
                            <span class="flex h-3 w-3 relative">
                                <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                                <span class="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
                            </span>
                            <h1 class="text-2xl font-bold tracking-tight text-white">Krajekis Bot Panel</h1>
                        </div>
                        <p class="text-sm text-slate-400 mt-1">Automatyczny system handlowy dla rynków BTC 15m Polymarket</p>
                    </div>
                    <div class="flex items-center gap-4">
                        <div class="border rounded-xl px-4 py-2 text-xs font-bold uppercase tracking-wider """ + mode_color + """">
                            """ + mode_label + """
                        </div>
                        <div class="bg-slate-900 border border-slate-800 rounded-xl px-5 py-3 flex items-center gap-4">
                            <div>
                                <p class="text-xs text-slate-400 uppercase tracking-wider font-semibold" id="ui-wallet-label">Saldo</p>
                                <p id="ui-balance" class="text-xl font-bold text-emerald-400">$0.00 USDC</p>
                            </div>
                            <div class="p-2 bg-emerald-500/10 rounded-lg">
                                <i class="fa-solid fa-wallet text-emerald-400 text-lg"></i>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- STATYSTYKI GŁÓWNE -->
                <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
                    <!-- CENA BTC -->
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Aktualna cena BTC</p>
                        <p id="ui-price" class="text-2xl font-extrabold mt-2 text-white">Wczytywanie...</p>
                        <p id="ui-sma" class="text-xs text-slate-500 mt-2">Średnia SMA: --</p>
                    </div>
                    <!-- ZEGAREK ŚWIECY -->
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Czas do końca świecy 15m</p>
                        <p id="ui-timer" class="text-2xl font-extrabold mt-2 text-amber-400">Wczytywanie...</p>
                        <div class="w-full bg-slate-800 h-1.5 rounded-full mt-3 overflow-hidden">
                            <div id="ui-progress" class="bg-amber-400 h-1.5 rounded-full" style="width: 0%"></div>
                        </div>
                    </div>
                    <!-- CENA STRIKE -->
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Cena Strike (Polymarket)</p>
                        <p id="ui-strike" class="text-2xl font-extrabold mt-2 text-slate-200">Wczytywanie...</p>
                        <p id="ui-diff" class="text-xs mt-2">Różnica: --</p>
                    </div>
                    <!-- SKUTECZNOŚĆ -->
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Skuteczność systemu</p>
                        <p id="ui-stats" class="text-2xl font-extrabold mt-2 text-white">0 / 0 (0%)</p>
                        <p id="ui-profit" class="text-xs mt-2 text-emerald-400">Wynik: $0.00 USDC</p>
                    </div>
                </div>

                <!-- AKTYWNA TRANSAKCJA -->
                <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 mb-8 shadow-xl">
                    <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                        <i class="fa-solid fa-chart-line text-indigo-400"></i> Aktywna Pozycja (Polymarket)
                    </h2>
                    <div id="ui-active-box" class="text-slate-400 py-4 text-center">
                        Brak aktywnej pozycji. Bot monitoruje rynek.
                    </div>
                </div>

                <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
                    <!-- KONSOLA NA ŻYWO -->
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl flex flex-col h-[400px]">
                        <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                            <i class="fa-solid fa-terminal text-emerald-400"></i> Konsola Bota na żywo
                        </h2>
                        <div id="ui-logs" class="bg-slate-950 p-4 rounded-xl font-mono text-xs text-emerald-400/90 overflow-y-auto flex-1 space-y-1.5 border border-slate-800/40">
                            Łączenie z serwerem logów bota...
                        </div>
                    </div>

                    <!-- HISTORIA TRANSAKCJI -->
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl flex flex-col h-[400px]">
                        <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                            <i class="fa-solid fa-history text-indigo-400"></i> Ostatnie pozycje
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
                                        <td colspan="4" class="py-6 text-center text-slate-500">Brak rozliczonych pozycji.</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>

            </div>

            <!-- SKRYPT AKTUALIZACJI DASHBOARDU -->
            <script>
                async function updateDashboard() {
                    try {
                        const res = await fetch('/api/status');
                        const data = await res.json();

                        const isLive = data.live_mode;
                        document.getElementById('ui-wallet-label').innerText = isLive ? "Portfel Polygon (LIVE)" : "Saldo (Paper)";
                        const balanceValue = isLive ? data.real_balance : data.virtual_balance;
                        document.getElementById('ui-balance').innerText = '$' + balanceValue.toFixed(2) + ' USDC';
                        
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
                            document.getElementById('ui-strike').innerText = 'Czekam na nową świecę...';
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
                            activeBox.innerHTML = `<p class="text-slate-500 py-2">Brak otwartej pozycji. Bot czeka na optymalne warunki.</p>`;
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
                                if (t.status === "WYGRANA" || t.status === "TAKE PROFIT" || t.status === "ZAKOŃCZONA") totalWins++;
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
    # Start osobnego wątku dla algorytmu tradingu
    bot_thread = threading.Thread(target=run_trading_strategy)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Start wielowątkowego serwera na porcie zdefiniowanym przez Render
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    add_log(f"Wielowątkowy serwer HTTP Dashboard wystartował na porcie {port}")
    server.serve_forever()
