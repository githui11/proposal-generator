"""
Repliix Proposal Engine — Vercel serverless handler.

Stateless API: no database. Frontend stores state in localStorage.
Three endpoints:
  GET  /api/transcripts  — list Fireflies transcripts
  POST /api/process      — process one transcript, return proposal data
  POST /api/send         — send proposal email to lead
"""
import os
import re
import json
import base64
import sys
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from mangum import Mangum

import requests as http_requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
API_DIR = Path(__file__).parent          # /var/task/api/  (or local api/)
PROJECT_DIR = API_DIR.parent             # /var/task/      (project root)
TEMPLATE_PATH = PROJECT_DIR / "template.md"

FIREFLIES_API_KEY = os.environ.get("FIREFLIES_API_KEY") or os.environ.get("Fireflies_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Repliix Proposal Engine")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Google credentials (from GOOGLE_TOKEN_JSON env var)
# ---------------------------------------------------------------------------
def load_google_creds():
    token_json_str = os.environ.get("GOOGLE_TOKEN_JSON")
    if not token_json_str:
        raise ValueError("GOOGLE_TOKEN_JSON env var not set")
    token_data = json.loads(token_json_str)
    creds = Credentials.from_authorized_user_info(token_data)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

# ---------------------------------------------------------------------------
# Fireflies API
# ---------------------------------------------------------------------------
def fetch_fireflies_list(limit=15):
    query = "query { transcripts(limit: %d) { id title date duration organizer_email } }" % limit
    r = http_requests.post(
        "https://api.fireflies.ai/graphql",
        json={"query": query},
        headers={"Authorization": f"Bearer {FIREFLIES_API_KEY}", "Content-Type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("data", {}).get("transcripts", [])


def fetch_fireflies_transcript(meeting_id):
    query = """
    query Transcript($transcriptId: String!) {
        transcript(id: $transcriptId) {
            id title
            sentences { text speaker_name }
            speakers { name }
            participants
            summary { overview keywords }
            organizer_email date duration
        }
    }
    """
    r = http_requests.post(
        "https://api.fireflies.ai/graphql",
        json={"query": query, "variables": {"transcriptId": meeting_id}},
        headers={"Authorization": f"Bearer {FIREFLIES_API_KEY}", "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", {}).get("transcript")

# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------
def load_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    if "## Template Content" in content:
        return content.split("## Template Content")[1].strip()
    return content


def replace_section(content, header, new_content):
    pattern = f"({re.escape(header)})(.*?)(?=\n## |\\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return content.replace(match.group(0), f"{header}\n\n{new_content}\n\n")
    return content

# ---------------------------------------------------------------------------
# Google Docs creation
# ---------------------------------------------------------------------------
def create_google_doc(title, content):
    creds = load_google_creds()
    docs = build("docs", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    doc = docs.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]
    drive.permissions().create(fileId=doc_id, body={"type": "anyone", "role": "reader"}).execute()

    # Parse markdown: strip # and ** markers, track positions on the clean text.
    # Google Docs indices are 1-based; the insert lands at index 1.
    HEADING_PREFIXES = [("### ", 3), ("## ", 2), ("# ", 1)]
    LEVEL_TO_STYLE = {1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3"}

    lines = content.split("\n")
    clean_lines = []
    heading_ranges = []  # (start_idx, end_idx, level)
    bold_ranges = []     # (start_idx, end_idx)
    idx = 1

    for line in lines:
        stripped = line.strip()
        heading_level = 0
        work_line = line

        for prefix, level in HEADING_PREFIXES:
            if stripped.startswith(prefix):
                heading_level = level
                work_line = stripped[len(prefix):]
                break

        # Strip bold markers, record absolute positions in the final doc
        clean_line = ""
        i = 0
        while i < len(work_line):
            if work_line[i:i+2] == "**":
                close = work_line.find("**", i + 2)
                if close != -1:
                    bold_text = work_line[i+2:close]
                    b_start = idx + len(clean_line)
                    clean_line += bold_text
                    bold_ranges.append((b_start, b_start + len(bold_text)))
                    i = close + 2
                else:
                    clean_line += "**"
                    i += 2
            else:
                clean_line += work_line[i]
                i += 1

        if heading_level:
            heading_ranges.append((idx, idx + len(clean_line), heading_level))

        clean_lines.append(clean_line)
        idx += len(clean_line) + 1  # +1 for the newline character

    clean_text = "\n".join(clean_lines)

    # Step 1: Insert clean text (no markdown markers)
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": 1}, "text": clean_text}}]},
    ).execute()

    # Step 2: Apply heading paragraph styles
    style_requests = [
        {"updateParagraphStyle": {
            "range": {"startIndex": start, "endIndex": end},
            "paragraphStyle": {"namedStyleType": LEVEL_TO_STYLE[level]},
            "fields": "namedStyleType",
        }}
        for start, end, level in heading_ranges
    ]
    if style_requests:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": style_requests}).execute()

    # Step 3: Apply bold — must be last (any style update after this resets bold)
    bold_requests = [
        {"updateTextStyle": {
            "range": {"startIndex": start, "endIndex": end},
            "textStyle": {"bold": True},
            "fields": "bold",
        }}
        for start, end in bold_ranges
    ]
    if bold_requests:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": bold_requests}).execute()

    return f"https://docs.google.com/document/d/{doc_id}/edit"

# ---------------------------------------------------------------------------
# AI extraction
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = """Analyze this discovery call transcript and extract data for a Lead Generation Proposal.

Meeting Title: {title}
{speaker_info}
{summary_info}

TRANSCRIPT:
{transcript_text}

**CRITICAL**: "Githui" or "Githui Kiuna" is the SALESPERSON. Do NOT extract him as CLIENT_NAME.

Return JSON only:
{{
  "CLIENT_NAME": "full name of client (not Githui)",
  "CLIENT_FIRST_NAME": "first name only",
  "PRODUCT_TYPE": "SAAS or SERVICE",
  "PRICING_MODEL": "RETAINER or REV_SHARE",
  "PRICING_CONTEXT": "specific pricing details",
  "CUSTOMER_GOAL_COUNT": "e.g. ten",
  "CUSTOMER_GOAL_TIMEFRAME": "e.g. 3 months",
  "DYNAMIC_CONTENT": {{
    "REASONS_WHY_CONTENT": "3-4 bullet points why client is well positioned",
    "GOALS_CONTENT": "bullet points of what they want to achieve",
    "PROBLEM_FACTORS_CONTENT": "3 paragraphs on specific bottlenecks. **bold** key phrases",
    "PROPOSED_SOLUTIONS_CONTENT": "Phase 1: Setup, Phase 2: Launch, Phase 3: Optimize",
    "PITCH_GOAL_STATEMENT": "one confident sentence on achievable outcome"
  }}
}}"""


def extract_proposal_data(transcript_data):
    from openai import OpenAI

    title = transcript_data.get("title", "Discovery Call")
    sentences = transcript_data.get("sentences", [])
    speakers = transcript_data.get("speakers", [])
    organizer_email = transcript_data.get("organizer_email", "")
    summary = transcript_data.get("summary") or {}

    transcript_text = " ".join(s.get("text", "") for s in sentences if s.get("text"))

    organizer_name = organizer_email.split("@")[0].lower() if organizer_email else ""
    client_speaker = next(
        (sp for sp in speakers if organizer_name not in sp.get("name", "").lower()),
        None
    )

    speaker_info = f"Potential Client: {client_speaker['name']}" if client_speaker else ""
    if speakers:
        speaker_info += f"\nAll Speakers: {', '.join(s.get('name','?') for s in speakers)}"

    summary_info = ""
    if summary.get("overview"):
        summary_info += f"Overview: {summary['overview']}\n"
    if summary.get("keywords"):
        summary_info += f"Keywords: {', '.join(summary['keywords'][:10])}"

    prompt = EXTRACTION_PROMPT.format(
        title=title,
        speaker_info=speaker_info,
        summary_info=summary_info,
        transcript_text=transcript_text[:20000],
    )

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)
    resp = client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a sales ops expert. Extract proposal data from transcripts. Return only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=2000,
    )

    result_text = resp.choices[0].message.content.strip()
    if result_text.startswith("```"):
        result_text = result_text.split("```")[1].replace("json", "").replace("JSON", "").strip()
    return json.loads(result_text)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/transcripts")
def list_transcripts():
    if not FIREFLIES_API_KEY:
        raise HTTPException(status_code=500, detail="FIREFLIES_API_KEY not configured")
    try:
        items = fetch_fireflies_list(limit=15)
        return {"transcripts": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ProcessRequest(BaseModel):
    meeting_id: str

@app.post("/api/process")
def process_transcript(body: ProcessRequest):
    if not FIREFLIES_API_KEY:
        raise HTTPException(status_code=500, detail="FIREFLIES_API_KEY not configured")
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY not configured")

    meeting_id = body.meeting_id

    transcript_data = fetch_fireflies_transcript(meeting_id)
    if not transcript_data:
        raise HTTPException(status_code=404, detail="Transcript not found")

    sentences = transcript_data.get("sentences", [])
    transcript_text = " ".join(s.get("text", "") for s in sentences)
    if len(sentences) < 20 and len(transcript_text) < 500:
        return {"status": "skipped", "reason": "Call too short"}

    # Lead email from participants (now [String!] — plain email strings, no subfields)
    organizer = (transcript_data.get("organizer_email") or "").lower()
    lead_email = None
    for p in (transcript_data.get("participants") or []):
        email = (p if isinstance(p, str) else "").lower()
        if email and email != organizer:
            lead_email = email
            break
    lead_name = None

    # AI extraction
    proposal_data = extract_proposal_data(transcript_data)

    # Normalize nested METADATA
    if "METADATA" in proposal_data and isinstance(proposal_data["METADATA"], dict):
        for k, v in proposal_data["METADATA"].items():
            proposal_data.setdefault(k, v)

    # Override client name if AI got it wrong
    extracted_name = proposal_data.get("CLIENT_NAME", "")
    if not extracted_name or "Githui" in extracted_name:
        speakers = transcript_data.get("speakers", [])
        organizer_name = organizer.split("@")[0].lower() if organizer else ""
        for sp in speakers:
            if organizer_name not in sp.get("name", "").lower():
                proposal_data["CLIENT_NAME"] = sp.get("name", "Valued Client")
                proposal_data["CLIENT_FIRST_NAME"] = sp.get("name", "there").split()[0]
                break

    if lead_name and ("Githui" in proposal_data.get("CLIENT_NAME", "") or not proposal_data.get("CLIENT_NAME")):
        proposal_data["CLIENT_NAME"] = lead_name
        proposal_data["CLIENT_FIRST_NAME"] = lead_name.split()[0]

    from datetime import datetime
    proposal_data["PREPARED_DATE"] = datetime.utcnow().strftime("%B %d, %Y")

    # Fill template
    filled = load_template()
    dyn = proposal_data.get("DYNAMIC_CONTENT") or {}

    def ensure_str(val):
        if isinstance(val, list):
            return "\n".join(str(item) for item in val)
        return str(val) if val is not None else ""

    if dyn.get("REASONS_WHY_CONTENT"):
        pattern = r"(Some reasons why:)(.*?)(With that out of the way)"
        match = re.search(pattern, filled, re.DOTALL)
        if match:
            filled = filled.replace(match.group(2), "\n\n" + ensure_str(dyn["REASONS_WHY_CONTENT"]) + "\n\n")

    if dyn.get("GOALS_CONTENT"):
        filled = replace_section(filled, "## Your Goals", ensure_str(dyn["GOALS_CONTENT"]))
    if dyn.get("PROBLEM_FACTORS_CONTENT"):
        filled = replace_section(filled, "## Problem Factors", ensure_str(dyn["PROBLEM_FACTORS_CONTENT"]))
    if dyn.get("PROPOSED_SOLUTIONS_CONTENT"):
        filled = replace_section(filled, "## Proposed Solutions", ensure_str(dyn["PROPOSED_SOLUTIONS_CONTENT"]))
    if dyn.get("PITCH_GOAL_STATEMENT"):
        proposal_data["PITCH_GOAL_STATEMENT"] = ensure_str(dyn["PITCH_GOAL_STATEMENT"])

    for key, value in proposal_data.items():
        if key in ("DYNAMIC_CONTENT", "METADATA"):
            continue
        filled = filled.replace(f"{{{{{key}}}}}", str(value))

    filled = re.sub(r"\{\{.*?\}\}", "", filled)

    # Create Google Doc
    client_name = proposal_data.get("CLIENT_NAME", "Client")
    doc_title = f"Proposal - {client_name} - {datetime.utcnow().strftime('%Y-%m-%d')} [TEST DATA]"
    doc_url = create_google_doc(doc_title, filled)

    return {
        "status": "ready",
        "doc_url": doc_url,
        "lead_name": lead_name or client_name,
        "lead_email": lead_email,
    }


class SendRequest(BaseModel):
    lead_name: str
    lead_email: str
    doc_url: str

@app.post("/api/send")
def send_proposal(body: SendRequest):
    first_name = body.lead_name.split()[0] if body.lead_name else "there"

    creds = load_google_creds()
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)

    msg = MIMEMultipart()
    msg["To"] = body.lead_email
    msg["Subject"] = "Your Repliix Proposal"
    email_body = (
        f"Hey {first_name},\n\n"
        f"Thanks for your time today. I've put together a proposal based on our conversation.\n\n"
        f"You can view it here: {body.doc_url}\n\n"
        f"Let me know if you have any questions.\n\n"
        f"Best,\nGithui\nRepliix | repliix.com"
    )
    msg.attach(MIMEText(email_body, "plain"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()

    from datetime import datetime
    return {"status": "sent", "sent_at": datetime.utcnow().isoformat()}


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "fireflies": bool(FIREFLIES_API_KEY),
        "openrouter": bool(OPENROUTER_API_KEY),
        "google_token": bool(os.environ.get("GOOGLE_TOKEN_JSON")),
    }


# Vercel handler
handler = Mangum(app, lifespan="off")
