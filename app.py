import time
import requests
import json
import threading
import os
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Importy wymagane do autoryzacji transakcji na blockchainie Polygon i Polymarket CLOB
from eth_account import Account
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
# Zmiana z ApiKeys na ApiCreds zgodnie z najnowszą wersją biblioteki Polymarketu
from py_clob_client.clob_types import ApiCreds, OrderArgs

# =====================================================================
#  PARAMETRY KONFIGURACYJNE (Wczytywane ze zmiennych Render)
# =====================================================================
# Adres portfela i jego klucz prywatny (potrzebny do podpisywania transakcji)
POLY_ADDRESS = os.environ.get("POLY_ADDRESS", "").strip()
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "").strip()

# Dane uwierzytelniające CLOB API (pobierane z Rendera lub generowane automatycznie)
POLY_API_KEY = os.environ.get("POLY_API_KEY", "").strip()
POLY_API_SECRET = os.environ.get("POLY_API_SECRET", "").strip()
POLY_API_PASSPHRASE = os.environ.get("POLY_API_PASSPHRASE", "").strip()

# Zarządzanie Wielkością Pozycji
USE_DYNAMIC_RISK = os.environ.get("USE_DYNAMIC_RISK", "True").lower() == "true"
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "5.0"))  # Domyślnie 5% całego salda portfela
FIXED_TRADE_AMOUNT = float(os.environ.get("FIXED_TRADE_AMOUNT", "10.0"))  # Stała stawka w USDC

# Automatyczne Zarządzanie Otwartą Pozycją
ENABLE_EARLY_EXIT = True
STOP_LOSS_PRICE = 0.30       # Jeśli cena udziału spadnie do 30 centów, tniemy stratę
TAKE_PROFIT_PRICE = 0.90     # Jeśli cena udziału wzrośnie do 90 centów, bierzemy pewny zysk

PRICE_MARGIN = 15.0          # O ile BTC musi oddalić się od SMA (w USD)
STRIKE_MARGIN = 10.0         # O ile BTC musi oddalić się od ceny Strike (w USD)

# Kontrakt tokenu USDC w sieci Polygon (Native USDC)
USDC_CONTRACT_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_ABI = '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"}]'
# =====================================================================

# --- GLOBALNY STAN BOTA (LIVE) ---
bot_state = {
    "real_balance": 0.0,            # Prawdziwe saldo pobrane z blockchainu Polygon
    "current_price": 0.0,           # Aktualna cena BTC
    "sma": 0.0,                     # Średnia krocząca (SMA)
    "minutes_left": 0,              # Minuty do końca świecy
    "seconds_remain": 0,            # Sekundy do końca świecy
    "current_candle_strike": 0.0,   # Cena początkowa bieżącej świecy 15m (Strike)
    "active_trade": None,           # Szczegóły aktywnej pozycji rynkowej
    "trade_history": [],            # Historia zamkniętych transakcji na żywo
    "logs": []                      # Logi z pracy bota
}

price_history = []
state_lock = threading.RLock()
clob_client = None
w3 = None

def add_log(message):
    """Zapisuje log w konsoli z dokładnym czasem UTC i przesyła go do Dashboardu"""
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 50:
            bot_state["logs"].pop(0)

# --- WYKONYWANIE ZAPYTAŃ Z EXPONENTIAL BACKOFF ---
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
            delay *= 2
            
    return None

# --- INICJALIZACJA DOSTĘPU DO BLOCKCHAINU I API ---
def init_live_connections():
    """Inicjalizuje połączenie z siecią Polygon oraz autoryzuje / generuje klucze CLOB"""
    global clob_client, w3
    
    # Walidacja minimalnej konfiguracji
    missing_vars = []
    if not POLY_ADDRESS: missing_vars.append("POLY_ADDRESS")
    if not POLY_PRIVATE_KEY: missing_vars.append("POLY_PRIVATE_KEY")
    
    if missing_vars:
        add_log(f"🚨 BRAK KONFIGURACJI! Dodaj zmienne w Renderze: {', '.join(missing_vars)}")
        return False

    try:
        # Połączenie z siecią RPC Polygon
        w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        
        # Oczyszczenie klucza prywatnego ze zbędnych znaków (np. 0x na początku)
        clean_key = POLY_PRIVATE_KEY
        if clean_key.startswith("0x"):
            clean_key = clean_key[2:]

        # Jeśli użytkownik podał komplet 3 kluczy API, używamy ich bezpośrednio
        if POLY_API_KEY and POLY_API_SECRET and POLY_API_PASSPHRASE:
            add_log("🔐 Wykryto pełną konfigurację kluczy API w Renderze. Łączenie z Polymarket...")
            clob_client = ClobClient(
                host="https://clob.polymarket.com",
                key=clean_key,
                chain_id=POLYGON,
                api_keys=ApiCreds(
                    key=POLY_API_KEY,
                    secret=POLY_API_SECRET,
                    passphrase=POLY_API_PASSPHRASE
                )
            )
        else:
            # Automatyczne generowanie brakujących kluczy przy użyciu klucza prywatnego
            add_log("⚙️ Wykryto brak kluczy Secret/Passphrase. Rozpoczynam automatyczną generację jednorazowych kluczy CLOB...")
            temp_client = ClobClient(
                host="https://clob.polymarket.com",
                key=clean_key,
                chain_id=POLYGON
            )
            
            # Tworzenie nowych kluczy API na giełdzie
            new_creds = temp_client.create_api_keys()
            
            # Wyświetlenie wygenerowanych kluczy wielkimi literami w logach bota
            add_log("==================================================================")
            add_log("⚠️ ZAPISZ TE KLUCZE I DODAJ JE DO ZMIENNYCH NA RENDERZE, ABY UNIKNĄĆ LIMITÓW:")
            add_log(f"🔑 POLY_API_KEY = {new_creds.key}")
            add_log(f"🔑 POLY_API_SECRET = {new_creds.secret}")
            add_log(f"🔑 POLY_API_PASSPHRASE = {new_creds.passphrase}")
            add_log("==================================================================")
            
            clob_client = ClobClient(
                host="https://clob.polymarket.com",
                key=clean_key,
                chain_id=POLYGON,
                api_keys=new_creds
            )

        add_log("🚀 Pomyślnie połączono i autoryzowano bota bezpośrednio na Polymarket LIVE!")
        update_blockchain_balance()
        return True
    except Exception as e:
        add_log(f"🚨 Błąd autoryzacji z giełdą (Sprawdź poprawność klucza prywatnego): {e}")
        return False

def update_blockchain_balance():
    """Pobiera rzeczywiste saldo USDC bezpośrednio z Twojego adresu w sieci Polygon"""
    global w3
    if not w3 or not POLY_ADDRESS:
        return
    try:
        # Odczyt salda tokenów native USDC za pomocą kontraktu na blockchainie Polygon
        contract = w3.eth.contract(address=Web3.to_checksum_address(POLY_ADDRESS), abi=json.loads(USDC_ABI))
        raw_balance = contract.functions.balanceOf(Web3.to_checksum_address(POLY_ADDRESS)).call()
        usdc_balance = raw_balance / 1000000.0
        with state_lock:
            bot_state["real_balance"] = usdc_balance
    except Exception as e:
        # Fallback na wypadek problemów z zapytaniem kontraktu
        try:
            balance_wei = w3.eth.get_balance(Web3.to_checksum_address(POLY_ADDRESS))
            with state_lock:
                bot_state["real_balance"] = balance_wei / 1e18
        except Exception:
            print(f"Błąd odczytu salda z blockchainu: {e}")

# --- DYNAMICZNE POBIERANIE AKTYWNYCH TOKENÓW BTC 15M ---
def fetch_active_polymarket_15m():
    """Przeszukuje aktywne rynki na Polymarkecie w poszukiwaniu aktualnej świecy BTC 15m"""
    url = "https://gamma-api.polymarket.com/markets?active=true&limit=50&query=Bitcoin%20Price%20Interval"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            markets = response.json()
            for m in markets:
                title = m.get("title", "")
                if "Bitcoin Price at" in title and m.get("active") is True:
                    clob_token_ids = m.get("clobTokenIds")
                    if clob_token_ids:
                        tokens = json.loads(clob_token_ids)
                        return {
                            "title": title,
                            "yes_token": tokens[0],  # Token kierunku UP
                            "no_token": tokens[1],   # Token kierunku DOWN
                            "strike": float(m.get("strike", 0.0))
                        }
    except Exception as e:
        print(f"Błąd wyszukiwania rynku na Gamma API: {e}")
    return None

def get_btc_price():
    """Pobiera cenę spot BTC z najstabilniejszego źródła (Binance/Coinbase)"""
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return float(response.json()['price'])
    except Exception:
        pass
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return float(response.json()['data']['amount'])
    except Exception:
        pass
    return None

def update_candle_logic(current_price):
    """Oblicza czas świecy 15m oraz monitoruje rozliczenie kontraktu na giełdzie"""
    global price_history
    now = datetime.utcnow()
    
    minutes_passed = now.minute % 15
    seconds_passed = now.second
    total_seconds_left = (15 * 60) - (minutes_passed * 60 + seconds_passed)
    
    with state_lock:
        bot_state["minutes_left"] = total_seconds_left // 60
        bot_state["seconds_remain"] = total_seconds_left % 60
        bot_state["current_price"] = current_price

        # Wyliczanie lokalnego trendu SMA
        price_history.append(current_price)
        if len(price_history) > 30:
            price_history.pop(0)
        bot_state["sma"] = sum(price_history) / len(price_history)

        # Dynamiczne przypisywanie ceny Strike z aktywnego rynku Polymarket
        market_info = fetch_active_polymarket_15m()
        if market_info:
            bot_state["current_candle_strike"] = market_info["strike"]

        # Rozliczenie pozycji na koniec świecy (zamknięcie automatyczne przez giełdę)
        if minutes_passed == 14 and seconds_passed >= 55:
            if bot_state["active_trade"]:
                trade = bot_state["active_trade"]
                add_log(f"🔔 [LIVE] Świeca dobiegła końca. Rynek {trade['direction']} uległ rozliczeniu. Aktualizuję saldo...")
                time.sleep(5)
                update_blockchain_balance()
                
                trade["status"] = "ZAKOŃCZONA (EXPIRED)"
                trade["exit_price"] = current_price
                trade["closed_at"] = now.strftime("%H:%M:%S")
                trade["profit"] = 0.0
                
                bot_state["trade_history"].append(trade)
                bot_state["active_trade"] = None

def run_trading_strategy():
    """Pętla wykonawcza analizująca rynki i zawierająca transakcje na Polymarkecie"""
    add_log("Inicjalizacja silnika handlowego LIVE...")
    
    if not init_live_connections():
        add_log("❌ Nie udało się wystartować w trybie LIVE. Sprawdź poprawność kluczy API na Renderze!")
        return

    while True:
        try:
            current_price = get_btc_price()
            if not current_price:
                time.sleep(5)
                continue
            
            update_candle_logic(current_price)
            
            with state_lock:
                m_left = bot_state["minutes_left"]
                active = bot_state["active_trade"]
                sma = bot_state["sma"]
                strike = bot_state["current_candle_strike"]
                balance = bot_state["real_balance"]

            # -----------------------------------------------------------------
            # 1. MONITOROWANIE I AKTYWNY STOP-LOSS / TAKE-PROFIT (EARLY EXIT)
            # -----------------------------------------------------------------
            if active and ENABLE_EARLY_EXIT and clob_client:
                price_diff = current_price - active["strike_price"]
                volatility_denominator = 5.0 + (m_left * 2.0)
                
                try:
                    if active["direction"] == "UP":
                        sim_share_price = 1.0 / (1.0 + math.exp(-price_diff / volatility_denominator))
                    else:
                        sim_share_price = 1.0 / (1.0 + math.exp(price_diff / volatility_denominator))
                    sim_share_price = min(0.98, max(0.02, sim_share_price))
                except OverflowError:
                    sim_share_price = 0.98 if price_diff > 0 else 0.02

                # A. STOP-LOSS
                if sim_share_price <= STOP_LOSS_PRICE:
                    recovered_amount = active["amount_shares"] * sim_share_price
                    loss = recovered_amount - active["cost"]
                    
                    add_log(f"🛡️ [STOP LOSS] Uruchamiam natychmiastowe wyjście z pozycji {active['direction']}...")
                    try:
                        order_args = OrderArgs(
                            price=sim_share_price,
                            size=active["amount_shares"],
                            side="SELL",
                            token_id=active["token_id"]
                        )
                        signed_order = clob_client.create_order(order_args)
                        clob_client.post_order(signed_order)
                        add_log(f"✅ Sprzedano udziały na Polymarket. Strata ograniczona do: {loss:.2f} USDC.")
                    except Exception as e:
                        add_log(f"🚨 Błąd wysyłania zlecenia Stop-Loss do CLOB API: {e}")

                    with state_lock:
                        active["status"] = "STOP LOSS (SELL)"
                        active["profit"] = loss
                        active["exit_price"] = current_price
                        active["closed_at"] = datetime.utcnow().strftime("%H:%M:%S")
                        bot_state["trade_history"].append(active)
                        bot_state["active_trade"] = None
                    update_blockchain_balance()
                    time.sleep(10)
                    continue

                # B. TAKE-PROFIT
                elif sim_share_price >= TAKE_PROFIT_PRICE:
                    secured_amount = active["amount_shares"] * sim_share_price
                    profit = secured_amount - active["cost"]
                    
                    add_log(f"💰 [TAKE PROFIT] Zabezpieczam zysk przed czasem dla pozycji {active['direction']}...")
                    try:
                        order_args = OrderArgs(
                            price=sim_share_price,
                            size=active["amount_shares"],
                            side="SELL",
                            token_id=active["token_id"]
                        )
                        signed_order = clob_client.create_order(order_args)
                        clob_client.post_order(signed_order)
                        add_log(f"✅ Zysk zaksięgowany na giełdzie: +{profit:.2f} USDC.")
                    except Exception as e:
                        add_log(f"🚨 Błąd wysyłania zlecenia Take-Profit do CLOB API: {e}")

                    with state_lock:
                        active["status"] = "TAKE PROFIT (SELL)"
                        active["profit"] = profit
                        active["exit_price"] = current_price
                        active["closed_at"] = datetime.utcnow().strftime("%H:%M:%S")
                        bot_state["trade_history"].append(active)
                        bot_state["active_trade"] = None
                    update_blockchain_balance()
                    time.sleep(10)
                    continue

            # -----------------------------------------------------------------
            # 2. OTWIERANIE NOWYCH TRANSAKCJI
            # -----------------------------------------------------------------
            if 5 <= m_left <= 10 and not active and sma > 0 and strike > 0:
                price_diff = current_price - strike
                
                # Obliczanie wielkości stawki z zarządzaniem kapitałem
                if USE_DYNAMIC_RISK:
                    investment = (balance * RISK_PERCENT) / 100.0
                    investment = min(balance, max(2.0, investment))
                else:
                    investment = min(balance, FIXED_TRADE_AMOUNT)

                if investment < 2.0:
                    time.sleep(10)
                    continue

                market_info = fetch_active_polymarket_15m()
                token_to_buy = None
                direction = None
                
                # Scenariusz UP
                if current_price > sma + PRICE_MARGIN and price_diff > STRIKE_MARGIN:
                    direction = "UP"
                    if market_info:
                        token_to_buy = market_info["yes_token"]
                        strike = market_info["strike"]

                # Scenariusz DOWN
                elif current_price < sma - PRICE_MARGIN and price_diff < -STRIKE_MARGIN:
                    direction = "DOWN"
                    if market_info:
                        token_to_buy = market_info["no_token"]
                        strike = market_info["strike"]

                if direction and token_to_buy and clob_client:
                    share_price = min(0.90, max(0.55, 0.50 + (abs(price_diff) / 100)))
                    shares = investment / share_price

                    add_log(f"🛒 [LIVE] Składam zlecenie zakupu {direction} po kursie ${share_price:.2f} za sztukę...")
                    try:
                        order_args = OrderArgs(
                            price=share_price,
                            size=shares,
                            side="BUY",
                            token_id=token_to_buy
                        )
                        signed_order = clob_client.create_order(order_args)
                        clob_client.post_order(signed_order)
                        add_log(f"✅ Pomyślnie kupiono udziały na Polymarkecie! Kwota: {investment:.2f} USDC.")
                        
                        with state_lock:
                            bot_state["active_trade"] = {
                                "direction": direction,
                                "entry_price": share_price,
                                "strike_price": strike,
                                "btc_at_entry": current_price,
                                "amount_shares": shares,
                                "cost": investment,
                                "token_id": token_to_buy,
                                "opened_at": datetime.utcnow().strftime("%H:%M:%S")
                            }
                        update_blockchain_balance()
                    except Exception as e:
                        add_log(f"🚨 Błąd składania zlecenia zakupu: {e}")

        except Exception as e:
            add_log(f"🚨 Nieoczekiwany błąd w pętli głównej: {e}")
            
        time.sleep(5)

# --- WEB PANEL MONITORUJĄCY DLA TELEFONU ---
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
        
        html = """
        <!DOCTYPE html>
        <html lang="pl">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Krajekis Bot Dashboard LIVE</title>
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
                                <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-rose-400 opacity-75"></span>
                                <span class="relative inline-flex rounded-full h-3 w-3 bg-rose-500"></span>
                            </span>
                            <h1 class="text-2xl font-bold tracking-tight text-white">Krajekis Bot Panel [LIVE]</h1>
                        </div>
                        <p class="text-sm text-slate-400 mt-1">Prawdziwy automatyczny handel na giełdzie Polymarket (sieć Polygon)</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800 rounded-xl px-5 py-3 flex items-center gap-4">
                        <div>
                            <p class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Stan Twojego Portfela</p>
                            <p id="ui-balance" class="text-xl font-bold text-emerald-400">Pobieranie...</p>
                        </div>
                        <div class="p-2 bg-emerald-500/10 rounded-lg">
                            <i class="fa-solid fa-wallet text-emerald-400 text-lg"></i>
                        </div>
                    </div>
                </div>

                <!-- STATYSTYKI GŁÓWNE -->
                <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
                    <!-- CENA BTC -->
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Prawdziwa Cena BTC spot</p>
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
                        <p id="ui-profit" class="text-xs mt-2 text-emerald-400">Rynek LIVE</p>
                    </div>
                </div>

                <!-- AKTYWNA TRANSAKCJA -->
                <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 mb-8 shadow-xl">
                    <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                        <i class="fa-solid fa-chart-line text-indigo-400"></i> Aktywne Zlecenie w Arkuszu Polymarket
                    </h2>
                    <div id="ui-active-box" class="text-slate-400 py-4 text-center">
                        Brak aktywnego zlecenia na giełdzie. Bot czeka na optymalne moment.
                    </div>
                </div>

                <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
                    <!-- KONSOLA NA ŻYWO -->
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl flex flex-col h-[400px]">
                        <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                            <i class="fa-solid fa-terminal text-emerald-400"></i> Konsola Bota na żywo
                        </h2>
                        <div id="ui-logs" class="bg-slate-950 p-4 rounded-xl font-mono text-xs text-emerald-400/90 overflow-y-auto flex-1 space-y-1.5 border border-slate-800/40">
                            Pobieranie logów z giełdy...
                        </div>
                    </div>

                    <!-- HISTORIA TRANSAKCJI -->
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl flex flex-col h-[400px]">
                        <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                            <i class="fa-solid fa-history text-indigo-400"></i> Ostatnie zamknięte zlecenia
                        </h2>
                        <div class="overflow-y-auto flex-1">
                            <table class="w-full text-left text-sm">
                                <thead class="text-xs text-slate-400 uppercase bg-slate-950/40 sticky top-0">
                                    <tr>
                                        <th class="py-2.5 px-3">Kierunek</th>
                                        <th class="py-2.5 px-3">Kurs wejścia</th>
                                        <th class="py-2.5 px-3">Strike vs Meta</th>
                                        <th class="py-2.5 px-3">Status</th>
                                    </tr>
                                </thead>
                                <tbody id="ui-history-rows" class="divide-y divide-slate-800/40">
                                    <tr>
                                        <td colspan="4" class="py-6 text-center text-slate-500">Brak zamkniętych zleceń.</td>
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

                        document.getElementById('ui-balance').innerText = '$' + data.real_balance.toFixed(2) + ' USDC';
                        
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
                            document.getElementById('ui-strike').innerText = 'Czekam na aktywną świecę...';
                        }

                        const activeBox = document.getElementById('ui-active-box');
                        if (data.active_trade) {
                            const trade = data.active_trade;
                            activeBox.innerHTML = `
                                <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-left bg-slate-950 p-4 rounded-xl border border-rose-500/20">
                                    <div>
                                        <p class="text-xs text-slate-400">ZLECENIE</p>
                                        <p class="text-lg font-bold ` + (trade.direction === 'UP' ? 'text-emerald-400' : 'text-rose-400') + `">KUP: ` + trade.direction + `</p>
                                    </div>
                                    <div>
                                        <p class="text-xs text-slate-400">KURS WEJŚCIA</p>
                                        <p class="text-lg font-bold text-slate-200">$` + trade.entry_price.toFixed(2) + `</p>
                                    </div>
                                    <div>
                                        <p class="text-xs text-slate-400">CENA WEJŚCIA BTC</p>
                                        <p class="text-lg font-bold text-slate-200">$` + trade.btc_at_entry.toLocaleString() + `</p>
                                    </div>
                                    <div>
                                        <p class="text-xs text-slate-400">UDZIAŁY / WARTOŚĆ</p>
                                        <p class="text-lg font-bold text-slate-200">` + trade.amount_shares.toFixed(1) + ` szt. / $` + trade.cost.toFixed(2) + ` USDC</p>
                                    </div>
                                </div>
                            `;
                        } else {
                            activeBox.innerHTML = `<p class="text-slate-500 py-2">Brak otwartych pozycji. Silnik monitoruje ruchy Bitcoina...</p>`;
                        }

                        const logsDiv = document.getElementById('ui-logs');
                        if (data.logs.length > 0) {
                            logsDiv.innerHTML = data.logs.slice().reverse().map(function(l) {
                                return '<div>' + l + '</div>';
                            }).join('');
                        } else {
                            logsDiv.innerHTML = '<div class="text-slate-500">Pobieranie logów...</div>';
                        }

                        const historyRows = document.getElementById('ui-history-rows');
                        if (data.trade_history.length > 0) {
                            let totalWins = 0;
                            
                            const rowsHtml = data.trade_history.slice().reverse().map(function(t) {
                                if (t.status.includes("WYGRANA") || t.status.includes("TAKE PROFIT") || t.status.includes("EXPIRED")) totalWins++;
                                
                                const dirColor = t.direction === 'UP' ? 'text-emerald-400' : 'text-rose-400';
                                
                                return `
                                    <tr class="border-b border-slate-800/30">
                                        <td class="py-3 px-3 font-semibold ` + dirColor + `">` + t.direction + `</td>
                                        <td class="py-3 px-3">$` + t.entry_price.toFixed(2) + `</td>
                                        <td class="py-3 px-3 text-xs text-slate-400">$` + t.strike_price.toLocaleString() + ` vs $` + t.exit_price.toLocaleString() + `</td>
                                        <td class="py-3 px-3 font-bold text-slate-300">` + t.status + `</td>
                                    </tr>
                                `;
                            }).join('');
                            
                            historyRows.innerHTML = rowsHtml;
                            
                            const winRate = (totalWins / data.trade_history.length) * 100;
                            document.getElementById('ui-stats').innerText = totalWins + ' / ' + data.trade_history.length + ' (' + winRate.toFixed(0) + '%)';
                            document.getElementById('ui-profit').innerText = "Rynek LIVE";
                        } else {
                            document.getElementById('ui-stats').innerText = "0 / 0 (0%)";
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
    # Start wątku handlowego
    bot_thread = threading.Thread(target=run_trading_strategy)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Start serwera monitorującego na porcie przypisanym przez Render
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    add_log(f"Serwer Dashboard [LIVE] wystartował na porcie {port}")
    server.serve_forever()
