import os
import re
import hashlib
import json
from typing import List
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import google.generativeai as genai
from google.generativeai.types import GenerationConfig

load_dotenv()
app = FastAPI(title="Gemini Health Verifier")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not found in .env")

genai.configure(api_key=GEMINI_API_KEY)

ALLOWED_DOMAINS = [
    "who.int", "cdc.gov", "icmr.gov.in", "mohfw.gov.in", "fda.gov",
    "ema.europa.eu", "nice.org.uk", "nih.gov", "ncbi.nlm.nih.gov",
    "cochranelibrary.com", "bmj.com", "thelancet.com", "nature.com"
]

class Link(BaseModel):
    title: str
    url: str

class VerifyReq(BaseModel):
    text: str

class VerifyResp(BaseModel):
    id: str
    verdict: str  # True | False | Misleading | Unclear
    summary: str
    links: List[Link]

SYSTEM_PROMPT = """You are a health fact-checker that verifies claims using only authoritative sources.
Rules:
1. Evaluate if the claim is "True", "False", "Misleading", or "Unclear"
2. Provide a concise 5 sentence summary explaining your verdict
3. Include up to 3 supporting links ONLY from these domains:
   - who.int, cdc.gov, icmr.gov.in, mohfw.gov.in, fda.gov
   - ema.europa.eu, nice.org.uk
   - cochranelibrary.com, bmj.com, thelancet.com, nature.com
4. If unsure or no valid sources found, return "Unclear" with empty links

Return valid JSON format like this:
{
  "verdict": "True|False|Misleading|Unclear",
  "summary": "Your explanation...",
  "links": [{"title": "...", "url": "..."}]
}"""

def build_prompt(claim: str) -> str:
    return f"{SYSTEM_PROMPT}\n\nClaim to verify:\n{claim}"

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def is_allowed_domain(url: str) -> bool:
    try:
        domain = url.split('//')[1].split('/')[0].lower()
        return any(domain.endswith(allowed) for allowed in ALLOWED_DOMAINS)
    except:
        return False

@app.post("/verify", response_model=VerifyResp)
async def verify_claim(request: VerifyReq):
    claim = normalize(request.text)
    claim_id = hashlib.sha256(claim.encode()).hexdigest()[:12]
    
    if not claim:
        return VerifyResp(
            id=claim_id,
            verdict="Unclear",
            summary="Empty claim provided",
            links=[]
        )
    
    try:
        model = genai.GenerativeModel('gemini-2.0-flash')
        response = model.generate_content(
            build_prompt(claim),
            generation_config=GenerationConfig(
                temperature=0.2,
                candidate_count=1,
                response_mime_type="application/json"
            )
        )
        
        try:
            result = json.loads(response.text)
 
            if not all(key in result for key in ["verdict", "summary", "links"]):
                raise ValueError("Missing required fields in response")
    
            valid_links = []
            for link in result.get("links", [])[:3]: 
                if not isinstance(link, dict):
                    continue
                    
                url = link.get("url", "").strip()
                title = link.get("title", url).strip()[:200]
                
                if url and is_allowed_domain(url):
                    valid_links.append({"title": title, "url": url})
            
            verdict = result["verdict"]
            if verdict not in ["True", "False", "Misleading", "Unclear"]:
                verdict = "Unclear"
            if not valid_links and verdict != "Unclear":
                verdict = "Unclear"
            
            return VerifyResp(
                id=claim_id,
                verdict=verdict,
                summary=result["summary"],
                links=valid_links
            )
            
        except (json.JSONDecodeError, ValueError) as e:
            return VerifyResp(
                id=claim_id,
                verdict="Unclear",
                summary=f"Error parsing response: {str(e)}",
                links=[]
            )
            
    except Exception as e:
        return VerifyResp(
            id=claim_id,
            verdict="Unclear",
            summary=f"API error: {str(e)}",
            links=[]
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)