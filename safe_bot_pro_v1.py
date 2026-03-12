import threading
import time
import pandas as pd
import requests
from kucoin.client import Client
import tkinter as tk
from tkinter import scrolledtext

# ==============================
# API
# ==============================

API_KEY = "69a867e95b38220001222eff"
API_SECRET = "3063bf3e-ff37-4dc5-8562-9af4683a82de"
API_PASSPHRASE = "botcrypto"

client = Client(API_KEY, API_SECRET, API_PASSPHRASE)

# ==============================
# TELEGRAM
# ==============================

TELEGRAM_TOKEN = "8665603511:AAGHaTOljKtDvPxluTXq0vAqeK580fYfPTo"
TELEGRAM_CHAT_ID = "711884629"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
 
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg
    }

    try:
        r = requests.post(url, data=data)
        print("Telegram response:", r.text)
    except Exception as e:
        print("Telegram error:", e)
################################
# TELEGRAM COMMANDS
################################

def handle_command(text):

    global bot_running

    if text == "/status":

        msg = f"""
BOT STATUS

Running: {bot_running}

Open positions: {len(positions)}
Trades: {len(trade_history)}
"""

        send_telegram(msg)

    elif text == "/positions":

        if not positions:
            send_telegram("No open positions")
            return

        msg = "OPEN POSITIONS\n\n"

        for p in positions:
            msg += f"{p['symbol']} BUY {round(p['buy'],4)}\n"

        send_telegram(msg)

    elif text == "/profit":

        trades = len(trade_history)

        if trades > 0:

            wins = len([x for x in trade_history if x > 0])
            winrate = wins / trades * 100
            profit = sum(trade_history)

        else:

            winrate = 0
            profit = 0

        msg = f"""
TRADES: {trades}

WIN RATE: {round(winrate,1)}%

TOTAL PROFIT: {round(profit,2)}%
"""

        send_telegram(msg)

    elif text == "/balance":

        accounts = client.get_accounts()

        usdt = 0

        for acc in accounts:
            if acc["currency"] == "USDT" and acc["type"] == "trade":
                usdt = acc["available"]

        send_telegram(f"USDT BALANCE: {usdt}")

    elif text.startswith("/price"):

        try:

            coin = text.split(" ")[1].upper()
            symbol = f"{coin}-USDT"

            ticker = client.get_ticker(symbol)
            price = ticker["price"]

            send_telegram(f"{symbol} price: {price}")

        except:
            send_telegram("Usage: /price BTC")

    elif text == "/pause":

        bot_running = False
        send_telegram("BOT PAUSED")

    elif text == "/start":

        bot_running = True
        send_telegram("BOT STARTED")


def telegram_commands():

    last_update = None

    while True:

        try:

            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"

            r = requests.get(url).json()

            for update in r["result"]:

                update_id = update["update_id"]

                if last_update and update_id <= last_update:
                    continue

                last_update = update_id

                if "message" not in update:
                    continue

                text = update["message"]["text"]

                handle_command(text)

        except Exception as e:
            print("Telegram command error:", e)

        time.sleep(3)
# ==============================
# USTAWIENIA BOTA
# ==============================

PAIRS = [
"BTC-USDT",
"ETH-USDT",
"BNB-USDT",
"SOL-USDT",
"XRP-USDT",
"ADA-USDT",
"AVAX-USDT",
"DOGE-USDT",
"TRX-USDT",
"LINK-USDT",

"DOT-USDT",
"POL-USDT",
"LTC-USDT",
"BCH-USDT",
"ATOM-USDT",
"NEAR-USDT",
"FIL-USDT",
"APT-USDT",
"ARB-USDT",
"OP-USDT",

"SUI-USDT",
"SEI-USDT",
"INJ-USDT",
"RUNE-USDT",
"AAVE-USDT",
"GRT-USDT",
"ALGO-USDT",
"XLM-USDT",
"HBAR-USDT",
"EGLD-USDT",

"FTM-USDT",
"SAND-USDT",
"MANA-USDT",
"APE-USDT",
"DYDX-USDT",
"KAS-USDT",
"PEPE-USDT",
"SHIB-USDT",
"UNI-USDT",
"ETC-USDT"

]

MAX_POSITIONS = 8

positions = []
trade_history = []

bot_running = True

# ==============================
# LOG
# ==============================

def log(text):

    now = time.strftime("%H:%M:%S")
    message = f"{now} | {text}"

    log_box.insert(tk.END, message + "\n")
    log_box.yview(tk.END)

# ==============================
# RSI
# ==============================

def calculate_rsi(df, period=14):

    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss

    rsi = 100 - (100 / (1 + rs))

    return rsi

# ==============================
# EMA
# ==============================

def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

# ==============================
# POBIERANIE DANYCH
# ==============================

def get_data(symbol):

    candles = client.get_kline_data(symbol, '3min')

    df = pd.DataFrame(candles)

    df = df.iloc[:,0:6]

    df.columns = [
        "time","open","close","high","low","volume"
    ]

    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)

    return df

# ==============================
# SYGNAŁ
# ==============================
def check_signal(symbol):

    try:

        df = get_data(symbol)

        df["RSI"] = calculate_rsi(df)

        df["EMA9"] = ema(df["close"],9)
        df["EMA21"] = ema(df["close"],21)

        rsi = df["RSI"].iloc[-1]

        ema9 = df["EMA9"].iloc[-1]
        ema21 = df["EMA21"].iloc[-1]

        volume_now = df["volume"].iloc[-1]
        volume_prev = df["volume"].iloc[-2]

        if 40 < rsi < 65 and ema9 > ema21 and volume_now > volume_prev * 1.1:
            return True

        return False

    except Exception as e:

        log(f"SIGNAL ERROR {symbol} {e}")
        return False
# ==============================
# OTWARCIE POZYCJI
# ==============================

def open_position(symbol):

    if symbol in [p["symbol"] for p in positions]:
        return

    if len(positions) >= MAX_POSITIONS:
        return

    try:

        ticker = client.get_ticker(symbol)
        price = float(ticker["price"])

        funds = 20

        order = client.create_market_order(
            symbol=symbol,
            side="buy",
            funds=str(funds)
        )

        tp = price * 1.012
        sl = price * 0.99

        pos = {
            "symbol": symbol,
            "buy": price,
            "tp": tp,
            "sl": sl
        }

        positions.append(pos)

        log(f"BUY {symbol} {price}")

        send_telegram(
f"""🟢 BUY

{symbol}
Cena: {price}
TP: {round(tp,2)}
SL: {round(sl,2)}
"""
        )

    except Exception as e:

        log(f"BUY ERROR {symbol} {e}")

# =========================
# POBIERANIE SALDA COINA
# =========================

def get_coin_balance(symbol):
    coin = symbol.split("-")[0]

    accounts = client.get_accounts()

    for acc in accounts:
        if acc["currency"] == coin and acc["type"] == "trade":
            return float(acc["available"])

    return 0


import math

def get_symbol_step(symbol):

    symbols = client.get_symbols()

    for s in symbols:
        if s["symbol"] == symbol:
            return float(s["baseIncrement"])

    return 0.00000001


import math

def adjust_size_to_step(size, step):

    precision = int(round(-math.log(step,10),0))

    size = math.floor(size/step)*step

    return float(f"{size:.{precision}f}")

# ==============================
# SPRAWDZANIE POZYCJI
# ==============================

def check_positions():

    for p in positions[:]:

        try:
           ticker = client.get_ticker(p["symbol"])
           price = float(ticker["price"])
        except Exception:
            continue

        profit = (price / p["buy"] - 1) * 100

        # TRAILING STOP
        if price > p["buy"] * 1.015:
            new_sl = price * 0.99
            if new_sl > p["sl"]:
                p["sl"] = new_sl

        # TAKE PROFIT lub STOP LOSS
        if price >= p["tp"] or price <= p["sl"]:

            try:
               balance = get_coin_balance(p["symbol"])

               if balance > 0:

                   step = get_symbol_step(p["symbol"])

                   size = adjust_size_to_step(balance * 0.98, step)

                   log(f"TRY SELL {p['symbol']} size {size}")

                   client.create_market_order(
                       symbol=p["symbol"],
                       side="sell",
                       size=size
                   )

                   log(f"SELL {p['symbol']} {round(profit,2)}%")

                   send_telegram(
            f"""🔴 SELL

            {p['symbol']}
            Cena: {price}
            Profit: {round(profit,2)}%
            """
                    )

                   trade_history.append(profit)
                   positions.remove(p)

            except Exception as e:
                log(f"SELL ERROR {p['symbol']} {e}")
# ==============================
# BOT LOOP
# ==============================

def bot():

    send_telegram("BOT URUCHOMIONY")

    while bot_running:

        try:

            for pair in PAIRS:

                try:

                    log(f"Scanning {pair}")

                    if len(positions) < MAX_POSITIONS:

                        if check_signal(pair):

                            open_position(pair)

                    time.sleep(0.3)

                except Exception:
                    continue

            check_positions()

        except Exception as e:

            log(f"ERROR {e}")

        time.sleep(10)
# ==============================
# GUI
# ==============================
def update_gui():

    pos_box.delete(1.0, tk.END)
    pos_box.insert(tk.END, "OPEN POSITIONS\n\n")

    for p in positions:
        try:
            ticker = client.get_ticker(p["symbol"])
            price = float(ticker["price"])
        except Exception:
            continue

        profit = (price/p["buy"]-1)*100

        pos_box.insert(
            tk.END,
            f"""\n{p['symbol']}

BUY: {round(p['buy'],2)}
NOW: {round(price,2)}

TP: {round(p['tp'],2)}
SL: {round(p['sl'],2)}

PROFIT: {round(profit,2)}%
--------------------------
"""
        )
    stats_box.delete(1.0,tk.END)

    trades = len(trade_history)

    if trades > 0:

        wins = len([x for x in trade_history if x > 0])
        winrate = wins/trades*100
        profit = sum(trade_history)

    else:

        winrate = 0
        profit = 0

    stats_box.insert(
        tk.END,
f"""
BOT STATUS: RUNNING

OPEN POSITIONS: {len(positions)}/{MAX_POSITIONS}

TRADES: {trades}

WIN RATE: {round(winrate,1)}%

TOTAL PROFIT: {round(profit,2)}%
"""
    )

    root.after(3000,update_gui)

# ==============================
# GUI WINDOW
# ==============================

root = tk.Tk()
root.title("CRYPTO TRADING BOT")

pos_box = scrolledtext.ScrolledText(root,width=40,height=20)
pos_box.grid(row=0,column=0)

log_box = scrolledtext.ScrolledText(root,width=40,height=20)
log_box.grid(row=0,column=1)

stats_box = scrolledtext.ScrolledText(root,width=40,height=10)
stats_box.grid(row=1,column=0,columnspan=2)

update_gui()

threading.Thread(target=bot, daemon=True).start()
threading.Thread(target=telegram_commands, daemon=True).start()

root.mainloop()