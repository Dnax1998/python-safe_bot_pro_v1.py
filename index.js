require('dotenv').config();
const { ethers } = require('ethers');
const { ClobClient } = require('@polymarket/clob-client');

// Konfiguracja środowiska
const RPC_URL = process.env.POLYGON_RPC_URL;
const PRIVATE_KEY = process.env.PRIVATE_KEY;
const TARGET_WALLET = process.env.TARGET_WALLET; // Portfel, który kopiujemy

// Inicjalizacja połączenia z siecią Polygon
const provider = new ethers.providers.JsonRpcProvider(RPC_URL);
const wallet = new ethers.Wallet(PRIVATE_KEY, provider);

// Inicjalizacja klienta Polymarket CLOB
const clobClient = new ClobClient(RPC_URL, 137, wallet);

async function startBot() {
    console.log(`Uruchamiam bota. Śledzę portfel: ${TARGET_WALLET}`);

    // Utworzenie kluczy API dla Polymarket (wymagane przy pierwszym użyciu)
    // W produkcji klucze te należy zapisać i odtwarzać, by nie generować ich co chwila.
    const creds = await clobClient.createApiKey();
    clobClient.setApiKey(creds);

    // Nasłuchiwanie nowych bloków w poszukiwaniu transakcji celu
    provider.on('block', async (blockNumber) => {
        const block = await provider.getBlockWithTransactions(blockNumber);
        
        for (let tx of block.transactions) {
            // Sprawdzamy, czy transakcja pochodzi od śledzonego portfela
            if (tx.from.toLowerCase() === TARGET_WALLET.toLowerCase()) {
                console.log(`Wykryto transakcję od celu! Hash: ${tx.hash}`);
                await analyzeAndCopyTrade(tx);
            }
        }
    });
}

async function analyzeAndCopyTrade(tx) {
    // UWAGA: To jest uproszczona logika. 
    // W praktyce musisz zdekodować dane transakcji (tx.data), aby dowiedzieć się:
    // 1. Jaki token (Condition ID) kupił cel.
    // 2. Czy kupił opcję "TAK" czy "NIE".
    
    console.log("Analizuję transakcję...");
    
    try {
        // Przykład wysłania zlecenia rynkowego na Polymarket
        // Zakładamy, że wyciągnęliśmy 'tokenId' z transakcji celu
        const dummyTokenId = "0x..."; 
        const amountToBuy = 10; // Kupujemy 10 udziałów (należy dostosować do strategii)

        // Stworzenie zlecenia kupna
        const order = await clobClient.createOrder({
            tokenID: dummyTokenId,
            price: 0.50, // Limit ceny zapobiegający poślizgowi (Slippage)
            side: 'BUY',
            size: amountToBuy,
            feeRateBps: 0 // Zależy od obecnych opłat na Polymarket
        });

        // Wysłanie zlecenia do Orderbooka
        const response = await clobClient.postOrder(order);
        console.log("Skopiowano transakcję! Odpowiedź:", response);

    } catch (error) {
        console.error("Błąd podczas kopiowania transakcji:", error.message);
    }
}

startBot();
