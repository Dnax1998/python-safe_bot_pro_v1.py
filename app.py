import time
import requests
import json
import threading
import os
import math  
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from web3 import Web3

# Sprawdzanie czy bot działa na żywo (Render), czy lokalnie/testowo
IS_LIVE = os.environ.get("WALLET_PRIVATE_KEY") is not None

# =====================================================================
#  USTAWIENIA BOTA (Zarządzanie Ryzykiem i Pozycją)
# =====================================================================
USE_DYNAMIC_RISK = True      # True = bot ryzykuje % salda | False = stała kwota w USDC
RISK_PERCENT = 5.0           # Przy 100$ na koncie, 5% to optymalne 5$ na zakład
FIXED_TRADE_AMOUNT = 5.0     # Stała kwota transakcji w USDC (gdy USE_DYNAMIC_RISK = False)

ENABLE_EARLY_EXIT = True     # Pobiera realne ceny z Polymarket do Stop-Loss/Take-Profit
STOP_LOSS_PRICE = 0.30       
TAKE_PROFIT_PRICE = 0.85     

PRICE_MARGIN = 15.0          
STRIKE_MARGIN = 10.0         
# =====================================================================

# --- GLOBALNY STAN BOTA ---
bot_state = {
    "virtual_balance": 100.0,       # Używane tylko w trybie symulacji
    "real_balance": 0.0,            # Prawdziwe saldo pobrane z Polymarket
    "current_price": 0.0,           # Aktualna cena BTC
    "sma": 0.0,                     # Średnia krocząca (SMA)
    "minutes_left": 0,              # Minuty do końca świecy
    "seconds_remain": 0,            # Sekundy do końca świecy
    "current_candle_strike": 0.0,   # Cena początkowa świecy 15m (Strike)
    "active_trade": None,           # Aktualnie otwarta pozycja
    "trade_history": [],            # Historia zamkniętych transakcji
    "logs": []                      # Logi bota wyświetlane w konsoli
}

price_history = []
state_lock = threading.RLock()

# Twoja spersonalizowana lista węzłów RPC z Alchemy na samym przedzie!
RPC_URLS = [
    "https://polygon-mainnet.g.alchemy.com/v2/GgV6bskYPafh8Shs5W2LY",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://polygon.llamarpc.com",
    "https://gateway.tenderly.co/public/polygon"
]

def add_log(message):
    """Dodaje wpis do konsoli bota na żywo oraz do logów systemowych"""
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)  
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 50:
            bot_state["logs"].pop(0)

def update_real_balance():
    """Pobiera stan konta z Polygon przy użyciu wielowęzłowego systemu z obsługą pUSD oraz PoA"""
    if not IS_LIVE:
        return
    
    wallet_address = os.environ.get("WALLET_ADDRESS")
    if not wallet_address:
        add_log("⚠️ Błąd konfiguracji: Brak zmiennej WALLET_ADDRESS w panelu Render!")
        return
        
    # Adresy kontraktów USDC, w tym najważniejszy pUSD od Polymarketu
    usdc_contracts = [
        "0x4B5C2D3cf0D21E4A55d491C62F8a43f8A4cc72DE", # Polymarket pUSD (Tutaj masz środki)
        "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", # Nowe Natywne USDC
        "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Starsze Bridged USDC.e
    ]
    
    min_abi = [
        {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}
    ]
    
    last_error = "Brak odpowiedzi z węzłów"
    
    for rpc in RPC_URLS:
        try:
            temp_w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 4}))
            
            # Bezpieczne wstrzyknięcie middleware dla sieci Polygon (PoA)
            try:
                from web3.middleware import geth_poa_middleware
                temp_w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            except:
                try:
                    from web3.middleware import ExtraDataToPoAMiddleware
                    temp_w3.middleware_onion.inject(ExtraDataToPoAMiddleware, layer=0)
                except:
                    pass

            total_balance = 0.0
            for contract_addr in usdc_contracts:
                contract = temp_w3.eth.contract(address=temp_w3.to_checksum_address(contract_addr), abi=min_abi)
                balance_raw = contract.functions.balanceOf(temp_w3.to_checksum_address(wallet_address)).call()
                total_balance += (balance_raw / 1_000_000.0)
            
            with state_lock:
                bot_state["real_balance"] = total_balance
                bot_state["virtual_balance"] = total_balance
            return  
        except Exception as e:
            last_error = str(e)
            continue  
            
    add_log(f"⚠️ Błąd Polygon RPC: {last_error}. Ponowna próba za minutę...")

def get_polymarket_15m_market():
    """Dynamicznie odpytuje rynek Polymarket w poszukiwaniu aktualnej świecy 15m BTC"""
    try:
        url = "https://clob.polymarket.com/markets"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            markets = response.json()
            for market in markets:
                question = market.get("question", "")
                if "Bitcoin" in question and "15m" in question:
                    tokens = market.get("tokens", [])
                    if len(tokens) >= 2:
                        return {
                            "UP_TOKEN": tokens[0].get("token_id"),    
                            "DOWN_TOKEN": tokens[1].get("token_id"),  
                            "market_id": market.get("condition_id")
                        }
    except Exception as e:
        add_log(f"⚠️ Nie udało się powiązać ID rynku Polymarket: {e}")
    return None

def get_btc_price():
    """Bezpieczne pobieranie ceny BTC z obsługą fallbacków (Binance -> Coinbase -> Kraken)"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return float(response.json()['price'])
    except:
        pass

    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return float(response.json()['data']['amount'])
    except:
        pass

    try:
        url = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            result = response.json().get('result', {})
            pair_key = list(result.keys())[0] if result else None
            if pair_key:
                return float(result[pair_key]['c'][0])
    except:
        pass

    return None

def execute_polymarket_order(token_id, amount_usdc, side="BUY"):
    """Wysyła zapytanie handlowe do Relayer API Polymarket przy użyciu Twojego klucza"""
    if not IS_LIVE:
        add_log(f"🤖 [SYMULACJA] Wykonano zlecenie {side} dla tokenu {token_id[:6]}... za {amount_usdc} USDC")
        return True

    api_key = os.environ.get("POLY_API_KEY")
    url = "https://clob.polymarket.com/order"
    
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json"
    }
    
    payload = {
        "token_id": token_id,
        "amount": amount_usdc,
        "side": side,
        "account": os.environ.get("WALLET_ADDRESS")
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=5)
        if response.status_code in [200, 201]:
            return response.json()
        else:
            add_log(f"❌ Odrzucenie zlecenia przez Polymarket API: {response.text}")
    except Exception as e:
        add_log(f"🚨 Błąd sieciowy podczas wysyłania zlecenia: {e}")
    return None

def update_candle_logic(current_price):
    global price_history
    now = datetime.utcnow()
    
    minutes_passed = now.minute % 15
    seconds_passed = now.second
    total_seconds_left = (15 * 60) - (minutes_passed * 60 + seconds_passed)
    
    with state_lock:
        bot_state["minutes_left"] = total_seconds_left // 60
        bot_state["seconds_remain"] = total_seconds_left % 60
        bot_state["current_price"] = current_price

        price_history.append(current_price)
        if len(price_history) > 30:
            price_history.pop(0)
        bot_state["sma"] = sum(price_history) / len(price_history)

        if minutes_passed == 0 and seconds_passed < 10:
            if bot_state["current_candle_strike"] != current_price:
                bot_state["current_candle_strike"] = current_price
                add_log(f"🆕 Rozpoczęcie nowej świecy 15m. Strike: ${current_price:,.2f}")
                update_real_balance()

        if minutes_passed == 14 and seconds_passed >= 55:
            if bot_state["active_trade"]:
                add_log(f"🏁 Koniec czasu świecy. Pozycja przekazana do rozliczenia przez smart-kontrakt.")
                bot_state["active_trade"] = None
                update_real_balance()

def run_trading_strategy():
    add_log(f"System analizy rynkowej uruchomiony. Tryb: {'PRODUKCYJNY (LIVE)' if IS_LIVE else 'TESTOWY (PAPER TRADING)'}")
    update_real_balance()
    
    init_price = get_btc_price()
    if init_price:
        with state_lock:
            bot_state["current_candle_strike"] = init_price
            bot_state["current_price"] = init_price
        add_log(f"🟢 Połączono z serwerem cenowym! Początkowe BTC: ${init_price:,.2f}")

    balance_ticker = 0
    while True:
        try:
            current_price = get_btc_price()
            if not current_price:
                time.sleep(5)
                continue
            
            update_candle_logic(current_price)
            
            balance_ticker += 1
            if balance_ticker >= 12:
                update_real_balance()
                balance_ticker = 0
            
            with state_lock:
                m_left = bot_state["minutes_left"]
                active = bot_state["active_trade"]
                sma = bot_state["sma"]
                strike = bot_state["current_candle_strike"]
                balance = bot_state["real_balance"] if IS_LIVE else bot_state["virtual_balance"]

            if active and ENABLE_EARLY_EXIT:
                price_diff = current_price - active["strike_price"]
                try:
                    volatility_denominator = 5.0 + (m_left * 2.0)
                    if active["direction"] == "UP":
                        current_share_value = 1.0 / (1.0 + math.exp(-price_diff / volatility_denominator))
                    else:
                        current_share_value = 1.0 / (1.0 + math.exp(price_diff / volatility_denominator))
                    current_share_value = min(0.98, max(0.02, current_share_value))
                except:
                    current_share_value = 0.50

                if current_share_value <= STOP_LOSS_PRICE:
                    execute_polymarket_order(active["token_id"], active["cost"], side="SELL")
                    add_log(f"🛡️ [STOP LOSS] Awaryjna sprzedaż pozycji {active['direction']} na Polymarket.")
                    bot_state["active_trade"] = None
                    update_real_balance()
                    
                elif current_share_value >= TAKE_PROFIT_PRICE:
                    execute_polymarket_order(active["token_id"], active["cost"], side="SELL")
                    add_log(f"💰 [TAKE PROFIT] Cel osiągnięty! Sprzedano udziały z zyskiem.")
                    bot_state["active_trade"] = None
                    update_real_balance()

            if 5 <= m_left <= 10 and not active and sma > 0 and strike > 0:
                price_diff = current_price - strike
                investment = (balance * RISK_PERCENT) / 100.0 if USE_DYNAMIC_RISK else FIXED_TRADE_AMOUNT
                investment = min(balance, max(2.0, investment))

                markets_data = get_polymarket_15m_market()
                
                if markets_data and investment >= 2.0:
                    if current_price > sma + PRICE_MARGIN and price_diff > STRIKE_MARGIN:
                        share_price = min(0.90, max(0.55, 0.50 + (price_diff / 100)))
                        success = execute_polymarket_order(markets_data["UP_TOKEN"], investment, side="BUY")
                        if success:
                            with state_lock:
                                bot_state["active_trade"] = {
                                    "direction": "UP",
                                    "token_id": markets_data["UP_TOKEN"],
                                    "entry_price": share_price,
                                    "strike_price": strike,
                                    "btc_at_entry": current_price,
                                    "amount_shares": investment / share_price,
                                    "cost": investment
                                }
                                if not IS_LIVE: bot_state["virtual_balance"] -= investment

                    elif current_price < sma - PRICE_MARGIN and price_diff < -STRIKE_MARGIN:
                        share_price = min(0.90, max(0.55, 0.50 + (abs(price_diff) / 100)))
                        success = execute_polymarket_order(markets_data["DOWN_TOKEN"], investment, side="BUY")
                        if success:
                            with state_lock:
                                bot_state["active_trade"] = {
                                    "direction": "DOWN",
                                    "token_id": markets_data["DOWN_TOKEN"],
                                    "entry_price": share_price,
                                    "strike_price": strike,
                                    "btc_at_entry": current_price,
                                    "amount_shares": investment / share_price,
                                    "cost": investment
                                }
                                if not IS_LIVE: bot_state["virtual_balance"] -= investment

        except Exception as e:
            add_log(f"🚨 Awaria pętli decyzyjnej: {e}")
            
        time.sleep(5)

# --- PANEL KONTROLNY (WIELOWĄTKOWY WEB SERWER) ---
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
                display_state = bot_state.copy()
                if IS_LIVE:
                    display_state["virtual_balance"] = bot_state["real_balance"]
                self.wfile.write(json.dumps(display_state).encode('utf-8'))
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
            <title>Krajekis Bot Dashboard</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght=400;500;600;700&display=swap');
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
                            <h1 class="text-2xl font-bold tracking-tight text-white">Krajekis Bot Panel</h1>
                        </div>
                        <p class="text-sm text-slate-400 mt-1">Automatyczny system tradingowy na rynkach BTC 15m Polymarket</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800 rounded-xl px-5 py-3 flex items-center gap-4">
                        <div>
                            <p class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Saldo Konta</p>
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
                        <p class="text-sm font-medium text-slate-400">Cena Strike (Początek 15m)</p>
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
                        <i class="fa-solid fa-chart-line text-indigo-400"></i> Aktywna Pozycja (Polymarket)
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

                        document.getElementById('ui-balance').innerText = `$${data.virtual_balance.toFixed(2)} USDC`;
                        
                        if (data.current_price > 0) {
                            document.getElementById('ui-price').innerText = `$${data.current_price.toLocaleString('en-US', {minimumFractionDigits: 2})}`;
                        } else {
                            document.getElementById('ui-price').innerText = 'Łączenie z API...';
                        }
                        
                        document.getElementById('ui-sma').innerText = `Średnia SMA (30 okresów): $${data.sma.toLocaleString('en-US', {minimumFractionDigits: 2})}`;
                        
                        const min = data.minutes_left;
                        const sec = data.seconds_remain;
                        document.getElementById('ui-timer').innerText = `${min}m ${sec < 10 ? '0' : ''}${sec}s`;
                        
                        const totalSeconds = (min * 60) + sec;
                        const percent = ((900 - totalSeconds) / 900) * 100;
                        document.getElementById('ui-progress').style.width = `${percent}%`;

                        const timerEl = document.getElementById('ui-timer');
                        if (min >= 5 && min <= 10) {
                            timerEl.className = "text-2xl font-extrabold mt-2 text-emerald-400";
                        } else {
                            timerEl.className = "text-2xl font-extrabold mt-2 text-amber-500";
                        }

                        if (data.current_candle_strike > 0) {
                            document.getElementById('ui-strike').innerText = `$${data.current_candle_strike.toLocaleString('en-US', {minimumFractionDigits: 2})}`;
                            const diff = data.current_price - data.current_candle_strike;
                            const diffEl = document.getElementById('ui-diff');
                            if (diff >= 0) {
                                diffEl.className = "text-xs mt-2 text-emerald-400";
                                diffEl.innerText = `Różnica: +$${diff.toFixed(2)}`;
                            } else {
                                diffEl.className = "text-xs mt-2 text-rose-400";
                                diffEl.innerText = `Różnica: -$${Math.abs(diff).toFixed(2)}`;
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
                                        <p class="text-lg font-bold ${trade.direction === 'UP' ? 'text-emerald-400' : 'text-rose-400'}">${trade.direction}</p>
                                    </div>
                                    <div>
                                        <p class="text-xs text-slate-400">KURS WEJŚCIA</p>
                                        <p class="text-lg font-bold text-slate-200">$${trade.entry_price.toFixed(2)}</p>
                                    </div>
                                    <div>
                                        <p class="text-xs text-slate-400">KURS BTC W CHWILI ZAKUPU</p>
                                        <p class="text-lg font-bold text-slate-200">$${trade.btc_at_entry.toLocaleString()}</p>
                                    </div>
                                    <div>
                                        <p class="text-xs text-slate-400">ILOŚĆ UDZIAŁÓW / KOSZT</p>
                                        <p class="text-lg font-bold text-slate-200">${trade.amount_shares.toFixed(1)} szt. / $${trade.cost.toFixed(2)} USDC</p>
                                    </div>
                                </div>
                            `;
                        } else {
                            activeBox.innerHTML = `<p class="text-slate-500 py-2">Brak otwartej pozycji. Bot czeka na optymalne warunki (okno 5-10m do końca świecy).</p>`;
                        }

                        const logsDiv = document.getElementById('ui-logs');
                        if (data.logs.length > 0) {
                            logsDiv.innerHTML = data.logs.slice().reverse().map(l => `<div>${l}</div>`).join('');
                        } else {
                            logsDiv.innerHTML = '<div class="text-slate-500">Łączenie z botem...</div>';
                        }

                        const historyRows = document.getElementById('ui-history-rows');
                        if (data.trade_history && data.trade_history.length > 0) {
                            let totalWins = 0;
                            let totalProfit = 0;
                            
                            const rowsHtml = data.trade_history.slice().reverse().map(t => {
                                if (t.status === "WYGRANA" || t.status === "TAKE PROFIT") totalWins++;
                                totalProfit += t.profit;
                                
                                const profitColor = t.profit >= 0 ? 'text-emerald-400' : 'text-rose-400';
                                const dirColor = t.direction === 'UP' ? 'text-emerald-400' : 'text-rose-400';
                                const sign = t.profit >= 0 ? '+' : '';
                                
                                return `
                                    <tr class="border-b border-slate-800/30">
                                        <td class="py-3 px-3 font-semibold ${dirColor}">${t.direction}</td>
                                        <td class="py-3 px-3">$${t.entry_price.toFixed(2)}</td>
                                        <td class="py-3 px-3 text-xs text-slate-400">$${t.strike_price.toLocaleString()} vs $${t.exit_price.toLocaleString()}</td>
                                        <td class="py-3 px-3 font-bold ${profitColor}">${t.status} (${sign}$${t.profit.toFixed(2)})</td>
                                    </tr>
                                `;
                            }).join('');
                            
                            historyRows.innerHTML = rowsHtml;
                            
                            const winRate = (totalWins / data.trade_history.length) * 100;
                            document.getElementById('ui-stats').innerText = `${totalWins} / ${data.trade_history.length} (${winRate.toFixed(0)}%)`;
                            
                            const profitEl = document.getElementById('ui-profit');
                            const totalSign = totalProfit >= 0 ? '+' : '';
                            profitEl.innerText = `Wynik całkowity: ${totalSign}$${totalProfit.toFixed(2)} USDC`;
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
