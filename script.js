// Przełączanie zakładek
function showTab(tabId) {
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.style.display = 'none';
    });
    document.getElementById(tabId).style.display = 'block';

    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    // Ustawia aktywny przycisk
    event.currentTarget.classList.add('active');
}

// System Veto
let banCount = 0;
function banMap(btn) {
    if (btn.classList.contains('banned')) return;
    
    btn.classList.add('banned');
    banCount++;
    
    const status = document.getElementById('veto-status');
    if (banCount < 6) {
        status.innerText = `Kolejka: ${banCount % 2 === 0 ? 'Drużyna A' : 'Drużyna B'} (Banuje)`;
    } else {
        status.innerText = "Została mapa finałowa!";
        status.style.color = "#00ffcc";
        status.style.fontWeight = "bold";
    }
}
