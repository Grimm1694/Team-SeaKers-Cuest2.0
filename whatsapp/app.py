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
    import pillow_heif  # HEIC/HEIF support for iOS
    pillow_heif.register_heif_opener()
except Exception:
    pass

# PDF OCR fallbacks (keep if you want PDFs)
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
WHATSAPP_MSG_LIMIT = 1500  # ~1600 hard cap; 1500 is safe

# ✅ Allowed health domains (sources will be filtered to these)
ALLOWED_DOMAINS = [
    "who.int", "cdc.gov", "icmr.gov.in", "mohfw.gov.in", "fda.gov",
    "ema.europa.eu", "nice.org.uk", "cochranelibrary.com", "bmj.com",
    "thelancet.com", "nature.com", "nhs.uk", "mayoclinic.org",
    "hopkinsmedicine.org"
]
# (Optional) if you want to exclude nih.gov, just don't add it above.

# Quick heuristics to detect "claim-like" text
MISINFO_KEYWORDS = [
    "cure", "cures", "causes", "hoax", "fake", "myth", "claim",
    "vaccine", "vaccination", "covid", "coronavirus", "garlic",
    "prevent", "prevents", "treats", "treatment", "miracle"
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
    chunks, start, n = [], 0, len(text)
    while start < n:
        end = min(start + limit, n)
        if end < n:
            space = text.rfind(" ", start, end)
            if space > start:
                end = space
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        start = end
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"({i+1}/{total}) {c}" for i, c in enumerate(chunks)]
    return chunks

def is_claim_like(text: str) -> bool:
    text_lower = text.lower()
    return any(k in text_lower for k in MISINFO_KEYWORDS)

def allowed_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        host = host.lower()
        return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)
    except Exception:
        return False

def extract_urls(text: str):
    return re.findall(r"https?://[^\s)]+", text or "")

def filter_urls(urls, limit=3):
    seen = set()
    keep = []
    for u in urls:
        u = u.rstrip(".,);")
        if u not in seen and allowed_url(u):
            keep.append(u)
            seen.add(u)
        if len(keep) >= limit:
            break
    return keep

def extract_text_from_pdf(pdf_bytes):
    tmp = "temp_twilio.pdf"
    with open(tmp, "wb") as f:
        f.write(pdf_bytes)
    text = ""
    try:
        reader = PdfReader(tmp)
        for page in reader.pages:
            text += (page.extract_text() or "") + "\n"
    except Exception:
        pass
    if len(text.strip()) < 10:
        try:
            pages = convert_from_path(tmp)  # needs Poppler
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

# -------------------- Gemini calls --------------------
def build_instruction_claim():
    # Single, clear message for medical claims
    return (
        "You are a careful medical fact-checker.\n"
        "If the user content is (or contains) a medical/health claim, reply as ONE concise message in EXACTLY this format:\n"
        "Verdict: <True|False|Misleading|Unclear>\n"
        "Summary: <2–5 sentences, plain language>\n"
        "Sources:\n"
        "- <trusted URL>\n"
        "- <trusted URL>\n"
        "- <trusted URL>\n"
        "Rules:\n"
        "- Only include up to 3 sources from these domains: "
        + ", ".join(ALLOWED_DOMAINS) + ".\n"
        "- If no reliable sources found, write 'Sources:\n- None'.\n"
        "- No prefaces, no emojis, no markdown headings."
    )

def build_instruction_query():
    # Single, clear message for general questions
    return (
        "You are a concise medical/health assistant.\n"
        "If the user asks a question (not a claim), reply as ONE concise message:\n"
        "Answer: <2–5 sentences, plain language>\n"
        "Sources:\n"
        "- <trusted URL>\n"
        "- <trusted URL>\n"
        "- <trusted URL>\n"
        "Rules:\n"
        "- Only include up to 3 sources from these domains: "
        + ", ".join(ALLOWED_DOMAINS) + ".\n"
        "- If no reliable sources found, write 'Sources:\n- None'.\n"
        "- No prefaces, no emojis, no markdown headings."
    )

def ask_gemini_text(user_text, history):
    hist_text = "\n".join(history[-8:]) if history else ""
    instruction = build_instruction_claim() if is_claim_like(user_text) else build_instruction_query()
    prompt = (
        f"{instruction}\n\n"
        f"Conversation context (last turns):\n{hist_text}\n\n"
        f"User content:\n\"\"\"{user_text}\"\"\""
    )
    model = genai.GenerativeModel("gemini-2.5-flash")
    resp = model.generate_content(prompt)
    return (resp.text or "").strip()

def ask_gemini_images(user_text, images):
    """
    images: list of (mime, bytes) for image/*.
    Sends raw images + text to Gemini 2.5 Flash (multimodal).
    Uses the same claim/query instruction logic.
    """
    instruction = build_instruction_claim() if is_claim_like(user_text) else build_instruction_query()
    parts = [instruction]
    for mime, blob in images:
        parts.append({"mime_type": mime, "data": blob})
    if user_text:
        parts.append(f"Additional user context:\n{user_text}")
    model = genai.GenerativeModel("gemini-2.5-flash")
    resp = model.generate_content(parts)
    return (resp.text or "").strip() or "I couldn't derive a useful analysis from the image(s)."

# -------- Twilio-authenticated media download --------
def auth_get(url, max_retries=3, backoff=0.8):
    """GET Twilio media with Basic Auth + simple retries (media URLs are short-lived)."""
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

    # 1) Collect media (images/PDFs)
    images = []
    pdf_blobs = []
    media_notes = []
    for i, url, mime, blob, err in iter_media(request):
        if err or not blob:
            media_notes.append(f"[Media {i}] {err or 'empty'}")
            continue
        if mime.startswith("image/"):
            images.append((mime, blob))
        elif mime in ("application/pdf", "application/x-pdf", "application/acrobat"):
            pdf_blobs.append(blob)
        else:
            media_notes.append(f"[Media {i}] unsupported type: {mime}")

    # 2) Optional: extract text from PDFs (OCR fallback)
    extracted_from_pdfs = []
    for idx, pdf in enumerate(pdf_blobs):
        text = extract_text_from_pdf(pdf)
        extracted_from_pdfs.append(f"[PDF {idx} text/OCR]\n{text if text.strip() else '(no text found)'}")

    # 3) Add brief context from any URLs the user typed (kept short)
    url_contexts = []
    for u in extract_urls(incoming_text)[:5]:
        page_text = extract_text_from_url(u)
        if page_text:
            url_contexts.append(f"[Extracted from {u}]\n{page_text}")

    # 4) Build unified context
    context_bits = []
    if incoming_text: context_bits.append(incoming_text)
    if extracted_from_pdfs: context_bits.append("\n\n".join(extracted_from_pdfs))
    if media_notes: context_bits.append("\n".join(media_notes))
    if url_contexts: context_bits.append("\n\n".join(url_contexts))
    combined_context = "\n\n".join(context_bits).strip()

    # 5) Ask Gemini (images if available, else text)
    try:
        if images:
            model_reply = ask_gemini_images(combined_context, images)
        else:
            model_reply = ask_gemini_text(combined_context or "User sent an empty message.", history)
    except Exception as e:
        model_reply = f"I couldn’t analyze the message due to an internal error: {e}"

    # 6) Filter sources to allowed domains; if none present, keep as-is
    urls_in_reply = filter_urls(extract_urls(model_reply), limit=3)
    if urls_in_reply:
        # Remove any trailing non-allowed URLs by appending a clean Sources footer
        # Find existing 'Sources:' block and trim it off for clarity
        cleaned = re.split(r"\bSources:\b", model_reply, maxsplit=1)[0].strip()
        final_reply = cleaned + "\nSources:\n" + "\n".join(f"- {u}" for u in urls_in_reply)
    else:
        final_reply = model_reply  # Gemini may have said 'Sources: None'

    # 7) Save short history, send chunked WhatsApp messages
    history.append(f"Assistant: {final_reply[:1000]}")
    save_history(user_id, history)

    for chunk in chunk_message(final_reply):
        resp.message(chunk)
    return str(resp)

# -------------------- Entrypoint --------------------
if __name__ == "__main__":
    # Run locally, then expose with: ngrok http 5000
    app.run(host="0.0.0.0", port=5000, debug=True)
