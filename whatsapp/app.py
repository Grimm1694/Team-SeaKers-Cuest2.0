import os
import re
import sqlite3
import requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from PIL import Image
import pytesseract
from pdf2image import convert_from_path
from PyPDF2 import PdfReader
from bs4 import BeautifulSoup
from io import BytesIO
from urllib.parse import urlparse
from dotenv import load_dotenv
import google.generativeai as genai

# Load environment variables
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY in .env")

genai.configure(api_key=GEMINI_API_KEY)

app = Flask(__name__)

DB_FILE = "conversations.db"
WHATSAPP_MSG_LIMIT = 1500

CANONICAL_SOURCES = {
    "who": "https://www.who.int/",
    "world health organization": "https://www.who.int/",
    "cdc": "https://www.cdc.gov/",
    "centers for disease control and prevention": "https://www.cdc.gov/",
    "pubmed": "https://pubmed.ncbi.nlm.nih.gov/",
    "nhs": "https://www.nhs.uk/",
    "mayoclinic": "https://www.mayoclinic.org/",
    "johns hopkins": "https://www.hopkinsmedicine.org/",
}

MISINFO_KEYWORDS = [
    "cure", "cures", "causes", "hoax", "fake", "myth", "claim",
    "vaccine", "vaccination", "covid", "coronavirus", "garlic",
]

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                user_id TEXT PRIMARY KEY,
                history TEXT
            )
        """)
init_db()

def get_history(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT history FROM conversations WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return row[0].split("\n") if row and row[0] else []

def save_history(user_id, history_list):
    history_text = "\n".join(history_list[-20:])
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO conversations(user_id, history) VALUES(?, ?)
            ON CONFLICT(user_id) DO UPDATE SET history=excluded.history
        """, (user_id, history_text))

def chunk_message(text, limit=WHATSAPP_MSG_LIMIT):
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + limit, n)
        if end < n:
            space = text.rfind(" ", start, end)
            if space > start:
                end = space
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"({i+1}/{total}) {c}" for i, c in enumerate(chunks)]
    return chunks

def detect_misinformation_query(text):
    text_lower = text.lower()
    return any(k in text_lower for k in MISINFO_KEYWORDS)

def extract_text_from_image(img_bytes):
    try:
        image = Image.open(BytesIO(img_bytes))
        return pytesseract.image_to_string(image)
    except Exception as e:
        return f"(OCR error: {e})"

def extract_text_from_pdf(pdf_bytes):
    tmp_path = "temp.pdf"
    with open(tmp_path, "wb") as f:
        f.write(pdf_bytes)
    text = ""
    try:
        reader = PdfReader(tmp_path)
        for page in reader.pages:
            text += page.extract_text() or ""
    except Exception:
        pass
    if not text.strip():
        try:
            pages = convert_from_path(tmp_path)
            for page in pages:
                text += pytesseract.image_to_string(page)
        except Exception as e:
            text = f"(PDF OCR error: {e})"
    return text.strip()

def extract_text_from_url(url):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        paragraphs = [p.get_text() for p in soup.find_all("p")]
        return "\n".join(paragraphs[:10])
    except Exception:
        return ""

def lookup_canonical_link(name_or_url):
    key = name_or_url.lower()
    if re.match(r"^https?://", key):
        return name_or_url
    for k, v in CANONICAL_SOURCES.items():
        if k in key or key in k:
            return v
    return None

def extract_source_mentions(text):
    urls = re.findall(r"https?://[^\s]+", text)
    sources = []
    for url in urls:
        sources.append(url)
    lowered = text.lower()
    for key in CANONICAL_SOURCES:
        if key in lowered:
            sources.append(CANONICAL_SOURCES[key])
    return list(dict.fromkeys(sources))

def append_verified_links(reply_text):
    sources = extract_source_mentions(reply_text)
    if not sources:
        sources = list(CANONICAL_SOURCES.values())
    lines = ["\n\nVerified sources:"]
    for s in sources:
        lines.append(f"- {s}")
    return reply_text + "\n".join(lines)

def ask_gemini(parts):
    model = genai.GenerativeModel("gemini-1.5-flash")
    resp = model.generate_content(parts)
    return resp.text.strip()

def build_prompt(user_text, history):
    hist_text = "\n".join(history[-10:]) if history else ""
    is_misinfo = detect_misinformation_query(user_text)
    if is_misinfo:
        prompt = f"""
You are a trusted health misinformation detection assistant.

Context of conversation so far:
{hist_text}

Task:
1) Determine if the following claim is misinformation or true.
2) Explain briefly with 2-6 sentences.
3) Provide trusted sources or URLs where applicable.

Claim:
\"\"\"{user_text}\"\"\"
"""
    else:
        prompt = f"""
You are a friendly, helpful health assistant.

Context of conversation so far:
{hist_text}

User question:
\"\"\"{user_text}\"\"\"

Please answer concisely (2-6 sentences), with sources or URLs if applicable.
"""
    return prompt

@app.route("/bot", methods=["POST"])
def bot():
    user_id = request.values.get("From", "unknown_user")
    incoming_text = request.values.get("Body", "").strip()
    num_media = int(request.values.get("NumMedia", 0))
    media_url = request.values.get("MediaUrl0")
    media_type = request.values.get("MediaContentType0")

    resp = MessagingResponse()
    history = get_history(user_id)

    history.append(f"User: {incoming_text if incoming_text else '(media)'}")

    if num_media > 0 and media_url:
        media_resp = requests.get(media_url)
        content_text = ""
        if media_type and media_type.startswith("image/"):
            content_text = extract_text_from_image(media_resp.content)
        elif media_type == "application/pdf":
            content_text = extract_text_from_pdf(media_resp.content)
        else:
            content_text = f"(Unsupported media type: {media_type})"
        incoming_text += f"\n[Extracted text from media]:\n{content_text}"
        history[-1] = f"User: {incoming_text}"

    urls = re.findall(r"https?://[^\s]+", incoming_text)
    for url in urls:
        page_text = extract_text_from_url(url)
        if page_text:
            incoming_text += f"\n[Extracted text from URL {url}]:\n{page_text}"

    prompt = build_prompt(incoming_text, history)
    gemini_response = ask_gemini(prompt)

    history.append(f"Assistant: {gemini_response[:1000]}")

    save_history(user_id, history)

    final_reply = append_verified_links(gemini_response)

    for chunk in chunk_message(final_reply):
        resp.message(chunk)

    return str(resp)

if __name__ == "__main__":
    app.run(port=5000, debug=True)
