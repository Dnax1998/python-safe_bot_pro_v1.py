import time
import requests
import json
import threading
import os
import math  
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from web3 import Web3

# =====================================================================
# --- BEZPIECZNA WERYFIKACJA INTEGRACJI SDK POLYMARKET ---
# =====================================================================
HAS_SDK = False
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    from py_clob_client.clob_types import OrderArgs, ApiCreds
    HAS_SDK = True
except ImportError:
    HAS_SDK = False

# Sprawdzanie czy bot działa na żywo (Render), czy lokalnie/testowo
IS_LIVE = os.environ.get("WALLET_PRIVATE_KEY") is not None

# =====================================================================
#  USTAWIENIA BOTA (Zarządzanie Ryzykiem i Pozycją)
# =====================================================================
USE_DYNAMIC_RISK = True      # True = bot ryzykuje % salda | False = stała kwota w USDC
RISK_PERCENT = 5.0           # Przy 100$ na koncie, 5% to optymalne 5$ na zakład
FIXED_TRADE_AMOUNT = 5.0     # Stała kwota transakcji w USDC (gdy USE_DYNAMIC_RISK = False)

ENABLE_EARLY_EXIT = True     # Dynamiczny Stop-Loss/Take-Profit wewnątrz świecy
STOP_LOSS_PRICE = 0.30       
TAKE_PROFIT_PRICE = 0.85     

PRICE_MARGIN = 15.0          # Wymagany dystans ceny od średniej SMA (w USD)
STRIKE_MARGIN = 10.0         # Wymagany dystans ceny od punktu Strike (w USD)
# =====================================================================

# --- GLOBALNY STAN BOTA ---
bot_state = {
    "virtual_balance": 100.0,       
    "real_balance": 0.0,            
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
poly_client = None  

# Zarządzanie węzłami RPC Polygon
PRIVATE_RPC = os.environ.get("POLYGON_RPC_URL")
RPC_URLS = []
if PRIVATE_RPC:
    RPC_URLS.append(PRIVATE_RPC.strip())
RPC_URLS.extend([
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic"
])

def add_log(message):
    """Dodaje wpis do konsoli bota oraz do pamięci stanów UI"""
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)  
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 40:
            bot_state["logs"].pop(0)

def init_clob_client():
    """Inicjalizuje zaawansowanego klienta Polymarket CLOB"""
    global poly_client
    if not IS_LIVE:
        return
    
    if not HAS_SDK:
        add_log("❌ BŁĄD SDK: Brak 'py-clob-client' lub błąd krytyczny importu typów w środowisku Render.")
        return

    try:
        private_key = os.environ.get("WALLET_PRIVATE_KEY", "").replace("0x", "")
        api_key = os.environ.get("POLY_API_KEY")
        api_secret = os.environ.get("POLY_API_SECRET")
        api_passphrase = os.environ.get("POLY_API_PASSPHRASE")

        if api_key and api_secret and api_passphrase:
            # Parametry ApiCreds podane pozycyjnie w celu uniknięcia konfliktów nazw argumentów w SDK
            poly_client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=POLYGON,
                private_key=private_key,
                api_creds=ApiCreds(api_key, api_secret, api_passphrase)
            )
            add_log("✅ Autoryzacja CLOB powiodła się. Moduł handlowy aktywny.")
        else:
            poly_client = ClobClient(host="https://clob.polymarket.com", private_key=private_key, chain_id=POLYGON)
            add_log("⚠️ Moduł CLOB zainicjalizowany bez kluczy API Secret/Passphrase (Tylko odczyt).")
    except Exception as e:
        add_log(f"⚠️ Podsystem transakcyjny CLOB nie mógł wystartować: {e}")

def update_real_balance():
    """Pobiera zabezpieczone saldo USDC i pUSD z portfela Polygon"""
    if not IS_LIVE:
        return
    wallet_address = os.environ.get("WALLET_ADDRESS")
    if not wallet_address:
        return
        
    usdc_contracts = [
        "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb", # Polymarket pUSD
        "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # Native USDC
    ]
    min_abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]
    
    for rpc in RPC_URLS:
        try:
            temp_w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 3}))
            try:
                from web3.middleware import geth_poa_middleware
                temp_w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            except: pass

            total_balance = 0.0
            for contract_addr in usdc_contracts:
                try:
                    contract = temp_w3.eth.contract(address=temp_w3.to_checksum_address(contract_addr), abi=min_abi)
                    balance_raw = contract.functions.balanceOf(temp_w3.to_checksum_address(wallet_address)).call()
                    total_balance += (balance_raw / 1_000_000.0)
                except: continue
            
            with state_lock:
                bot_state["real_balance"] = total_balance
                bot_state["virtual_balance"] = total_balance
            return
        except: continue

def get_polymarket_15m_market():
    """Pobiera dane strukturalne aktualnego rynku BTC z Gamma API"""
    try:
        url = "https://gamma-api.polymarket.com/markets?closed=false&order=volume&direction=desc&limit=60&slug=bitcoin"
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=4)
        if response.status_code == 200:
            markets_list = response.json()
            for market in markets_list:
                q = market.get("question", "")
                title = market.get("title", "")
                if "Bitcoin" in q and any(x in q.lower() or x in title.lower() for x in ["15m", "15-min", "15 min", "quarter"]):
                    tokens = market.get("clobTokenIds")
                    if tokens and isinstance(tokens, str):
                        tokens = json.loads(tokens)
                    if tokens and len(tokens) >= 2:
                        return {"UP_TOKEN": tokens[0], "DOWN_TOKEN": tokens[1], "market_id": market.get("conditionId")}
    except Exception as e:
        add_log(f"⚠️ Problem z Gamma API: {e}")
    return None

def get_btc_price():
    """Stabilny i redundantny przelicznik ceny spot BTC"""
    try:
        res = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=3)
        return float(res.json()['price'])
    except:
        try:
            res = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=3)
            return float(res.json()['data']['amount'])
        except: return None

def execute_polymarket_order(token_id, amount_usdc, side="BUY"):
    """Składa bezpieczne zlecenie na giełdzie"""
    if not IS_LIVE:
        add_log(f"🤖 [SYMULACJA] Zlecenie {side} | Token: {token_id[:6]}... | Kwota: {amount_usdc} USDC")
        return True
    
    global poly_client
    if poly_client and HAS_SDK and hasattr(poly_client, 'create_order'):
        try:
            return poly_client.create_order(OrderArgs(price=0.50, size=round(amount_usdc/0.50, 1), side=side, token_id=token_id))
        except Exception as e:
            add_log(f"❌ Błąd SDK CLOB: {e}. Próba wykonania żądania REST...")
            
    url = "https://clob.polymarket.com/order"
    headers = {"x-api-key": os.environ.get("POLY_API_KEY", ""), "Content-Type": "application/json"}
    payload = {"token_id": token_id, "amount": amount_usdc, "side": side, "account": os.environ.get("WALLET_ADDRESS")}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=4)
        return res.status_code in [200, 201]
    except: return False

def update_candle_logic(current_price):
    """Zarządza cyklem życia świecy 15-minutowej oraz historii transakcji"""
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
        if len(price_history) > 40: price_history.pop(0)
        bot_state["sma"] = sum(price_history) / len(price_history)

        # Inicjalizacja nowej świecy
        if minutes_passed == 0 and seconds_passed < 8:
            if bot_state["current_candle_strike"] != current_price:
                bot_state["current_candle_strike"] = current_price
                add_log(f"🆕 Nowy punkt Strike świecy 15m: ${current_price:,.2f}")
                update_real_balance()

        # Automatyczne rozliczenie pozycji na koniec świecy
        if minutes_passed == 14 and seconds_passed >= 54:
            if bot_state["active_trade"]:
                trade = bot_state["active_trade"]
                is_up = "UP" in trade["direction"]
                win = (current_price > trade["strike_price"]) if is_up else (current_price < trade["strike_price"])
                profit = trade["cost"] * 0.85 if win else -trade["cost"]
                
                history_entry = {
                    "direction": trade["direction"],
                    "entry_btc": trade["btc_at_entry"],
                    "strike_btc": trade["strike_price"],
                    "exit_btc": current_price,
                    "result": "WIN" if win else "LOSS",
                    "profit": profit
                }
                bot_state["trade_history"].append(history_entry)
                if not IS_LIVE and win:
                    bot_state["virtual_balance"] += (trade["cost"] * 1.85)
                
                add_log(f"🏁 Koniec świecy rozliczony. Wynik: {'SUKCES 💰' if win else 'PORAŻKA 📉'}")
                bot_state["active_trade"] = None
                update_real_balance()

def run_trading_strategy():
    add_log(f"Uruchomiono system tradingowy Krajekis. Tryb: {'PRODUKCJA' if IS_LIVE else 'SYMULACJA'}")
    init_clob_client()
    update_real_balance()
    
    init_p = get_btc_price()
    if init_p:
        with state_lock:
            bot_state["current_candle_strike"] = init_p
            bot_state["current_price"] = init_p
            
    balance_ticker = 0
    while True:
        try:
            current_price = get_btc_price()
            if not current_price:
                time.sleep(4)
                continue
                
            update_candle_logic(current_price)
            
            balance_ticker += 1
            if balance_ticker >= 15:
                update_real_balance()
                balance_ticker = 0
                
            with state_lock:
                active = bot_state["active_trade"]
                strike = bot_state["current_candle_strike"]
                sma = bot_state["sma"]
                balance = bot_state["real_balance"] if IS_LIVE else bot_state["virtual_balance"]

            # --- MONITORING EARLY EXIT (STOP-LOSS / TAKE-PROFIT) ---
            if active and ENABLE_EARLY_EXIT:
                btc_diff = current_price - active["btc_at_entry"]
                est_token_price = 0.50 + (btc_diff / 80.0) if "UP" in active["direction"] else 0.50 - (btc_diff / 80.0)
                est_token_price = max(0.05, min(0.95, est_token_price))
                
                if est_token_price <= STOP_LOSS_PRICE or est_token_price >= TAKE_PROFIT_PRICE:
                    is_tp = est_token_price >= TAKE_PROFIT_PRICE
                    profit = active["cost"] * (est_token_price / 0.50 - 1)
                    
                    execute_polymarket_order(active["token_id"], active["cost"], side="SELL")
                    
                    history_entry = {
                        "direction": f"EARLY {'TP' if is_tp else 'SL'}",
                        "entry_btc": active["btc_at_entry"],
                        "strike_btc": active["strike_price"],
                        "exit_btc": current_price,
                        "result": "WIN" if is_tp else "LOSS",
                        "profit": profit
                    }
                    with state_lock:
                        bot_state["trade_history"].append(history_entry)
                        if not IS_LIVE:
                            bot_state["virtual_balance"] += (active["cost"] + profit)
                        bot_state["active_trade"] = None
                    add_log(f"🚨 Awaryjne zamknięcie pozycji ({'Take-Profit' if is_tp else 'Stop-Loss'}) przy cenie tokenu {est_token_price:.2f}")

            # --- REALIZACJA LOGIKI STRATEGII WEJŚCIA ---
            if not active and strike > 0 and sma > 0:
                buy_up = (current_price > strike + STRIKE_MARGIN) and (current_price > sma + PRICE_MARGIN)
                buy_down = (current_price < strike - STRIKE_MARGIN) and (current_price < sma - PRICE_MARGIN)
                
                if buy_up or buy_down:
                    investment = (balance * RISK_PERCENT) / 100.0 if USE_DYNAMIC_RISK else FIXED_TRADE_AMOUNT
                    investment = min(balance, max(2.0, investment))
                    
                    markets_data = get_polymarket_15m_market()
                    if markets_data and investment >= 2.0:
                        chosen_token = markets_data["UP_TOKEN"] if buy_up else markets_data["DOWN_TOKEN"]
                        dir_str = "UP" if buy_up else "DOWN"
                        method = markets_data.get("market_id", "Gamma")
                        
                        add_log(f"🎯 Sygnał {dir_str}! BTC: ${current_price:,.2f} | SMA: ${sma:,.2f}. Kupuję kontrakt...")
                        if execute_polymarket_order(chosen_token, investment, side="BUY"):
                            with state_lock:
                                bot_state["active_trade"] = {
                                    "direction": f"{dir_str} ({method[:6]})",
                                    "token_id": chosen_token,
                                    "entry_price": 0.50,
                                    "strike_price": strike,
                                    "btc_at_entry": current_price,
                                    "cost": investment
                                }
                                if not IS_LIVE:
                                    bot_state["virtual_balance"] -= investment
        except Exception as e:
            add_log(f"🚨 Awaria pętli decyzyjnej: {e}")
        time.sleep(4)

# --- PANEL KONTROLNY (WEB SERWER) ---
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): return 

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
                        <p id="ui-profit" class="text-xs mt-2 text-emerald-400 font-semibold">Wynik: $0.00 USDC</p>
                    </div>
                </div>

                <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 mb-8 shadow-xl">
                    <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                        <i class="fa-solid fa-chart-line text-indigo-400"></i> Aktywna Pozycja (Polymarket)
                    </h2>
                    <div id="ui-active-box" class="text-slate-400 py-4 text-center">
                        Brak otwartej pozycji. Bot czeka na sygnał strategii.
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
                        }
                        document.getElementById('ui-sma').innerText = `Średnia SMA: $${data.sma.toLocaleString('en-US', {minimumFractionDigits: 2})}`;
                        
                        const min = data.minutes_left;
                        const sec = data.seconds_remain;
                        document.getElementById('ui-timer').innerText = `${min}m ${sec < 10 ? '0' : ''}${sec}s`;
                        document.getElementById('ui-progress').style.width = `${((900 - ((min * 60) + sec)) / 900) * 100}%`;

                        if (data.current_candle_strike > 0) {
                            document.getElementById('ui-strike').innerText = `$${data.current_candle_strike.toLocaleString('en-US', {minimumFractionDigits: 2})}`;
                            const diff = data.current_price - data.current_candle_strike;
                            const diffEl = document.getElementById('ui-diff');
                            diffEl.className = diff >= 0 ? "text-xs mt-2 text-emerald-400" : "text-xs mt-2 text-rose-400";
                            diffEl.innerText = `Różnica: ${diff >= 0 ? '+' : '-'}$${Math.abs(diff).toFixed(2)}`;
                        }

                        const activeBox = document.getElementById('ui-active-box');
                        if (data.active_trade) {
                            const trade = data.active_trade;
                            activeBox.innerHTML = `
                                <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-left bg-slate-950 p-4 rounded-xl border border-indigo-500/20">
                                    <div><p class="text-xs text-slate-400">KIERUNEK</p><p class="text-lg font-bold text-emerald-400">${trade.direction}</p></div>
                                    <div><p class="text-xs text-slate-400">KURS WEJŚCIA</p><p class="text-lg font-bold text-slate-200">$${trade.entry_price.toFixed(2)}</p></div>
                                    <div><p class="text-xs text-slate-400">KURS BTC OBECNY</p><p class="text-lg font-bold text-slate-200">$${data.current_price.toLocaleString()}</p></div>
                                    <div><p class="text-xs text-slate-400">KOSZT TRANSAKCJI</p><p class="text-lg font-bold text-slate-200">$${trade.cost.toFixed(2)} USDC</p></div>
                                </div>`;
                        } else {
                            activeBox.innerHTML = `<p class="text-slate-500 py-2">Brak otwartej pozycji. Bot czeka na sygnał strategii.</p>`;
                        }

                        if (data.trade_history && data.trade_history.length > 0) {
                            const total = data.trade_history.length;
                            const wins = data.trade_history.filter(t => t.result === 'WIN').length;
                            const pct = ((wins / total) * 100).toFixed(0);
                            document.getElementById('ui-stats').innerText = `${wins} / ${total} (${pct}%)`;
                            
                            let totalProfit = data.trade_history.reduce((sum, t) => sum + t.profit, 0);
                            const profitEl = document.getElementById('ui-profit');
                            profitEl.innerText = `Wynik: ${totalProfit >= 0 ? '+' : ''}$${totalProfit.toFixed(2)} USDC`;
                            profitEl.className = totalProfit >= 0 ? "text-xs mt-2 text-emerald-400 font-semibold" : "text-xs mt-2 text-rose-400 font-semibold";

                            const historyRows = document.getElementById('ui-history-rows');
                            historyRows.innerHTML = data.trade_history.slice().reverse().map(t => `
                                <tr class="border-b border-slate-800/30 hover:bg-slate-900/40 transition">
                                    <td class="py-3 px-3 font-semibold ${t.direction.includes('UP') || t.direction.includes('TP') ? 'text-emerald-400' : 'text-rose-400'}">${t.direction}</td>
                                    <td class="py-3 px-3 text-slate-300">$${t.entry_btc.toLocaleString()}</td>
                                    <td class="py-3 px-3 text-slate-400">$${t.strike_btc.toLocaleString()} vs $${t.exit_btc.toLocaleString()}</td>
                                    <td class="py-3 px-3 font-bold ${t.result === 'WIN' ? 'text-emerald-400' : 'text-rose-400'}">${t.result === 'WIN' ? '+' : ''}$${t.profit.toFixed(2)}</td>
                                </tr>
                            `).join('');
                        }

                        const logsDiv = document.getElementById('ui-logs');
                        if (data.logs.length > 0) {
                            logsDiv.innerHTML = data.logs.slice().reverse().map(l => `<div>${l}</div>`).join('');
                        }
                    } catch (e) { console.error(e); }
                }
                setInterval(updateDashboard, 2000);
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
