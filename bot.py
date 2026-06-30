import os
import time
import sys
from web3 import Web3
from dotenv import load_dotenv

# 1. Konfiguracja
load_dotenv()

# Sprawdzenie czy klucze są wczytane ze środowiska (Render/Railway/Lokalnie)
rpc_url = os.getenv("POLYGON_RPC_URL")
private_key = os.getenv("PRIVATE_KEY")
target_wallet = os.getenv("TARGET_WALLET") # Adres Mistrza też trzymaj w env

if not rpc_url or not private_key or not target_wallet:
    print("Błąd: Brak wymaganych zmiennych środowiskowych (PRIVATE_KEY, RPC_URL, TARGET_WALLET).")
    sys.exit(1)

w3 = Web3(Web3.HTTPProvider(rpc_url))

if not w3.is_connected():
    print("Błąd: Nie udało się połączyć z siecią Polygon.")
    sys.exit(1)

print(f"Bot uruchomiony. Monitoruję portfel: {target_wallet}")

# 2. Główna pętla monitorująca
def monitor_wallet():
    last_block = w3.eth.block_number
    
    while True:
        try:
            current_block = w3.eth.block_number
            
            if current_block > last_block:
                # Sprawdzamy blok
                block = w3.eth.get_block(current_block, full_transactions=True)
                
                for tx in block.transactions:
                    if tx['from'] and tx['from'].lower() == target_wallet.lower():
                        print(f"\n--- WYKRYTO TRANSAKCJĘ MISTRZA ---")
                        print(f"Hash: {tx['hash'].hex()}")
                        print(f"Wartość: {w3.from_wei(tx['value'], 'ether')} MATIC")
                        # Tu w przyszłości dodamy logikę kopiowania
                
                last_block = current_block
            
            time.sleep(5) # Sprawdzanie co 5 sekund
            
        except Exception as e:
            print(f"Wystąpił błąd: {e}")
            time.sleep(10)

if __name__ == "__main__":
    monitor_wallet()
