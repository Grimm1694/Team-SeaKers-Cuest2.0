document.getElementById("checkBtn").addEventListener("click", () => {
    const text = document.getElementById("factInput").value.trim();
    if (!text) {
        alert("Please enter text to check");
        return;
    }

    chrome.runtime.sendMessage(
        { type: "checkFact", text },
        (response) => {
            const resultDiv = document.getElementById("result");
            if (response && response.success) {
                resultDiv.innerHTML = `
                    <p><strong>Verdict:</strong> ${response.data.verdict}</p>
                    <p><strong>Summary:</strong> ${response.data.summary}</p>
                    <p><strong>Links:</strong> ${response.data.links.map(
                        l => `<a href="${l.url}" target="_blank">${l.title}</a>`
                    ).join(", ")}</p>
                `;
            } else {
                resultDiv.textContent = "Error: " + (response?.error || "Unknown");
            }
        }
    );
});
