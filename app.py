import os
import threading
import time
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

# Inicjalizacja klienta CLOB
def init_mainnet_client():
    global poly_client
    key = os.environ.get("POLY_PRIVATE_KEY", "").replace("0x", "")
    if not key:
        print("🚨 BŁĄD: Brak POLY_PRIVATE_KEY!")
        return None
    
    try:
        # Inicjalizacja dla standardowego portfela (MetaMask)
        poly_client = ClobClient(
            host="https://clob.polymarket.com",
            key=key,
            chain_id=POLYGON
        )
        print("✅ Klient CLOB zainicjalizowany pomyślnie.")
        return poly_client
    except Exception as e:
        print(f"🚨 Błąd inicjalizacji: {e}")
        return None

# Funkcja do sprawdzania salda (używamy bezpośrednio klienta)
def update_real_balance():
    global poly_client
    if not poly_client:
        return
    try:
        # Pobieramy saldo bezpośrednio z kontraktu tradingowego
        balance = poly_client.get_collateral_balance()
        # Saldo w CLOB często zwraca dict lub float
        val = float(balance.get("balance", 0)) if isinstance(balance, dict) else float(balance)
        print(f"💰 Aktualne saldo: {val} USDC")
    except Exception as e:
        print(f"⚠️ Błąd pobierania salda: {e}")

# --- START BOTA ---
if __name__ == "__main__":
    client = init_mainnet_client()
    if client:
        # Testowe pobranie salda przy starcie
        update_real_balance()
        # Tutaj wstawiasz resztę swojej pętli tradingowej
        print("🤖 Bot gotowy do pracy!")
