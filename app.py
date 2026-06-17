import time
import requests
from datetime import datetime

# Prosta pamięć bota do liczenia średniej ceny (zastępuje VWAP na start)
price_history = []

def get_btc_price():
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        response = requests.get(url, timeout=5).json()
        return float(response['price'])
    except Exception as e:
        print(f"❌ Błąd pobierania ceny z Binance: {e}")
        return None

def trading_bot():
    print("🚀 Bot Krajekis v1.0 (Paper Trading) został uruchomiony!")
    
    while True:
        current_price = get_btc_price()
        if not current_price:
            time.sleep(10)
            continue
            
        # Zapisz cenę do historii (maksymalnie 30 ostatnich wpisów)
        price_history.append(current_price)
        if len(price_history) > 30:
            price_history.pop(0)
            
        # Oblicz prostą średnią kroczącą (SMA)
        sma = sum(price_history) / len(price_history)
        
        # Obliczanie czasu do końca 15-minutowej świecy Polymarket
        now = datetime.utcnow()
        minutes_passed = now.minute % 15
        seconds_passed = now.second
        
        # Ile sekund zostało do końca rynku 15m
        total_seconds_left = (15 * 60) - (minutes_passed * 60 + seconds_passed)
        minutes_left = total_seconds_left // 60
        seconds_remain = total_seconds_left % 60
        
        print(f"📊 [{now.strftime('%H:%M:%S')}] BTC: ${current_price:,.2f} | Średnia: ${sma:,.2f} | Do końca świecy: {minutes_left}m {seconds_remain}s")
        
        # REGUŁA KRAJEKISA: Szukamy pozycji tylko w oknie 5-10 minut do końca świecy
        if 5 <= minutes_left <= 10:
            # Warunek trendu wzrostowego (Cena nad średnią)
            if current_price > sma + 20: # Margines $20
                print(f"🎯 [SYGNAŁ] Okno czasowe idealne. Cena nad średnią. Symulacja zakupu: UP (YES)")
                
            # Warunek trendu spadkowego (Cena pod średnią)
            elif current_price < sma - 20:
                print(f"🎯 [SYGNAŁ] Okno czasowe idealne. Cena pod średnią. Symulacja zakupu: DOWN (YES)")
        
        else:
            print("⏳ Poza oknem decyzyjnym (Czekam na przedział 5-10 minut do końca świecy)")
            
        # Sprawdzaj sytuację co 30 sekund
        time.sleep(30)

if __name__ == "__main__":
    import threading
    import os
    
    # Uruchomienie bota w tle
    bot_thread = threading.Thread(target=trading_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Render wymaga dynamicznego portu (domyślnie 10000)
    port = int(os.environ.get("PORT", 10000))
    
    from http.server import SimpleHTTPRequestHandler, HTTPServer
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"🌍 Serwer podtrzymujący działa na porcie {port}")
    server.serve_forever()