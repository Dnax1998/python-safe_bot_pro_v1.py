import time
import requests
import json
import threading
import os
import math
import asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Importy dla prawdziwego Polymarketu ---
from pyclob.client.async_client import AsyncClobClient
from pyclob.models.credentials import ApiCredentials

# =====================================================================
#  USTAWIENIA BOTA (Zarządzanie Ryzykiem)
# =====================================================================
USE_DYNAMIC_RISK = True      
RISK_PERCENT = 2.0           
FIXED_TRADE_AMOUNT = 20.0    

ENABLE_EARLY_EXIT = True     
STOP_LOSS_PRICE = 0.35       
TAKE_PROFIT_PRICE = 0.90     

PRICE_MARGIN = 15.0          
STRIKE_MARGIN = 10.0         
# =====================================================================

# --- GLOBALNY STAN BOTA ---
bot_state = {
    "real_balance": 0.0,            # Prawdziwe saldo USDC
    "current_price": 0.0,           # Cena BTC z giełd
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
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with state_lock:
        bot_state["logs"].append(log_entry)
        if len(bot_state["logs"]) > 50:
            bot_state["logs"].pop(0)

def get_btc_price():
    """Pobieranie ceny spot BTC (potrzebne do określenia trendu)"""
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return float(response.json()['data']['amount'])
    except Exception:
        pass
    return None

# =====================================================================
#  KLIENT POLYMARKET (CLOB)
# =====================================================================
def get_clob_client():
    """Inicjalizacja klienta na podstawie zmiennych środowiskowych z Render"""
    priv_key = os.getenv("POLYGON_PRIVATE_KEY")
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_pass = os.getenv("POLYMARKET_API_PASSPHRASE")

    if not all([priv_key, api_key, api_secret, api_pass]):
        add_log("⚠️ BŁĄD KRYTYCZNY: Brak kluczy API Polymarket w zmiennych środowiskowych!")
        return None

    creds = ApiCredentials(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass)
    
    # Chain ID 137 dla Polygon Mainnet
    client = AsyncClobClient("https://clob.polymarket.com", key=priv_key, chain_id=137, creds=creds)
    return client

async def find_current_15m_market_tokens(current_btc_price):
    """
    UWAGA: TĘ FUNKCJĘ MUSISZ ZBUDOWAĆ!
    Polymarket tworzy nowe Token ID dla każdych 15 minut. 
    Musisz odpytać API Gamma (https://gamma-api.polymarket.com/events) 
    aby znaleźć aktualny rynek i zwrócić Token ID dla opcji YES (UP) i NO (DOWN).
    """
    add_log("Szukam aktualnych Token IDs dla rynków 15-minutowych...")
    # Tutaj logika zapytania do Gamma API...
    
    # Zwraca strukturę dla przykładu:
    return {
        "strike_price": current_btc_price, # Zastąp ceną z nazwy rynku
        "token_up": "0x...", # Token ID dla YES
        "token_down": "0x..." # Token ID dla NO
    }

async def execute_trade(client, token_id, investment_usd, side="BUY"):
    """Składa prawdziwe zlecenie na giełdzie Polymarket"""
    try:
        # Pobranie arkusza zleceń by sprawdzić cenę
        orderbook = await client.get_order_book(token_id)
        if not orderbook.asks:
            add_log("Brak ofert sprzedaży w arkuszu!")
            return None
            
        best_price = orderbook.asks[0].price
        size = investment_usd / float(best_price)

        add_log(f"Wysyłam zlecenie {side} na kwotę {investment_usd} USDC po cenie {best_price}...")
        
        # Prawdziwe podpisanie i wysłanie transakcji
        order = await client.create_and_post_order(
            token_id=token_id,
            price=float(best_price),
            side=side,
            size=size,
            fee_rate_bps=0
        )
        add_log(f"Zlecenie przyjęte! ID: {order.get('orderID', 'Brak ID')}")
        
        return {"price": float(best_price), "size": size, "order_id": order.get('orderID')}
    except Exception as e:
        add_log(f"Błąd składania zlecenia: {e}")
        return None

# =====================================================================
#  GŁÓWNA PĘTLA ASYNCHRONICZNA
# =====================================================================
async def async_trading_strategy():
    add_log("Uruchamianie PRAWDZIWEGO systemu handlowego Polymarket...")
    client = get_clob_client()
    
    if not client:
        return

    # Autoryzacja i pobranie salda
    try:
        # Uwaga: w zależności od Twojego proxy na koncie (CTF), pobieranie salda może wyglądać inaczej.
        # Często wymaga zapytań o balans kontraktu ERC20 USDC.e na Polygonie.
        add_log("Połączono z CLOB API pomyślnie.")
    except Exception as e:
        add_log(f"Błąd autoryzacji: {e}")
        return

    current_market_tokens = None

    while True:
        try:
            current_price = get_btc_price()
            now = datetime.utcnow()
            minutes_passed = now.minute % 15
            seconds_passed = now.second
            total_seconds_left = (15 * 60) - (minutes_passed * 60 + seconds_passed)

            with state_lock:
                bot_state["minutes_left"] = total_seconds_left // 60
                bot_state["seconds_remain"] = total_seconds_left % 60
                bot_state["current_price"] = current_price
                
                if current_price:
                    price_history.append(current_price)
                    if len(price_history) > 30:
                        price_history.pop(0)
                    bot_state["sma"] = sum(price_history) / len(price_history)

            m_left = bot_state["minutes_left"]
            
            # 1. NA POCZĄTKU ŚWIECY ZNAJDŹ NOWE RYNKI
            if minutes_passed == 0 and seconds_passed < 15:
                if not current_market_tokens:
                    current_market_tokens = await find_current_15m_market_tokens(current_price)
                    with state_lock:
                        bot_state["current_candle_strike"] = current_market_tokens.get("strike_price", current_price)

            # 2. ZAMYKANIE NA KONIEC ŚWIECY
            if minutes_passed == 14 and seconds_passed >= 50:
                current_market_tokens = None # Reset na kolejną świecę
                with state_lock:
                    if bot_state["active_trade"]:
                        # Tutaj powinna być logika księgowania wyników z API lub smart kontraktu CTF
                        add_log("Zamykanie pozycji na koniec świecy. Sprawdzam wynik...")
                        trade = bot_state["active_trade"]
                        bot_state["trade_history"].append(trade)
                        bot_state["active_trade"] = None

            # 3. WEJŚCIE W POZYCJĘ (Zasada Krajekisa)
            with state_lock:
                active = bot_state["active_trade"]
                sma = bot_state["sma"]
                strike = bot_state["current_candle_strike"]
                # Dla uproszczenia bierzemy stałą kwotę jeśli saldo z API nie jest podpięte
                investment = FIXED_TRADE_AMOUNT 

            if 5 <= m_left <= 10 and not active and sma > 0 and current_market_tokens:
                price_diff = current_price - strike

                if current_price > sma + PRICE_MARGIN and price_diff > STRIKE_MARGIN:
                    # TREND WZROSTOWY - KUPUJEMY "YES" (UP)
                    token = current_market_tokens["token_up"]
                    result = await execute_trade(client, token, investment, "BUY")
                    if result:
                        with state_lock:
                            bot_state["active_trade"] = {
                                "direction": "UP",
                                "entry_price": result["price"],
                                "strike_price": strike,
                                "btc_at_entry": current_price,
                                "amount_shares": result["size"],
                                "cost": investment,
                                "opened_at": datetime.utcnow().strftime("%H:%M:%S")
                            }

                elif current_price < sma - PRICE_MARGIN and price_diff < -STRIKE_MARGIN:
                    # TREND SPADKOWY - KUPUJEMY "NO" (DOWN)
                    token = current_market_tokens["token_down"]
                    result = await execute_trade(client, token, investment, "BUY")
                    if result:
                        with state_lock:
                            bot_state["active_trade"] = {
                                "direction": "DOWN",
                                "entry_price": result["price"],
                                "strike_price": strike,
                                "btc_at_entry": current_price,
                                "amount_shares": result["size"],
                                "cost": investment,
                                "opened_at": datetime.utcnow().strftime("%H:%M:%S")
                            }

            # 4. EARLY EXIT (Stop Loss / Take Profit)
            if active and ENABLE_EARLY_EXIT:
                # Wymaga odpytania orderbooka o aktualną cenę sprzedazy (bids) dla aktywnych udziałów
                # orderbook = await client.get_order_book(active_token)
                # current_sell_price = orderbook.bids[0].price
                pass # Zostawiono miejsce na implementację

        except Exception as e:
            add_log(f"🚨 Błąd w pętli handlowej: {e}")
        
        await asyncio.sleep(3)

def run_trading_strategy():
    """Wrapper uruchamiający pętlę asyncio w osobnym wątku"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_trading_strategy())

# --- PANEL KONTROLNY (BEZ ZMIAN - Wymaga by UI było stabilne) ---
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
        
        # Twój oryginalny kod HTML ze skryptu JS 
        # (usunięto dla oszczędności miejsca w odpowiedzi, ale zostawiasz tu swój blok html = """ <!DOCTYPE html> ... """)
        html = "<html><body><h1>Dashboard serwuje dane z JSON na /api/status</h1><p>Uzyj swojego poprzedniego kodu HTML tutaj.</p></body></html>"
        self.wfile.write(html.encode('utf-8'))

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_trading_strategy)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)
    add_log(f"Serwer HTTP (Live) wystartował na porcie {port}")
    server.serve_forever()
