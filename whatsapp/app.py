import os
import re
import sqlite3
import time
import requests
from io import BytesIO
from urllib.parse import urlparse

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

from PIL import Image
try:
    import pillow_heif  # HEIC/HEIF support (iOS)
    pillow_heif.register_heif_opener()
except Exception:
    pass

import pytesseract
from pdf2image import convert_from_path
from PyPDF2 import PdfReader
from bs4 import BeautifulSoup

from dotenv import load_dotenv
import google.generativeai as genai

# -------------------- Setup --------------------
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY in .env")
genai.configure(api_key=GEMINI_API_KEY)

TWILIO_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
if not (TWILIO_SID and TWILIO_AUTH):
    raise RuntimeError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN in .env")

app = Flask(__name__)

DB_FILE = "conversations.db"
WHATSAPP_MSG_LIMIT = 1500  # ~1600 limit; 1500 is safe

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

# -------------------- DB (conversation memory) --------------------
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
    history_text = "\n".join(history_list[-20:])  # keep last 20 turns
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO conversations(user_id, history) VALUES(?, ?)
            ON CONFLICT(user_id) DO UPDATE SET history=excluded.history
        """, (user_id, history_text))

# -------------------- Helpers --------------------
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
    tmp_path = "temp_twilio.pdf"
    with open(tmp_path, "wb") as f:
        f.write(pdf_bytes)
    text = ""
    try:
        reader = PdfReader(tmp_path)
        for page in reader.pages:
            text += (page.extract_text() or "") + "\n"
    except Exception:
        pass
    # OCR fallback if text is empty/short
    if len(text.strip()) < 10:
        try:
            pages = convert_from_path(tmp_path)  # needs Poppler
            for page in pages:
                text += pytesseract.image_to_string(page) + "\n"
        except Exception as e:
            text = f"(PDF OCR error: {e})"
    return text.strip()

def extract_text_from_url(url):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        return "\n".join(paragraphs[:10])
    except Exception:
        return ""

def extract_source_mentions(text):
    urls = re.findall(r"https?://[^\s]+", text)
    sources = list(dict.fromkeys(urls))
    lowered = text.lower()
    for key, val in CANONICAL_SOURCES.items():
        if key in lowered and val not in sources:
            sources.append(val)
    return sources

def append_verified_links(reply_text):
    sources = extract_source_mentions(reply_text)
    if not sources:
        sources = list(CANONICAL_SOURCES.values())
    lines = ["", "", "Verified sources:"]
    for s in sources[:5]:
        lines.append(f"- {s}")
    combined = reply_text + "\n".join(lines)
    return combined if len(combined) <= 6000 else reply_text

def ask_gemini_text(user_text, history):
    """Plain text to Gemini 2.5 Flash with light misinfo steering."""
    hist_text = "\n".join(history[-10:]) if history else ""
    is_misinfo = detect_misinformation_query(user_text)
    if is_misinfo:
        prompt = f"""
You are a trusted health misinformation detection assistant.

Context so far:
{hist_text}

Task:
1) Determine if the following claim is misinformation or true.
2) Explain briefly with 2–6 sentences.
3) Provide trusted sources or URLs where applicable.

Claim:
\"\"\"{user_text}\"\"\"""".strip()
    else:
        prompt = f"""
You are a friendly, helpful health assistant.

Context so far:
{hist_text}

User question:
\"\"\"{user_text}\"\"\"\n
Please answer concisely (2–6 sentences), with sources or URLs if applicable.""".strip()

    model = genai.GenerativeModel("gemini-2.5-flash")
    resp = model.generate_content(prompt)
    return (resp.text or "").strip()

def ask_gemini_images(user_text, images):
    """
    images: list of (mime, bytes) for image/*.
    Sends raw images + text to Gemini 2.5 Flash (multimodal).
    """
    system_prompt = (
        "You are a careful medical/health assistant. Analyze the image(s). "
        "If there is text, read it. Identify any health claims, assess credibility, "
        "and give a concise 2–6 sentence summary with practical advice and what to verify."
    )
    parts = [system_prompt]
    for mime, blob in images:
        parts.append({"mime_type": mime, "data": blob})
    if user_text:
        parts.append(f"Additional user context:\n{user_text}")

    model = genai.GenerativeModel("gemini-2.5-flash")
    resp = model.generate_content(parts)
    return (resp.text or "").strip() or "I couldn't derive a useful analysis from the image(s)."

# -------- Twilio-authenticated media download + iterators --------
def auth_get(url, max_retries=3, backoff=0.8):
    """GET Twilio media with Basic Auth + simple retries."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, auth=(TWILIO_SID, TWILIO_AUTH), timeout=30)
            if r.status_code == 200 and r.content:
                return r
            last_err = f"HTTP {r.status_code} len={len(r.content)}"
        except Exception as e:
            last_err = str(e)
        time.sleep(backoff * attempt)
    raise RuntimeError(f"download_failed: {last_err}")

def iter_media(req):
    """
    Yield (index, url, content_type, bytes or None, error or None) for each media item.
    """
    try:
        n = int(req.values.get("NumMedia", "0"))
    except:
        n = 0
    for i in range(n):
        url = req.values.get(f"MediaUrl{i}")
        ctype = (req.values.get(f"MediaContentType{i}") or "").lower()
        if not url:
            yield i, None, ctype, None, "missing_media_url"
            continue
        try:
            resp = auth_get(url)
            mime = (resp.headers.get("Content-Type") or ctype).split(";")[0].strip().lower()
            blob = resp.content
            # Normalize HEIC to PNG (common from iPhones)
            if mime in ("image/heic", "image/heif"):
                try:
                    img = Image.open(BytesIO(blob))
                    buf = BytesIO()
                    img.save(buf, format="PNG")
                    blob = buf.getvalue()
                    mime = "image/png"
                except Exception:
                    pass
            yield i, url, mime, blob, None
        except Exception as e:
            yield i, url, ctype, None, f"download_error: {e}"

# -------------------- Flask webhook --------------------
@app.route("/bot", methods=["POST"])
def bot():
    user_id = request.values.get("From", "unknown_user")
    incoming_text = (request.values.get("Body") or "").strip()

    resp = MessagingResponse()
    history = get_history(user_id)
    history.append(f"User: {incoming_text if incoming_text else '(media)'}")

    # 1) Collect and classify all media (images/PDFs)
    images = []
    pdf_blobs = []
    non_image_notes = []
    for i, url, mime, blob, err in iter_media(request):
        if err or not blob:
            non_image_notes.append(f"[Media {i}] {err or 'empty'}")
            continue
        if mime.startswith("image/"):
            images.append((mime, blob))
        elif mime in ("application/pdf", "application/x-pdf", "application/acrobat"):
            pdf_blobs.append(blob)
        else:
            non_image_notes.append(f"[Media {i}] unsupported type: {mime}")

    # 2) Optional: extract text from PDFs (OCR fallback)
    extracted_from_pdfs = []
    for idx, pdf in enumerate(pdf_blobs):
        text = extract_text_from_pdf(pdf)
        extracted_from_pdfs.append(f"[PDF {idx} text/OCR]\n{text if text.strip() else '(no text found)'}")

    # 3) Expand any included URLs in the user's text for context
    urls = re.findall(r"https?://[^\s]+", incoming_text)
    url_contexts = []
    for u in urls[:5]:
        page_text = extract_text_from_url(u)
        if page_text:
            url_contexts.append(f"[Extracted from {u}]\n{page_text}")

    # 4) Build unified context (for both text-only and image flows)
    context_bits = []
    if incoming_text: context_bits.append(incoming_text)
    if extracted_from_pdfs: context_bits.append("\n\n".join(extracted_from_pdfs))
    if non_image_notes: context_bits.append("\n".join(non_image_notes))
    if url_contexts: context_bits.append("\n\n".join(url_contexts))
    combined_context = "\n\n".join(context_bits).strip()

    # 5) Decide which Gemini path to use
    try:
        if images:
            # Multimodal: images + (optional) text context
            gemini_response = ask_gemini_images(combined_context, images)
        else:
            # Text-only (includes any OCR’d PDF text & URL excerpts)
            # If nothing at all, give a gentle prompt back
            if not combined_context:
                gemini_response = "Please send a question or an image to analyze."
            else:
                gemini_response = ask_gemini_text(combined_context, history)
    except Exception as e:
        gemini_response = f"I couldn’t analyze the message due to an internal error: {e}"

    # 6) Save short history and append verified links
    history.append(f"Assistant: {gemini_response[:1000]}")
    save_history(user_id, history)

    final_reply = append_verified_links(gemini_response)

    # 7) Send chunked WA messages
    for chunk in chunk_message(final_reply):
        resp.message(chunk)

    return str(resp)

# -------------------- Entrypoint --------------------
if __name__ == "__main__":
    # Run locally, then expose with: ngrok http 5000
    app.run(host="0.0.0.0", port=5000, debug=True)
