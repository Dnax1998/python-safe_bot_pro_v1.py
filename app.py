import os
import time
import threading
from flask import Flask, render_template_string
import ccxt
import pandas as pd
import pandas_ta as ta

app = Flask(__name__)

# ==========================================
# 1. KONFIGURACJA BOTA I PAPIEROWEGO PORTFELA
# ==========================================
SYMBOLS = ['BTC/USDC', 'ETH/USDC']
TIMEFRAME = '5m'
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 200
ATR_PERIOD = 14

SL_MULTIPLIER = 1.5
TP_MULTIPLIER = 2.0

# Połączenie z giełdą MEXC (Pobieranie publicznych świec NIE wymaga kluczy API)
exchange = ccxt.mexc({'enableRateLimit': True})

# Stan papierowego portfela i logów (przechowywany w pamięci)
paper_wallet = {
    'usdc_balance': 1000.0,  # Początkowy kapitał testowy
    'positions': {},         # Otwarte pozycje testowe
    'trade_history': [],     # Zakończone transakcje
    'bot_logs': []           # Dziennik zdarzeń
}

def log_event(message):
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    entry = f"[{timestamp}] {message}"
    paper_wallet['bot_logs'].insert(0, entry)
    # Trzymamy tylko 50 ostatnich logów
    if len(paper_wallet['bot_logs']) > 50:
        paper_wallet['bot_logs'].pop()
    print(entry)

# ==========================================
# 2. POBIERANIE DANYCH I LOGIKA STRATEGII
# ==========================================
def fetch_and_calculate(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=300)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        df['ema_fast'] = ta.ema(df['close'], length=EMA_FAST)
        df['ema_slow'] = ta.ema(df['close'], length=EMA_SLOW)
        df['ema_trend'] = ta.ema(df['close'], length=EMA_TREND)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_PERIOD)
        return df
    except Exception as e:
        log_event(f"Błąd pobierania danych dla {symbol}: {e}")
        return None

def process_symbol(symbol):
    df = fetch_and_calculate(symbol)
    if df is None or len(df) < EMA_TREND:
        return

    current_price = df['close'].iloc[-1]
    
    # ----------------------------------------
    # A. Sprawdzanie otwartej pozycji (SL / TP)
    # ----------------------------------------
    if symbol in paper_wallet['positions']:
        pos = paper_wallet['positions'][symbol]
        side = pos['side']
        entry_price = pos['entry_price']
        sl = pos['sl']
        tp = pos['tp']

        closed = False
        pnl = 0

        if side == 'BUY':
            if current_price <= sl:
                closed, reason = True, 'Stop Loss'
            elif current_price >= tp:
                closed, reason = True, 'Take Profit'
        elif side == 'SELL':
            if current_price >= sl:
                closed, reason = True, 'Stop Loss'
            elif current_price <= tp:
                closed, reason = True, 'Take Profit'

        if closed:
            if side == 'BUY':
                pnl = (current_price - entry_price) / entry_price * pos['amount_usdc']
            else:
                pnl = (entry_price - current_price) / entry_price * pos['amount_usdc']

            paper_wallet['usdc_balance'] += (pos['amount_usdc'] + pnl)
            paper_wallet['trade_history'].insert(0, {
                'symbol': symbol,
                'side': side,
                'entry': entry_price,
                'exit': current_price,
                'pnl': pnl,
                'reason': reason,
                'time': time.strftime('%H:%M:%S')
            })
            log_event(f"Zamknięto pozycję {side} na {symbol} z powodem {reason}. PnL: {pnl:.2f} USDC")
            del paper_wallet['positions'][symbol]
            return

    # ----------------------------------------
    # B. Wykrywanie nowych sygnałów (gdy brak pozycji)
    # ----------------------------------------
    prev_close = df['close'].iloc[-2]
    prev_fast, prev_slow, prev_trend = df['ema_fast'].iloc[-2], df['ema_slow'].iloc[-2], df['ema_trend'].iloc[-2]
    prev2_fast, prev2_slow = df['ema_fast'].iloc[-3], df['ema_slow'].iloc[-3]
    current_atr = df['atr'].iloc[-2]

    trade_amount = 100.0  # Kwota pojedynczej transakcji papierowej w USDC

    # Sygnał KUPNA (LONG)
    if prev_close > prev_trend and (prev2_fast <= prev2_slow) and (prev_fast > prev_slow):
        if paper_wallet['usdc_balance'] >= trade_amount:
            sl = prev_close - (current_atr * SL_MULTIPLIER)
            tp = prev_close + (current_atr * TP_MULTIPLIER)
            paper_wallet['usdc_balance'] -= trade_amount
            paper_wallet['positions'][symbol] = {
                'side': 'BUY', 'entry_price': prev_close, 'sl': sl, 'tp': tp, 'amount_usdc': trade_amount
            }
            log_event(f"PAPER BUY {symbol} po cenie {prev_close:.2f} | SL: {sl:.2f} | TP: {tp:.2f}")

    # Sygnał SPRZEDAŻY (SHORT)
    elif prev_close < prev_trend and (prev2_fast >= prev2_slow) and (prev_fast < prev_slow):
        if paper_wallet['usdc_balance'] >= trade_amount:
            sl = prev_close + (current_atr * SL_MULTIPLIER)
            tp = prev_close - (current_atr * TP_MULTIPLIER)
            paper_wallet['usdc_balance'] -= trade_amount
            paper_wallet['positions'][symbol] = {
                'side': 'SELL', 'entry_price': prev_close, 'sl': sl, 'tp': tp, 'amount_usdc': trade_amount
            }
            log_event(f"PAPER SELL {symbol} po cenie {prev_close:.2f} | SL: {sl:.2f} | TP: {tp:.2f}")

# Wątek tła wykonujący analizę rynku w pętli
def bot_loop():
    log_event("Uruchomiono silnik Paper Trading na MEXC (BTC/USDC, ETH/USDC)...")
    while True:
        for symbol in SYMBOLS:
            process_symbol(symbol)
            time.sleep(2)
        time.sleep(10)

# Uruchomienie wątku tła
threading.Thread(target=bot_loop, daemon=True).start()

# ==========================================
# 3. INTERFEJS STRONY WWW (HTML + FLASK)
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="10">
    <title>MEXC Scalping Bot - Paper Trading</title>
    <style>
        body { font-family: monospace, Arial, sans-serif; background-color: #121214; color: #e1e1e6; padding: 20px; margin: 0; }
        h1, h2 { color: #00b37e; border-bottom: 1px solid #29292e; padding-bottom: 8px; }
        .card { background-color: #202024; padding: 15px; border-radius: 6px; margin-bottom: 20px; border: 1px solid #29292e; }
        .balance { font-size: 24px; font-weight: bold; color: #00b37e; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #29292e; }
        th { background-color: #121214; color: #8d8d99; }
        .green { color: #00b37e; font-weight: bold; }
        .red { color: #f75a68; font-weight: bold; }
        .logs { background-color: #121214; padding: 10px; height: 180px; overflow-y: scroll; font-size: 12px; color: #a8a8b3; border: 1px solid #29292e; }
    </style>
</head>
<body>
    <h1>MEXC Scalper - Paper Trading Panel</h1>
    
    <div class="card">
        <div>Wirtualne Saldo Portfela (USDC):</div>
        <div class="balance">{{ "%.2f"|format(wallet.usdc_balance) }} USDC</div>
    </div>

    <div class="card">
        <h2>Aktywne Pozycje Testowe</h2>
        <table>
            <tr><th>Para</th><th>Typ</th><th>Wejście</th><th>Stop Loss</th><th>Take Profit</th></tr>
            {% for symbol, pos in wallet.positions.items() %}
            <tr>
                <td><b>{{ symbol }}</b></td>
                <td class="{{ 'green' if pos.side == 'BUY' else 'red' }}">{{ pos.side }}</td>
                <td>{{ "%.2f"|format(pos.entry_price) }}</td>
                <td class="red">{{ "%.2f"|format(pos.sl) }}</td>
                <td class="green">{{ "%.2f"|format(pos.tp) }}</td>
            </tr>
            {% else %}
            <tr><td colspan="5">Brak aktywnych pozycji w tej chwili.</td></tr>
            {% endif %}
        </table>
    </div>

    <div class="card">
        <h2>Zamknięte Transakcje</h2>
        <table>
            <tr><th>Czas</th><th>Para</th><th>Typ</th><th>Wejście</th><th>Wyjście</th><th>Wynik (PnL)</th><th>Powód</th></tr>
            {% for t in wallet.trade_history %}
            <tr>
                <td>{{ t.time }}</td>
                <td><b>{{ t.symbol }}</b></td>
                <td class="{{ 'green' if t.side == 'BUY' else 'red' }}">{{ t.side }}</td>
                <td>{{ "%.2f"|format(t.entry) }}</td>
                <td>{{ "%.2f"|format(t.exit) }}</td>
                <td class="{{ 'green' if t.pnl >= 0 else 'red' }}">{{ "%.2f"|format(t.pnl) }} USDC</td>
                <td>{{ t.reason }}</td>
            </tr>
            {% else %}
            <tr><td colspan="7">Brak historii zamkniętych transakcji.</td></tr>
            {% endif %}
        </table>
    </div>

    <div class="card">
        <h2>Dziennik Zdarzeń (Logs)</h2>
        <div class="logs">
            {% for log in wallet.bot_logs %}
                <div>{{ log }}</div>
            {% endfor %}
        </div>
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, wallet=paper_wallet)

if __name__ == '__main__':
    # Pobieranie portu wymaganego przez serwery chmurowe (np. Render)
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
