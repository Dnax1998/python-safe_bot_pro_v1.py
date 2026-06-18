import time
import requests
import json
import threading
import os
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Importy do obsługi prawdziwego handlu
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import OrderArgs

# =====================================================================
#  USTAWIENIA BOTA
# =====================================================================
USE_DYNAMIC_RISK = True      
RISK_PERCENT = 2.0           
FIXED_TRADE_AMOUNT = 10.0    

ENABLE_EARLY_EXIT = True     
STOP_LOSS_PRICE = 0.35       
TAKE_PROFIT_PRICE = 0.90     

PRICE_MARGIN = 15.0          
STRIKE_MARGIN = 10.0         

active_market_info = {
    "token_id_up": None,
    "token_id_down": None,
    "title": "Wyszukiwanie aktywnego rynku..."
}
# =====================================================================

bot_state = {
    "virtual_balance": 0.0,         
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

def add_log(message):
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 50:
            bot_state["logs"].pop(0)

def auto_discover_btc_tokens():
    global active_market_info
    try:
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&q=Bitcoin"
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            markets = r.json()
            for m in markets:
                title = m.get("title", "")
                tokens = m.get("clobTokenIds")
                if tokens and len(tokens) >= 2 and "above" in title.lower() and "15m" in title.lower():
                    token_up = tokens[0]   
                    token_down = tokens[1] 
                    if active_market_info["token_id_up"] != token_up:
                        active_market_info["token_id_up"] = token_up
                        active_market_info["token_id_down"] = token_down
                        active_market_info["title"] = title
                        add_log(f"🎯 WYKRYTO RYNEK: {title}")
                        add_log(f"   ↳ Token UP (YES): {token_up[:15]}...")
                        add_log(f"   ↳ Token DOWN (NO): {token_down[:15]}...")
                    return
    except Exception as e:
        pass

def update_real_balance():
    """Odczytuje saldo przez oficjalne API Polygonscan z logowaniem błędów"""
    global poly_client
    if not poly_client: 
        add_log("⚠️ Oczekiwanie na inicjalizację klienta (poly_client to None).")
        return

    POLY_ADDRESS = os.environ.get("POLY_ADDRESS", "").strip()
    target_address = POLY_ADDRESS if POLY_ADDRESS else poly_client.get_address()
    
    POLYGONSCAN_API_KEY = os.environ.get("POLYGONSCAN_API_KEY", "").strip()
    api_key_param = f"&apikey={POLYGONSCAN_API_KEY}" if POLYGONSCAN_API_KEY else ""

    # Podstawowe tokeny Polymarketu na sieci Polygon (USDC.e / pUSD)
    tokens = [
        "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb", # Nowy token Polymarket pUSD
        "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", # USDC.e
        "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # Native USDC
    ]

    try:
        clob_collat = poly_client.get_collateral_address()
        if clob_collat and clob_collat not in tokens:
            tokens.insert(0, clob_collat)
    except Exception as e:
        add_log(f"⚠️ Nie udało się pobrać adresu kolaterali: {e}")

    total_balance = 0.0

    # METODA 1: Oficjalne API Polygonscan
    try:
        for token in tokens:
            url = f"https://api.polygonscan.com/api?module=account&action=tokenbalance&contractaddress={token}&address={target_address}&tag=latest{api_key_param}"
            res = requests.get(url, timeout=5)
            data = res.json()
            if data.get("status") == "1":
                bal = float(data["result"]) / 10**6
                if bal > 0:
                    total_balance += bal
            else:
                msg = data.get("message", "")
                if "NOTOK" in msg:
                    add_log(f"⚠️ Polygonscan Limit/Błąd: {data.get('result', msg)}")
    except Exception as e:
        add_log(f"⚠️ Błąd Metody 1 (Polygonscan): {e}")

    # METODA 2: Fallback do standardowych RPC
    if total_balance == 0.0:
        rpcs = ["https://polygon-rpc.com", "https://rpc.ankr.com/polygon"]
        for rpc in rpcs:
            if total_balance > 0: break
            try:
                for token in tokens:
                    payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": token, "data": "0x70a08231" + target_address.replace("0x", "").zfill(64)}, "latest"], "id": 1}
                    res = requests.post(rpc, json=payload, timeout=5)
                    if res.status_code == 200:
                        val_hex = res.json().get("result", "0x0")
                        if val_hex and val_hex != "0x":
                            bal = int(val_hex, 16) / 10**6
                            if bal > 0: total_balance += bal
            except Exception as e:
                continue

    with state_lock:
        bot_state["virtual_balance"] = total_balance

def init_mainnet_client():
    global poly_client
    POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "").strip()
    POLY_ADDRESS = os.environ.get("POLY_ADDRESS", "").strip()
    
    if POLY_PRIVATE_KEY:
        try:
            client_kwargs = {
                "host": "https://clob.polymarket.com",
                "key": POLY_PRIVATE_KEY.replace("0x", ""),
                "chain_id": POLYGON
            }
            if POLY_ADDRESS:
                client_kwargs["funder"] = POLY_ADDRESS
                try:
                    from py_clob_client.clob_types import SignatureType
                    client_kwargs["signature_type"] = SignatureType.POLY_GNOSIS_SAFE 
                except: pass
            
            poly_client = ClobClient(**client_kwargs)
            
            if POLY_ADDRESS:
                add_log(f"✅ MAINNET: Skonfigurowano konto Gmail/Proxy: {POLY_ADDRESS}")
            else:
                add_log(f"✅ MAINNET: Zalogowano standardowo na: {poly_client.get_address()}")
            
            update_real_balance()
            add_log(f"💰 MAINNET: Pobrano saldo startowe: {bot_state['virtual_balance']:.2f} USDC")
                
        except Exception as e:
            add_log(f"🚨 BŁĄD MAINNET: {e}")

def get_btc_price():
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200: return float(res.json()['price'])
    except: pass
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200: return float(res.json()['data']['amount'])
    except: pass
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
        if len(price_history) > 30: price_history.pop(0)
        bot_state["sma"] = sum(price_history) / len(price_history)

        if minutes_passed == 0 and seconds_passed < 10:
            if bot_state["current_candle_strike"] != current_price:
                bot_state["current_candle_strike"] = current_price
                add_log(f"🆕 Rozpoczęcie nowej świecy 15m. Strike: ${current_price:,.2f}")

        if minutes_passed == 14 and seconds_passed >= 55:
            if bot_state["active_trade"]:
                trade = bot_state["active_trade"]
                strike = trade["strike_price"]
                direction = trade["direction"]
                
                won = False
                if direction == "UP" and current_price > strike: won = True
                elif direction == "DOWN" and current_price < strike: won = True
                
                cost = trade["entry_price"] * trade["amount_shares"]
                if won:
                    payout = 1.0 * trade["amount_shares"]
                    profit = payout - cost
                    add_log(f"🎉 Polymarket: Wygrana. Zysk: +${profit:.2f} USDC")
                    trade["status"] = "WYGRANA"
                else:
                    profit = -cost
                    add_log(f"📉 Polymarket: Przegrana. Strata: -${cost:.2f} USDC")
                    trade["status"] = "PRZEGRANA"
                
                trade["exit_price"] = current_price
                trade["profit"] = profit
                trade["closed_at"] = now.strftime("%H:%M:%S")
                bot_state["trade_history"].append(trade)
                bot_state["active_trade"] = None
                update_real_balance()

def run_trading_strategy():
    add_log("System uruchomiony pomyślnie.")
    init_mainnet_client()
    
    init_price = get_btc_price()
    if init_price:
        with state_lock:
            bot_state["current_candle_strike"] = init_price
            bot_state["current_price"] = init_price
        add_log(f"🟢 Pobrano cenę BTC: ${init_price:,.2f}")

    while True:
        try:
            auto_discover_btc_tokens()
            update_real_balance()
            
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
                balance = bot_state["virtual_balance"]

            # --- ZAMYKANIE POZYCJI ---
            if active and ENABLE_EARLY_EXIT:
                price_diff = current_price - active["strike_price"]
                vd = 5.0 + (m_left * 2.0)
                try:
                    if active["direction"] == "UP": sim_price = 1.0 / (1.0 + math.exp(-price_diff / vd))
                    else: sim_price = 1.0 / (1.0 + math.exp(price_diff / vd))
                    sim_price = min(0.98, max(0.02, sim_price))
                except:
                    sim_price = 0.98 if price_diff > 0 else 0.02

                if sim_price <= STOP_LOSS_PRICE or sim_price >= TAKE_PROFIT_PRICE:
                    atype = "TAKE PROFIT" if sim_price >= TAKE_PROFIT_PRICE else "STOP LOSS"
                    recovered = active["amount_shares"] * sim_price
                    profit = recovered - active["cost"]
                    
                    if poly_client and active["token_id"]:
                        try:
                            order = OrderArgs(price=round(sim_price, 2), size=round(active["amount_shares"], 2), side="sell", token_id=active["token_id"])
                            poly_client.post_order(poly_client.create_order(order))
                            add_log(f"📡 Wysłano zlecenie sprzedaży...")
                        except Exception as e:
                            add_log(f"🚨 BŁĄD SPRZEDAŻY: {e}")

                    with state_lock:
                        active["status"] = atype
                        active["profit"] = profit
                        active["exit_price"] = current_price
                        active["closed_at"] = datetime.utcnow().strftime("%H:%M:%S")
                        bot_state["trade_history"].append(active)
                        bot_state["active_trade"] = None
                    
                    add_log(f"⚡ [{atype}] Zamknięto. Wynik: {profit:.2f} USDC.")
                    update_real_balance()
                    time.sleep(5)
                    continue

            # --- OTWIERANIE POZYCJI ---
            if 5 <= m_left <= 10 and not active and sma > 0 and strike > 0:
                price_diff = current_price - strike
                investment = min(balance, max(2.0, (balance * RISK_PERCENT) / 100.0)) if USE_DYNAMIC_RISK else min(balance, FIXED_TRADE_AMOUNT)

                if investment >= 2.0 and active_market_info["token_id_up"] and active_market_info["token_id_down"]:
                    
                    if current_price > sma + PRICE_MARGIN and price_diff > STRIKE_MARGIN:
                        share_price = min(0.90, max(0.55, 0.50 + (price_diff / 100)))
                        shares = investment / share_price
                        tid = active_market_info["token_id_up"]
                        
                        if poly_client:
                            try:
                                order = OrderArgs(price=round(share_price, 2), size=round(shares, 2), side="buy", token_id=tid)
                                poly_client.post_order(poly_client.create_order(order))
                                with state_lock:
                                    bot_state["active_trade"] = {"direction": "UP", "token_id": tid, "entry_price": share_price, "strike_price": strike, "btc_at_entry": current_price, "amount_shares": shares, "cost": investment, "opened_at": datetime.utcnow().strftime("%H:%M:%S")}
                                add_log(f"🛒 KUPIONO UP: {investment:.2f} USDC po ${share_price:.2f}")
                                update_real_balance()
                            except Exception as e: add_log(f"🚨 BŁĄD KUPNA UP: {e}")

                    elif current_price < sma - PRICE_MARGIN and price_diff < -STRIKE_MARGIN:
                        share_price = min(0.90, max(0.55, 0.50 + (abs(price_diff) / 100)))
                        shares = investment / share_price
                        tid = active_market_info["token_id_down"]
                        
                        if poly_client:
                            try:
                                order = OrderArgs(price=round(share_price, 2), size=round(shares, 2), side="buy", token_id=tid)
                                poly_client.post_order(poly_client.create_order(order))
                                with state_lock:
                                    bot_state["active_trade"] = {"direction": "DOWN", "token_id": tid, "entry_price": share_price, "strike_price": strike, "btc_at_entry": current_price, "amount_shares": shares, "cost": investment, "opened_at": datetime.utcnow().strftime("%H:%M:%S")}
                                add_log(f"🛒 KUPIONO DOWN: {investment:.2f} USDC po ${share_price:.2f}")
                                update_real_balance()
                            except Exception as e: add_log(f"🚨 BŁĄD KUPNA DOWN: {e}")

        except Exception as e:
            pass
        time.sleep(5)

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
                            <h1 class="text-2xl font-bold tracking-tight text-white">Krajekis Bot Panel (LIVE)</h1>
                        </div>
                        <p class="text-sm text-slate-400 mt-1">System operujący na prawdziwych środkach (Mainnet USDC/pUSD)</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800 rounded-xl px-5 py-3 flex items-center gap-4">
                        <div>
                            <p class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Realne Saldo USDC</p>
                            <p id="ui-balance" class="text-xl font-bold text-emerald-400">Pobieranie...</p>
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
                        <i class="fa-solid fa-chart-line text-indigo-400"></i> Aktywna Pozycja (Mainnet)
                    </h2>
                    <div id="ui-active-box" class="text-slate-400 py-4 text-center">
                        Brak otwartej pozycji. Bot czeka na optymalne warunki.
                    </div>
                </div>

                <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl flex flex-col h-[400px]">
                        <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                            <i class="fa-solid fa-terminal text-emerald-400"></i> Konsola Bota na żywo
                        </h2>
                        <div id="ui-logs" class="bg-slate-950 p-4 rounded-xl font-mono text-xs text-emerald-400/90 overflow-y-auto flex-1 space-y-1.5 border border-slate-800/40">
                            Poczekaj...
                        </div>
                    </div>

                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl flex flex-col h-[400px]">
                        <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                            <i class="fa-solid fa-history text-indigo-400"></i> Historia Transakcji (Live)
                        </h2>
                        <div class="overflow-y-auto flex-1">
                            <table class="w-full text-left text-sm">
                                <thead class="text-xs text-slate-400 uppercase bg-slate-950/40 sticky top-0">
                                    <tr>
                                        <th class="py-2.5 px-3">Kierunek</th>
                                        <th class="py-2.5 px-3">Wejście</th>
                                        <th class="py-2.5 px-3">Zakończenie</th>
                                        <th class="py-2.5 px-3">Wynik PnL</th>
                                    </tr>
                                </thead>
                                <tbody id="ui-history-rows" class="divide-y divide-slate-800/40">
                                    <tr><td colspan="4" class="py-6 text-center text-slate-500">Brak zamkniętych transakcji</td></tr>
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

                        document.getElementById('ui-balance').innerText = data.virtual_balance.toFixed(2) + ' USDC';
                        
                        if (data.current_price > 0) document.getElementById('ui-price').innerText = '$' + data.current_price.toLocaleString('en-US', {minimumFractionDigits: 2});
                        document.getElementById('ui-sma').innerText = 'Średnia SMA (30 okresów): $' + data.sma.toLocaleString('en-US', {minimumFractionDigits: 2});
                        
                        const min = data.minutes_left; const sec = data.seconds_remain;
                        document.getElementById('ui-timer').innerText = min + 'm ' + (sec < 10 ? '0' : '') + sec + 's';
                        document.getElementById('ui-progress').style.width = (((900 - (min * 60 + sec)) / 900) * 100) + '%';

                        if (data.current_candle_strike > 0) {
                            document.getElementById('ui-strike').innerText = '$' + data.current_candle_strike.toLocaleString('en-US', {minimumFractionDigits: 2});
                            const diff = data.current_price - data.current_candle_strike;
                            const diffEl = document.getElementById('ui-diff');
                            diffEl.className = diff >= 0 ? "text-xs mt-2 text-emerald-400" : "text-xs mt-2 text-rose-400";
                            diffEl.innerText = 'Różnica: ' + (diff >= 0 ? '+$' : '-$') + Math.abs(diff).toFixed(2);
                        }

                        const activeBox = document.getElementById('ui-active-box');
                        if (data.active_trade) {
                            const t = data.active_trade;
                            activeBox.innerHTML = `<div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-left bg-slate-950 p-4 rounded-xl border border-indigo-500/20">
                                <div><p class="text-xs text-slate-400">KIERUNEK</p><p class="text-lg font-bold ${t.direction === 'UP' ? 'text-emerald-400' : 'text-rose-400'}">${t.direction}</p></div>
                                <div><p class="text-xs text-slate-400">KURS WEJŚCIA</p><p class="text-lg font-bold text-slate-200">$${t.entry_price.toFixed(2)}</p></div>
                                <div><p class="text-xs text-slate-400">KURS BTC STARTOWY</p><p class="text-lg font-bold text-slate-200">$${t.btc_at_entry.toLocaleString()}</p></div>
                                <div><p class="text-xs text-slate-400">UDZIAŁY / KOSZT</p><p class="text-lg font-bold text-slate-200">${t.amount_shares.toFixed(1)} / $${t.cost.toFixed(2)} USDC</p></div>
                            </div>`;
                        } else activeBox.innerHTML = `<p class="text-slate-500 py-2">Brak otwartej pozycji. Bot czeka na wejście.</p>`;

                        const logsDiv = document.getElementById('ui-logs');
                        logsDiv.innerHTML = data.logs.length > 0 ? data.logs.slice().reverse().map(l => `<div>${l}</div>`).join('') : '<div class="text-slate-500">...</div>';

                        const historyRows = document.getElementById('ui-history-rows');
                        if (data.trade_history.length > 0) {
                            let w = 0, p = 0;
                            historyRows.innerHTML = data.trade_history.slice().reverse().map(t => {
                                if (t.status === "WYGRANA" || t.status === "TAKE PROFIT") w++;
                                p += t.profit;
                                return `<tr class="border-b border-slate-800/30">
                                    <td class="py-3 px-3 font-semibold ${t.direction === 'UP' ? 'text-emerald-400' : 'text-rose-400'}">${t.direction}</td>
                                    <td class="py-3 px-3">$${t.entry_price.toFixed(2)}</td>
                                    <td class="py-3 px-3 text-xs text-slate-400">$${t.exit_price.toLocaleString()}</td>
                                    <td class="py-3 px-3 font-bold ${t.profit >= 0 ? 'text-emerald-400' : 'text-rose-400'}">${t.status} (${t.profit >= 0 ? '+' : ''}$${t.profit.toFixed(2)})</td>
                                </tr>`;
                            }).join('');
                            document.getElementById('ui-stats').innerText = `${w} / ${data.trade_history.length} (${((w / data.trade_history.length) * 100).toFixed(0)}%)`;
                            document.getElementById('ui-profit').innerText = `Wynik całkowity: ${p >= 0 ? '+' : ''}$${p.toFixed(2)} USDC`;
                            document.getElementById('ui-profit').className = p >= 0 ? 'text-xs mt-2 text-emerald-400 font-semibold' : 'text-xs mt-2 text-rose-400 font-semibold';
                        }
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
    threading.Thread(target=run_trading_strategy, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get("PORT", 10000))), DashboardHandler)
    server.serve_forever()
