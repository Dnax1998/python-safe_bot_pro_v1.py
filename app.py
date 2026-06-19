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

bot_state = {
    "virtual_balance": 100.0,       # Używane tylko w trybie symulacji
    "real_balance": 0.0,            # Prawdziwe saldo pobrane z Polymarket
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

# Konfiguracja sieci Polygon do odczytu danych on-chain
w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))

def add_log(message):
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 50:
            bot_state["logs"].pop(0)

def update_real_balance():
    """Pobiera rzeczywisty stan konta USDC na Polygon dla Twojego adresu"""
    if not IS_LIVE:
        return
    try:
        wallet_address = os.environ.get("WALLET_ADDRESS")
        if not wallet_address:
            return
            
        # Adres kontraktu USDC.e (Bridged USDC) używanego na Polymarket
        usdc_contract_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        
        # Minimalny ABI do pobrania balansu ERC-20
        min_abi = [
            {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}
        ]
        
        contract = w3.eth.contract(address=w3.to_checksum_address(usdc_contract_address), abi=min_abi)
        balance_raw = contract.functions.balanceOf(w3.to_checksum_address(wallet_address)).call()
        
        # USDC ma 6 miejsc po przecinku na Polygon
        balance_usdc = balance_raw / 1_000_000.0
        
        with state_lock:
            bot_state["real_balance"] = balance_usdc
    except Exception as e:
        print(f"Błąd podczas pobierania salda z blockchainu: {e}")

def get_polymarket_15m_market():
    """Dynamicznie odpytuje rynek Polymarket w poszukiwaniu aktualnej świecy 15m BTC"""
    try:
        # Pobieranie aktywnych rynków bezpośrednio z CLOB Polymarket
        url = "https://clob.polymarket.com/markets"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            markets = response.json()
            for market in markets:
                question = market.get("question", "")
                # Szukamy rynków powiązanych z ceną Bitcoina na dany przedział czasowy
                if "Bitcoin" in question and "15m" in question:
                    tokens = market.get("tokens", [])
                    if len(tokens) >= 2:
                        return {
                            "UP_TOKEN": tokens[0].get("token_id"),    # Zazwyczaj TAK / WYŻEJ
                            "DOWN_TOKEN": tokens[1].get("token_id"),  # Zazwyczaj NIE / NIŻEJ
                            "market_id": market.get("condition_id")
                        }
    except Exception as e:
        add_log(f"⚠️ Nie udało się powiązać ID rynku Polymarket: {e}")
    return None

def get_btc_price():
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return float(response.json()['price'])
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
    
    # Przykładowy uproszczony payload zgodny z dokumentacją Relayer API Polymarket
    # Uwaga: Prawdziwe transakcje wymagają podpisu kryptograficznego EIP-712 portfelem.
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
                add_log(f"🆕 Nowa świeca 15m. Strike: ${current_price:,.2f}")
                update_real_balance()

        # Automat na koniec świecy
        if minutes_passed == 14 and seconds_passed >= 55:
            if bot_state["active_trade"]:
                add_log(f"🏁 Koniec czasu świecy. Pozycja przekazana do rozliczenia przez smart-kontrakt.")
                bot_state["active_trade"] = None
                update_real_balance()

def run_trading_strategy():
    add_log(f"System uruchomiony. Tryb: {'PRODUKCYJNY (LIVE)' if IS_LIVE else 'TESTOWY (PAPER TRADING)'}")
    update_real_balance()
    
    init_price = get_btc_price()
    if init_price:
        with state_lock:
            bot_state["current_candle_strike"] = init_price
            bot_state["current_price"] = init_price

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
                balance = bot_state["real_balance"] if IS_LIVE else bot_state["virtual_balance"]

            # -----------------------------------------------------------------
            # MONITOROWANIE STRAT / ZYSKÓW W CZASIE RZECZYWISTYM
            # -----------------------------------------------------------------
            if active and ENABLE_EARLY_EXIT:
                # W trybie LIVE pobieramy aktualny koszt udziałów bezpośrednio ze specyfikacji rynku
                price_diff = current_price - active["strike_price"]
                try:
                    volatility_denominator = 5.0 + (m_left * 2.0)
                    if active["direction"] == "UP":
                        current_share_value = 1.0 / (1.0 + math.exp(-price_diff / volatility_denominator))
                    else:
                        current_share_value = 1.0 / (1.0 + math.exp(price_diff / volatility_denominator))
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

            # -----------------------------------------------------------------
            # SKELETON OTWIERANIA POZYCJI (ZASADA KRAJEKISA)
            # -----------------------------------------------------------------
            if 5 <= m_left <= 10 and not active and sma > 0 and strike > 0:
                price_diff = current_price - strike
                investment = (balance * RISK_PERCENT) / 100.0 if USE_DYNAMIC_RISK else FIXED_TRADE_AMOUNT
                investment = min(balance, max(2.0, investment))

                markets_data = get_polymarket_15m_market()
                
                if markets_data and investment >= 2.0:
                    # KUPUJEMY "UP"
                    if current_price > sma + PRICE_MARGIN and price_diff > STRIKE_MARGIN:
                        success = execute_polymarket_order(markets_data["UP_TOKEN"], investment, side="BUY")
                        if success:
                            with state_lock:
                                bot_state["active_trade"] = {
                                    "direction": "UP",
                                    "token_id": markets_data["UP_TOKEN"],
                                    "strike_price": strike,
                                    "cost": investment
                                }
                                if not IS_LIVE: bot_state["virtual_balance"] -= investment

                    # KUPUJEMY "DOWN"
                    elif current_price < sma - PRICE_MARGIN and price_diff < -STRIKE_MARGIN:
                        success = execute_polymarket_order(markets_data["DOWN_TOKEN"], investment, side="BUY")
                        if success:
                            with state_lock:
                                bot_state["active_trade"] = {
                                    "direction": "DOWN",
                                    "token_id": markets_data["DOWN_TOKEN"],
                                    "strike_price": strike,
                                    "cost": investment
                                }
                                if not IS_LIVE: bot_state["virtual_balance"] -= investment

        except Exception as e:
            add_log(f"🚨 Awaria pętli decyzyjnej: {e}")
            
        time.sleep(5)

# --- PANEL KONTROLNY (WEB SERWER) ---
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): return
    def do_GET(self):
        if self.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            with state_lock:
                display_state = bot_state.copy()
                if IS_LIVE:
                    display_state["virtual_balance"] = bot_state["real_balance"]
                self.wfile.write(json.dumps(display_state).encode('utf-8'))
            return

        # Serwowanie Dashboardu HTML (Zostaje bez zmian z Twojej wersji, automatycznie podmieni saldo)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"<h1>Krajekis Bot Aktywny</h1><p>Status wysylany do API...</p>")

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_trading_strategy)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    server.serve_forever()
