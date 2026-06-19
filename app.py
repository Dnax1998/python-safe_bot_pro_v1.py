import time
import requests
import json
import threading
import os
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# =====================================================================
#  USTAWIENIA BOTA (Zarządzanie Ryzykiem i Pozycją - LIVE MODE)
# =====================================================================
FIXED_TRADE_AMOUNT = 20.0    # Stała kwota transakcji w USDC (w trybie LIVE lepiej zacząć od stałej kwoty)

ENABLE_EARLY_EXIT = True     # True = włącza Stop-Loss i Take-Profit w trakcie świecy
STOP_LOSS_PRICE = 0.35       # Sprzedaj udziały, jeśli ich wartość spadnie poniżej 35 centów
TAKE_PROFIT_PRICE = 0.90     # Sprzedaj udziały, jeśli ich wartość wzrośnie do 90 centów

PRICE_MARGIN = 15.0          # Wymagany dystans BTC od SMA (w USD)
STRIKE_MARGIN = 10.0         # Wymagany dystans BTC od ceny Strike (w USD)
# =====================================================================

# --- KONFIGURACJA POLYMARKET CLOB API ---
try:
    from pyclob.client.client import ClobClient
    from pyclob.client.environment import POLYGON_CHAIN_ID
except ImportError:
    print("BŁĄD: Zainstaluj pyclob (pip install pyclob)")
    exit(1)

PRIVATE_KEY = os.environ.get("POLYGON_PRIVATE_KEY")
if not PRIVATE_KEY:
    print("KRYTYCZNY BŁĄD: Brak zmiennej środowiskowej POLYGON_PRIVATE_KEY!")
    # W trybie testowym na lokalnym PC bot się zatrzyma, na Renderze musi mieć ten klucz
    # exit(1) 

HOST_URL = "https://clob.polymarket.com"

# Inicjalizacja Klienta Polymarket
clob_client = None
if PRIVATE_KEY:
    try:
        clob_client = ClobClient(
            host=HOST_URL,
            key=PRIVATE_KEY,
            chain_id=137, # Polygon Mainnet
            signature_type=1
        )
        clob_client.set_api_creds(clob_client.create_or_derive_api_creds())
        print("✅ Pomyślnie zautoryzowano klienta Polymarket CLOB API.")
    except Exception as e:
        print(f"❌ Błąd autoryzacji Polymarket: {e}")

# --- GLOBALNY STAN BOTA ---
bot_state = {
    "session_balance": 0.0,         # Śledzi zysk/stratę z bieżącej sesji
    "current_price": 0.0,
    "sma": 0.0,
    "minutes_left": 0,
    "seconds_remain": 0,
    "current_candle_strike": 0.0,
    "active_trade": None,           # Zapisuje prawdziwe token_id i dane transakcji
    "trade_history": [],
    "logs": []
}

price_history = []
state_lock = threading.RLock()

def add_log(message):
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 50:
            bot_state["logs"].pop(0)

# =====================================================================
#  FUNKCJE API (Ceny i Polymarket)
# =====================================================================

def get_btc_price():
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return float(response.json()['data']['amount'])
    except:
        pass
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return float(response.json()['price'])
    except:
        pass
    return None

def get_current_market_tokens():
    """
    Odpytuje Gamma API Polymarketu, by znaleźć aktualny rynek 15m BTC.
    Zwraca token_id dla YES (UP) i NO (DOWN).
    UWAGA: Struktura zapytania może wymagać dostosowania do dokładnego slug'a rynków 15m.
    """
    try:
        # Przykładowe zapytanie do Gamma API w poszukiwaniu rynków BTC (Należy zweryfikować endpoint tagów dla 15m)
        url = "https://gamma-api.polymarket.com/events?limit=10&active=true&tag_id=bitcoin"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            events = response.json()
            # Szukamy eventu, którego tytuł wskazuje na krótkoterminowy rynek, np. "BTC price at..."
            for event in events:
                if "BTC" in event.get("title", "") and "price" in event.get("title", ""):
                    markets = event.get("markets", [])
                    if markets:
                        market = markets[0]
                        tokens = market.get("clobTokenIds", [])
                        if len(tokens) >= 2:
                            # Zazwyczaj index 0 to YES (UP), index 1 to NO (DOWN)
                            return {"UP": tokens[0], "DOWN": tokens[1]}
        return None
    except Exception as e:
        add_log(f"⚠️ Błąd pobierania ID rynku: {e}")
        return None

def execute_trade(token_id, side, price, size):
    """
    Wysyła prawdziwe zlecenie na blockchain (Polygon).
    side: 'BUY' lub 'SELL'
    price: float (np. 0.55)
    size: float (ilość tokenów)
    """
    if not clob_client:
        add_log("⚠️ Tryb offline: Brak autoryzacji Polymarket. Transakcja pominięta.")
        return False

    try:
        order_args = {
            "token_id": token_id,
            "price": round(price, 3),
            "side": side,
            "size": round(size, 2),
            "fee_rate_bps": 0
        }
        order = clob_client.create_order(**order_args)
        response = clob_client.post_order(order)
        
        if response and response.get('success'):
            add_log(f"✅ Zlecenie zrealizowane! {side} | P: ${price} | Ilość: {size}")
            return True
        else:
            add_log(f"❌ Błąd odrzucenia przez giełdę: {response.get('errorMsg', 'Nieznany błąd')}")
            return False
    except Exception as e:
        add_log(f"❌ Krytyczny błąd wykonania zlecenia: {e}")
        return False

# =====================================================================
#  GŁÓWNA LOGIKA HANDLOWA
# =====================================================================

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

        # ROZSTRZYGNIĘCIE NA KOŃCU ŚWIECY
        if minutes_passed == 14 and seconds_passed >= 55:
            if bot_state["active_trade"]:
                trade = bot_state["active_trade"]
                strike = trade["strike_price"]
                direction = trade["direction"]
                
                won = (direction == "UP" and current_price > strike) or (direction == "DOWN" and current_price < strike)
                cost = trade["cost"]
                
                if won:
                    payout = 1.0 * trade["amount_shares"]
                    profit = payout - cost
                    bot_state["session_balance"] += profit
                    status = "WYGRANA"
                    add_log(f"🎉 Sukces (Koniec Czasu)! Zysk: +${profit:.2f} USDC")
                else:
                    profit = -cost
                    bot_state["session_balance"] += profit
                    status = "PRZEGRANA"
                    add_log(f"📉 Porażka. Transakcja stratna: -${cost:.2f} USDC")
                
                trade["exit_price"] = current_price
                trade["status"] = status
                trade["profit"] = profit
                trade["closed_at"] = now.strftime("%H:%M:%S")
                
                bot_state["trade_history"].append(trade)
                bot_state["active_trade"] = None

def run_trading_strategy():
    add_log("System LIVE (Polymarket) uruchomiony pomyślnie.")
    
    init_price = get_btc_price()
    if init_price:
        with state_lock:
            bot_state["current_candle_strike"] = init_price
            bot_state["current_price"] = init_price
    
    error_count = 0
    while True:
        try:
            current_price = get_btc_price()
            if not current_price:
                error_count += 1
                if error_count % 6 == 0:
                    add_log("❌ Problem z pobraniem ceny BTC.")
                time.sleep(5)
                continue
            
            error_count = 0
            update_candle_logic(current_price)
            
            with state_lock:
                m_left = bot_state["minutes_left"]
                active = bot_state["active_trade"]
                sma = bot_state["sma"]
                strike = bot_state["current_candle_strike"]

            # -----------------------------------------------------------------
            # ZAMYKANIE POZYCJI W TRAKCIE ŚWIECY (LIVE SELL ORDER)
            # -----------------------------------------------------------------
            if active and ENABLE_EARLY_EXIT:
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

                # STOP-LOSS (Sprzedaż tokenów)
                if sim_share_price <= STOP_LOSS_PRICE:
                    add_log(f"🛡️ Aktywacja STOP-LOSS. Próba sprzedaży tokenów...")
                    success = execute_trade(active["token_id"], "SELL", sim_share_price, active["amount_shares"])
                    
                    if success:
                        recovered_amount = active["amount_shares"] * sim_share_price
                        loss = recovered_amount - active["cost"]
                        with state_lock:
                            bot_state["session_balance"] += loss
                            active["status"] = "STOP LOSS"
                            active["profit"] = loss
                            active["exit_price"] = current_price
                            bot_state["trade_history"].append(active)
                            bot_state["active_trade"] = None
                    time.sleep(5)
                    continue

                # TAKE-PROFIT (Sprzedaż tokenów)
                elif sim_share_price >= TAKE_PROFIT_PRICE:
                    add_log(f"💰 Aktywacja TAKE-PROFIT. Próba zabezpieczenia zysku...")
                    success = execute_trade(active["token_id"], "SELL", sim_share_price, active["amount_shares"])
                    
                    if success:
                        secured_amount = active["amount_shares"] * sim_share_price
                        profit = secured_amount - active["cost"]
                        with state_lock:
                            bot_state["session_balance"] += profit
                            active["status"] = "TAKE PROFIT"
                            active["profit"] = profit
                            active["exit_price"] = current_price
                            bot_state["trade_history"].append(active)
                            bot_state["active_trade"] = None
                    time.sleep(5)
                    continue

            # -----------------------------------------------------------------
            # OTWIERANIE NOWYCH POZYCJI (LIVE BUY ORDER)
            # -----------------------------------------------------------------
            if 5 <= m_left <= 10 and not active and sma > 0 and strike > 0:
                price_diff = current_price - strike
                investment = FIXED_TRADE_AMOUNT
                tokens_market = None # Pobierane tylko wtedy, gdy sygnał zostanie aktywowany

                # Scenariusz 1: Trend wzrostowy (UP / YES)
                if current_price > sma + PRICE_MARGIN and price_diff > STRIKE_MARGIN:
                    share_price = min(0.90, max(0.55, 0.50 + (price_diff / 100)))
                    shares = investment / share_price
                    
                    tokens_market = get_current_market_tokens()
                    if tokens_market and "UP" in tokens_market:
                        token_id = tokens_market["UP"]
                        add_log(f"🛒 Próba zakupu YES(UP). Cel: ${share_price:.2f}")
                        
                        if execute_trade(token_id, "BUY", share_price, shares):
                            with state_lock:
                                bot_state["active_trade"] = {
                                    "direction": "UP",
                                    "token_id": token_id,
                                    "entry_price": share_price,
                                    "strike_price": strike,
                                    "btc_at_entry": current_price,
                                    "amount_shares": shares,
                                    "cost": investment
                                }

                # Scenariusz 2: Trend spadkowy (DOWN / NO)
                elif current_price < sma - PRICE_MARGIN and price_diff < -STRIKE_MARGIN:
                    share_price = min(0.90, max(0.55, 0.50 + (abs(price_diff) / 100)))
                    shares = investment / share_price
                    
                    tokens_market = get_current_market_tokens()
                    if tokens_market and "DOWN" in tokens_market:
                        token_id = tokens_market["DOWN"]
                        add_log(f"🛒 Próba zakupu NO(DOWN). Cel: ${share_price:.2f}")
                        
                        if execute_trade(token_id, "BUY", share_price, shares):
                            with state_lock:
                                bot_state["active_trade"] = {
                                    "direction": "DOWN",
                                    "token_id": token_id,
                                    "entry_price": share_price,
                                    "strike_price": strike,
                                    "btc_at_entry": current_price,
                                    "amount_shares": shares,
                                    "cost": investment
                                }

        except Exception as e:
            add_log(f"🚨 Nieoczekiwany błąd pętli: {e}")
            
        time.sleep(5)

# --- PANEL KONTROLNY ---
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
            <title>Krajekis LIVE Bot</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');
                body { font-family: 'Plus Jakarta Sans', sans-serif; }
            </style>
        </head>
        <body class="bg-slate-950 text-slate-100 min-h-screen">
            <div class="max-w-7xl mx-auto px-4 py-8">
                <div class="flex flex-col md:flex-row md:items-center md:justify-between border-b border-rose-800/50 pb-6 mb-8 gap-4">
                    <div>
                        <div class="flex items-center gap-3">
                            <span class="flex h-3 w-3 relative">
                                <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-rose-400 opacity-75"></span>
                                <span class="relative inline-flex rounded-full h-3 w-3 bg-rose-600"></span>
                            </span>
                            <h1 class="text-2xl font-bold tracking-tight text-white">Krajekis LIVE (Polymarket)</h1>
                        </div>
                        <p class="text-sm text-slate-400 mt-1">Automatyczny system egzekucji na głównym blockchainie Polygon</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800 rounded-xl px-5 py-3 flex items-center gap-4">
                        <div>
                            <p class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Wynik Sesji</p>
                            <p id="ui-balance" class="text-xl font-bold text-white">$0.00 USDC</p>
                        </div>
                        <div class="p-2 bg-rose-500/10 rounded-lg">
                            <i class="fa-solid fa-fire text-rose-500 text-lg"></i>
                        </div>
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Aktualna cena BTC</p>
                        <p id="ui-price" class="text-2xl font-extrabold mt-2 text-white">Wczytywanie...</p>
                        <p id="ui-sma" class="text-xs text-slate-500 mt-2">Średnia SMA: --</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Czas do końca świecy</p>
                        <p id="ui-timer" class="text-2xl font-extrabold mt-2 text-amber-400">Wczytywanie...</p>
                    </div>
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl">
                        <p class="text-sm font-medium text-slate-400">Cena Strike</p>
                        <p id="ui-strike" class="text-2xl font-extrabold mt-2 text-slate-200">Wczytywanie...</p>
                        <p id="ui-diff" class="text-xs mt-2">Różnica: --</p>
                    </div>
                </div>

                <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 mb-8 shadow-xl">
                    <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                        <i class="fa-solid fa-bolt text-amber-400"></i> Aktywna Pozycja
                    </h2>
                    <div id="ui-active-box" class="text-slate-400 py-4 text-center">
                        Brak otwartej pozycji.
                    </div>
                </div>

                <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl flex flex-col h-[400px]">
                        <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                            <i class="fa-solid fa-terminal text-emerald-400"></i> Logi Systemowe
                        </h2>
                        <div id="ui-logs" class="bg-slate-950 p-4 rounded-xl font-mono text-xs text-emerald-400/90 overflow-y-auto flex-1 space-y-1.5 border border-slate-800/40"></div>
                    </div>
                    <div class="bg-slate-900 border border-slate-800/80 rounded-2xl p-6 shadow-xl flex flex-col h-[400px]">
                        <h2 class="text-lg font-bold mb-4 flex items-center gap-2 text-white">
                            <i class="fa-solid fa-history text-indigo-400"></i> Historia LIVE
                        </h2>
                        <div class="overflow-y-auto flex-1">
                            <table class="w-full text-left text-sm">
                                <tbody id="ui-history-rows" class="divide-y divide-slate-800/40">
                                    <tr><td class="py-6 text-center text-slate-500">Pusto</td></tr>
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

                        const balEl = document.getElementById('ui-balance');
                        balEl.innerText = (data.session_balance >= 0 ? '+' : '') + '$' + data.session_balance.toFixed(2) + ' USDC';
                        balEl.className = data.session_balance >= 0 ? "text-xl font-bold text-emerald-400" : "text-xl font-bold text-rose-400";
                        
                        if (data.current_price > 0) document.getElementById('ui-price').innerText = '$' + data.current_price.toLocaleString();
                        document.getElementById('ui-sma').innerText = 'Średnia SMA: $' + data.sma.toLocaleString();
                        
                        document.getElementById('ui-timer').innerText = data.minutes_left + 'm ' + data.seconds_remain + 's';
                        
                        if (data.current_candle_strike > 0) {
                            document.getElementById('ui-strike').innerText = '$' + data.current_candle_strike.toLocaleString();
                            const diff = data.current_price - data.current_candle_strike;
                            document.getElementById('ui-diff').innerText = 'Różnica: ' + (diff >= 0 ? '+$' : '-$') + Math.abs(diff).toFixed(2);
                        }

                        const activeBox = document.getElementById('ui-active-box');
                        if (data.active_trade) {
                            const trade = data.active_trade;
                            activeBox.innerHTML = `
                                <div class="grid grid-cols-2 gap-4 text-left bg-slate-950 p-4 rounded-xl border border-rose-500/30">
                                    <div><p class="text-xs text-slate-400">KIERUNEK</p><p class="text-lg font-bold text-white">` + trade.direction + `</p></div>
                                    <div><p class="text-xs text-slate-400">KOSZT / UDZIAŁY</p><p class="text-lg font-bold text-white">$` + trade.cost.toFixed(2) + ` / ` + trade.amount_shares.toFixed(1) + `</p></div>
                                </div>
                            `;
                        } else {
                            activeBox.innerHTML = `<p class="text-slate-500">Czekam na sygnał...</p>`;
                        }

                        const logsDiv = document.getElementById('ui-logs');
                        logsDiv.innerHTML = data.logs.slice().reverse().map(l => '<div>' + l + '</div>').join('');

                        const historyRows = document.getElementById('ui-history-rows');
                        if (data.trade_history.length > 0) {
                            historyRows.innerHTML = data.trade_history.slice().reverse().map(t => {
                                const profitColor = t.profit >= 0 ? 'text-emerald-400' : 'text-rose-400';
                                return `<tr class="border-b border-slate-800/30">
                                    <td class="py-3 px-3 font-semibold text-white">` + t.direction + `</td>
                                    <td class="py-3 px-3 font-bold ` + profitColor + `">` + t.status + ` (` + (t.profit >= 0 ? '+' : '') + `$` + t.profit.toFixed(2) + `)</td>
                                </tr>`;
                            }).join('');
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
    bot_thread = threading.Thread(target=run_trading_strategy)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    add_log(f"Serwer Dashboard wystartował na porcie {port}")
    server.serve_forever()
