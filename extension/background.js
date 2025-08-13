chrome.runtime.onInstalled.addListener(() => {
    console.log("Extension installed");
});

// Listen for messages from content or popup
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === "checkFact") {
        console.log("Backend request for:", message.text);

        // Replace with your backend API URL
        fetch("http://localhost:8000/verify", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ text: message.text })
        })
        .then(res => res.json())
        .then(data => {
            console.log("Backend response:", data);
            sendResponse({ success: true, data });
        })
        .catch(err => {
            console.error("Error calling backend:", err);
            sendResponse({ success: false, error: err.toString() });
        });

        return true; // keep the sendResponse channel open
    }
});
