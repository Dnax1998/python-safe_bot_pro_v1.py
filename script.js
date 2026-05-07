function showTab(tabId) {
    // Ukryj wszystkie zakładki
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.style.display = 'none';
    });
    
    // Pokaż wybraną
    document.getElementById(tabId).style.display = 'block';

    // Zmień aktywny przycisk
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.classList.remove('active');
        if(btn.innerText.toLowerCase().includes(tabId === 'home' ? 'start' : tabId)) {
            btn.classList.add('active');
        }
    });
}
