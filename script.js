// Przełączanie zakładek
function showTab(tabId) {
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.style.display = 'none';
    });
    document.getElementById(tabId).style.display = 'block';
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
    }
}

// Obsługa formularza (tylko komunikat)
document.getElementById('regForm').addEventListener('submit', function(e) {
    e.preventDefault();
    alert('Zgłoszenie zostało wysłane! (Pamiętaj, że to tylko demo - dane nie trafiły do bazy)');
});
