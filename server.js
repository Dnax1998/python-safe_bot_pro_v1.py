import express from 'express';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Stan Bota
let botState = {
    running: false,
    loanSize: 100000,
    minProfit: 15,
    balance: 5000.0,
    logs: [],
    prices: {
        "BTCB/USDT": { price: 65000, spread: 0 },
        "ETH/USDT": { price: 3500, spread: 0 },
        "BNB/USDT": { price: 600, spread: 0 },
        "CAKE/USDT": { price: 2.50, spread: 0 }
    }
};

// Pętla symulacji / skanowania
setInterval(() => {
    if (!botState.running) return;

    const timeStr = new Date().toLocaleTimeString();
    
    for (const [pair, data] of Object.entries(botState.prices)) {
        // Symulacja skanowania puli DEX
        const dev = (Math.random() - 0.48) * (data.price * 0.008);
        data.price = Number((data.price + dev).toFixed(2));
        data.spread = Number((Math.random() * 1.2).toFixed(2));

        // Kalkulacja zysku (próg 0.35% opłat)
        if (data.spread > 0.35) {
            const gross = botState.loanSize * (data.spread / 100);
            const net = gross - (botState.loanSize * 0.0035);

            if (net >= botState.minProfit) {
                botState.balance += net;
                const logEntry = `[${timeStr}] ✅ ARBITRAŻ: ${pair} | Spread: ${data.spread}% | Zysk: +$${net.toFixed(2)}`;
                botState.logs.unshift(logEntry);
                if (botState.logs.length > 20) botState.logs.pop();
            }
        }
    }
}, 2000);

// Endpointy API dla Frontendu HTML
app.get('/api/data', (req, res) => res.json(botState));

app.post('/api/control', (req, res) => {
    const { running, loanSize, minProfit } = req.body;
    if (running !== undefined) botState.running = running;
    if (loanSize) botState.loanSize = Number(loanSize);
    if (minProfit) botState.minProfit = Number(minProfit);
    res.json({ success: true, state: botState });
});

app.listen(PORT, () => {
    console.log(`Serwer bota uruchomiony na porcie ${PORT}`);
});
