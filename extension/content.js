// Listen for verification request
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'verifySelectedText') {
    const selectedText = window.getSelection().toString().trim();
    
    if (selectedText) {
      verifyClaim(selectedText)
        .then(result => showAlert(result))
        .catch(error => {
          showAlert({
            verdict: 'Error',
            summary: 'Failed to verify: ' + error.message
          });
        });
    }
  }
});

async function verifyClaim(text) {
  const response = await fetch('http://localhost:8000/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text })
  });
  return await response.json();
}

function showAlert(result) {
  let message = `Verdict: ${result.verdict}\n\n`;
  message += `Summary: ${result.summary}\n\n`;
  
  if (result.links?.length > 0) {
    message += "References:\n";
    result.links.forEach(link => {
      message += `- ${link.title || link.url}\n`;
    });
  }
  
  alert(message);
}