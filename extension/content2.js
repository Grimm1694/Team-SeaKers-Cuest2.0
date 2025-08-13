document.addEventListener("keydown", (e) => {
    if (e.altKey && e.shiftKey && e.code === "KeyC") {
        let selectedText = window.getSelection().toString().trim();
        if (!selectedText) {
            alert("Please select some text first.");
            return;
        }

        chrome.runtime.sendMessage(
            { type: "checkFact", text: selectedText },
            (response) => {
                if (response && response.success) {
                    showPopup(response.data);
                } else {
                    alert("Backend error: " + (response?.error || "Unknown error"));
                }
            }
        );
    }
});

function showPopup(result) {
    let existing = document.getElementById("fact-popup");
    if (existing) existing.remove();

    // Inline SVG icons
    const verdictIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" fill="green" viewBox="0 0 16 16"><path d="M13.485 1.929a.75.75 0 0 1 0 1.06L6.56 10.915l-3.537-3.538a.75.75 0 1 1 1.06-1.06l2.477 2.478 6.425-6.426a.75.75 0 0 1 1.06 0z"/></svg>`;
    const summaryIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" fill="#007bff" viewBox="0 0 16 16"><path d="M2 2h12v2H2V2zm0 3h12v2H2V5zm0 3h12v2H2V8zm0 3h12v2H2v-2z"/></svg>`;
    const linksIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" fill="#f39c12" viewBox="0 0 16 16"><path d="M6.354 5.5H5a3 3 0 0 0 0 6h1.354a.5.5 0 0 0 0-1H5a2 2 0 1 1 0-4h1.354a.5.5 0 0 0 0-1z"/><path d="M9.646 5.5H11a3 3 0 0 1 0 6H9.646a.5.5 0 0 1 0-1H11a2 2 0 1 0 0-4H9.646a.5.5 0 0 1 0-1z"/></svg>`;

    const linksHtml = (result.links || []).map(l => `
        <a href="${l.url}" target="_blank" style="color:#f39c12; text-decoration:none; margin-right:8px; font-weight:500;">
            ${l.title} ↗
        </a>
    `).join("");

    let popup = document.createElement("div");
    popup.id = "fact-popup";
    popup.innerHTML = `
        <div style="display:flex; align-items:center; gap:6px; margin-bottom:8px; font-weight:700; font-size:1.1em; color:#2c3e50;">
            ${verdictIcon}
            <span>Verdict:</span>
            <span style="color:${result.verdict.toLowerCase().includes('true') ? 'green' : 'red'};">${result.verdict}</span>
        </div>
        <div style="display:flex; align-items:center; gap:6px; margin-bottom:8px; color:#34495e;">
            ${summaryIcon}
            <span>${result.summary}</span>
        </div>
        <div style="display:flex; align-items:center; gap:6px; flex-wrap: wrap;">
            ${linksIcon}
            <div>${linksHtml || "<span style='color:#888'>No links available</span>"}</div>
        </div>
    `;

    popup.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        width: 340px;
        background: #fff;
        border-radius: 10px;
        border: 1px solid #ccc;
        padding: 15px 20px;
        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        font-size: 14px;
        z-index: 2147483647;
        box-shadow: 0 8px 24px rgba(0,0,0,0.2);
        animation: fadeInScale 0.3s ease forwards;
        user-select: text;
        cursor: default;
    `;

    // Add animations only once
    if (!document.getElementById("fact-popup-animations")) {
        const style = document.createElement("style");
        style.id = "fact-popup-animations";
        style.innerHTML = `
            @keyframes fadeInScale {0% {opacity:0; transform: scale(0.8);} 100% {opacity:1; transform: scale(1);}}
            @keyframes fadeOutScale {0% {opacity:1; transform: scale(1);} 100% {opacity:0; transform: scale(0.8);}}
            #fact-popup a:hover {text-decoration: underline;}
        `;
        document.head.appendChild(style);
    }

    document.body.appendChild(popup);

    // Auto remove with fade-out after 8s
    setTimeout(() => {
        popup.style.animation = "fadeOutScale 0.3s ease forwards";
        setTimeout(() => popup.remove(), 300);
    }, 8000);
}
