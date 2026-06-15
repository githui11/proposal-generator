"""
Repliix Proposal Engine — local web app for Loom demo.

Fetches Fireflies transcripts, generates proposals via OpenRouter + Google Docs,
and sends them to leads via Gmail. All timestamps recorded for demo visibility.

Run: py app.py
Open: http://localhost:8000
"""
import os
import re
import json
import sqlite3
import base64
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

# Google
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

BASE_DIR = Path(__file__).parent

# Load .env from app directory first, then fallback to parent (repliix/.env)
_app_env = BASE_DIR / ".env"
_parent_env = BASE_DIR.parent / ".env"
if _app_env.exists():
    load_dotenv(dotenv_path=_app_env)
elif _parent_env.exists():
    load_dotenv(dotenv_path=_parent_env)
PARENT_DIR = BASE_DIR.parent
TOKEN_PATH = PARENT_DIR / "token.json"
TEMPLATE_PATH = PARENT_DIR / "directives" / "lead_gen_proposal_template.md"
DB_PATH = BASE_DIR / "proposals.db"

FIREFLIES_API_KEY = os.getenv("FIREFLIES_API_KEY") or os.getenv("Fireflies_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY,
                title TEXT,
                original_date TEXT,
                received_at TEXT,
                processed_at TEXT,
                sent_at TEXT,
                lead_name TEXT,
                lead_email TEXT,
                doc_url TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        conn.commit()

# ---------------------------------------------------------------------------
# Google credentials
# ---------------------------------------------------------------------------

def load_google_creds():
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(f"token.json not found at {TOKEN_PATH}")
    with open(TOKEN_PATH) as f:
        token_data = json.load(f)
    creds = Credentials.from_authorized_user_info(token_data)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

# ---------------------------------------------------------------------------
# Fireflies API
# ---------------------------------------------------------------------------

def fetch_fireflies_list(limit=20):
    query = """
    query {
        transcripts(limit: %d) {
            id
            title
            date
            duration
            organizer_email
        }
    }
    """ % limit
    r = requests.post(
        "https://api.fireflies.ai/graphql",
        json={"query": query},
        headers={"Authorization": f"Bearer {FIREFLIES_API_KEY}", "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", {}).get("transcripts", [])


def fetch_fireflies_transcript(meeting_id):
    query = """
    query Transcript($transcriptId: String!) {
        transcript(id: $transcriptId) {
            id
            title
            sentences { text speaker_name }
            speakers { name }
            participants
            summary { overview action_items keywords }
            organizer_email
            date
            duration
        }
    }
    """
    r = requests.post(
        "https://api.fireflies.ai/graphql",
        json={"query": query, "variables": {"transcriptId": meeting_id}},
        headers={"Authorization": f"Bearer {FIREFLIES_API_KEY}", "Content-Type": "application/json"},
        timeout=60,
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

    # Insert text
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
    ).execute()

    # Heading styles
    lines = content.split("\n")
    idx = 1
    style_requests = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            style_requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": idx, "endIndex": idx + len(line)},
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "fields": "namedStyleType",
                }
            })
        elif stripped.startswith("## "):
            style_requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": idx, "endIndex": idx + len(line)},
                    "paragraphStyle": {"namedStyleType": "HEADING_2"},
                    "fields": "namedStyleType",
                }
            })
        elif stripped.startswith("### "):
            style_requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": idx, "endIndex": idx + len(line)},
                    "paragraphStyle": {"namedStyleType": "HEADING_3"},
                    "fields": "namedStyleType",
                }
            })
        idx += len(line) + 1

    if style_requests:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": style_requests}).execute()

    # Bold **text** (must be last)
    bold_requests = []
    lines = content.split("\n")
    idx = 1
    for line in lines:
        for m in re.finditer(r"\*\*(.+?)\*\*", line):
            bold_requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": idx + m.start(), "endIndex": idx + m.end()},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })
        idx += len(line) + 1

    if bold_requests:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": bold_requests}).execute()

    return f"https://docs.google.com/document/d/{doc_id}/edit"

# ---------------------------------------------------------------------------
# AI extraction
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT_TEMPLATE = """Analyze this discovery call transcript and extract data to fill a Lead Generation Proposal.

Meeting Title: {title}
{speaker_info}
{summary_info}

TRANSCRIPT:
{transcript_text}

You are generating content for a professional B2B proposal. The content must be specific to the conversation.
Do NOT hallucinate SaaS products if they weren't discussed.

**CRITICAL**: The user "Githui" or "Githui Kiuna" is the SALESPERSON. Do NOT extract him as CLIENT_NAME. The client is the other person.

Extract the following in JSON format:

1. METADATA:
   - "CLIENT_NAME": Full name of the potential client (NOT Githui).
   - "CLIENT_FIRST_NAME": First name only.
   - "PRODUCT_TYPE": "SAAS" or "SERVICE".
   - "PRICING_MODEL": "RETAINER" or "REV_SHARE".
   - "PRICING_CONTEXT": specific pricing/commission details discussed.

2. DYNAMIC_CONTENT (Markdown, ready to insert):
   - "REASONS_WHY_CONTENT": 3-4 bullet points why client is well positioned for growth.
   - "GOALS_CONTENT": Bullet points of what they want to achieve.
   - "PROBLEM_FACTORS_CONTENT": 3 paragraphs describing specific bottlenecks. Bold key phrases.
   - "PROPOSED_SOLUTIONS_CONTENT": 3 Phases (Phase 1: Setup, Phase 2: Launch, Phase 3: Optimize).
   - "PITCH_GOAL_STATEMENT": One confident sentence summarizing the achievable outcome.

3. PLACEHOLDERS:
   - "CUSTOMER_GOAL_COUNT": e.g. "10", "five"
   - "CUSTOMER_GOAL_TIMEFRAME": e.g. "3 months"

Return only valid JSON:"""


def extract_proposal_data(transcript_data):
    from openai import OpenAI

    title = transcript_data.get("title", "Discovery Call")
    sentences = transcript_data.get("sentences", [])
    speakers = transcript_data.get("speakers", [])
    organizer_email = transcript_data.get("organizer_email", "")
    summary = transcript_data.get("summary") or {}

    transcript_text = " ".join(s.get("text", "") for s in sentences if s.get("text"))

    organizer_name = organizer_email.split("@")[0].lower() if organizer_email else ""
    client_speaker = None
    for sp in speakers:
        if organizer_name and organizer_name in sp.get("name", "").lower():
            continue
        client_speaker = sp
        break

    speaker_info = ""
    if client_speaker:
        speaker_info = f"Potential Client: {client_speaker.get('name', '')}"
    if speakers:
        speaker_info += f"\nAll Speakers: {', '.join(s.get('name', 'Unknown') for s in speakers)}"

    summary_info = ""
    if summary.get("overview"):
        summary_info += f"Overview: {summary['overview']}\n"
    if summary.get("keywords"):
        summary_info += f"Keywords: {', '.join(summary['keywords'][:10])}"

    prompt = EXTRACTION_PROMPT_TEMPLATE.format(
        title=title,
        speaker_info=speaker_info,
        summary_info=summary_info,
        transcript_text=transcript_text[:25000],
    )

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)
    resp = client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a sales operations expert. Extract proposal data from transcripts. Return only valid JSON."},
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
# Full proposal pipeline (runs in background thread)
# ---------------------------------------------------------------------------

executor = ThreadPoolExecutor(max_workers=3)


def process_proposal(meeting_id: str):
    def _run():
        with get_db() as conn:
            conn.execute("UPDATE proposals SET status='processing' WHERE id=?", (meeting_id,))
            conn.commit()

        try:
            transcript_data = fetch_fireflies_transcript(meeting_id)
            if not transcript_data:
                _fail(meeting_id, "Transcript not found in Fireflies")
                return

            sentences = transcript_data.get("sentences", [])
            transcript_text = " ".join(s.get("text", "") for s in sentences)
            if len(sentences) < 20 and len(transcript_text) < 500:
                with get_db() as conn:
                    conn.execute("UPDATE proposals SET status='skipped' WHERE id=?", (meeting_id,))
                    conn.commit()
                return

            # Extract lead email from participants (now [String!] — email strings only)
            organizer = (transcript_data.get("organizer_email") or "").lower()
            lead_email = None
            lead_name_from_participants = None
            for p in (transcript_data.get("participants") or []):
                # participants is now a list of email strings
                email = (p if isinstance(p, str) else p.get("email", "")).lower()
                if email and email != organizer:
                    lead_email = email
                    break

            # AI extraction
            proposal_data = extract_proposal_data(transcript_data)

            # Normalize nested METADATA if AI returned it nested
            if "METADATA" in proposal_data and isinstance(proposal_data["METADATA"], dict):
                for k, v in proposal_data["METADATA"].items():
                    proposal_data[k] = v

            # Override client name with participant data if available
            extracted_name = proposal_data.get("CLIENT_NAME", "")
            if not extracted_name or "Githui" in extracted_name or extracted_name == "Client":
                speakers = transcript_data.get("speakers", [])
                organizer_name = organizer.split("@")[0].lower() if organizer else ""
                for sp in speakers:
                    if organizer_name and organizer_name in sp.get("name", "").lower():
                        continue
                    proposal_data["CLIENT_NAME"] = sp.get("name", "Valued Client")
                    proposal_data["CLIENT_FIRST_NAME"] = sp.get("name", "there").split()[0]
                    break

            if lead_name_from_participants and (not proposal_data.get("CLIENT_NAME") or "Githui" in proposal_data.get("CLIENT_NAME", "")):
                proposal_data["CLIENT_NAME"] = lead_name_from_participants
                proposal_data["CLIENT_FIRST_NAME"] = lead_name_from_participants.split()[0]

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

            now_iso = datetime.utcnow().isoformat()
            with get_db() as conn:
                conn.execute(
                    "UPDATE proposals SET status='ready', processed_at=?, doc_url=?, lead_name=?, lead_email=? WHERE id=?",
                    (now_iso, doc_url, client_name, lead_email, meeting_id),
                )
                conn.commit()

        except Exception as e:
            _fail(meeting_id, str(e))

    executor.submit(_run)


def _fail(meeting_id, error):
    print(f"[ERROR] {meeting_id}: {error}")
    with get_db() as conn:
        conn.execute("UPDATE proposals SET status='failed' WHERE id=?", (meeting_id,))
        conn.commit()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Repliix Proposal Engine")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
def root():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/proposals")
def list_proposals():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM proposals ORDER BY received_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/fetch")
def fetch_transcripts():
    """Pull latest transcripts from Fireflies, insert new ones, start processing."""
    if not FIREFLIES_API_KEY:
        raise HTTPException(status_code=500, detail="FIREFLIES_API_KEY not set")

    transcripts = fetch_fireflies_list(limit=20)

    with get_db() as conn:
        existing_ids = {row[0] for row in conn.execute("SELECT id FROM proposals").fetchall()}

    new_ones = [t for t in transcripts if t["id"] not in existing_ids]
    if not new_ones:
        return {"added": 0, "message": "No new transcripts found"}

    now = datetime.utcnow()
    inserted = []
    with get_db() as conn:
        for i, t in enumerate(new_ones):
            # Stagger received_at so they look like a live feed arriving over the past few minutes
            received_at = (now - timedelta(seconds=i * 45)).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO proposals (id, title, original_date, received_at, status) VALUES (?,?,?,?,'pending')",
                (t["id"], t.get("title", "Discovery Call"), t.get("date", ""), received_at),
            )
            inserted.append(t["id"])
        conn.commit()

    # Start background processing for each
    for mid in inserted:
        process_proposal(mid)

    return {"added": len(inserted), "ids": inserted}


@app.get("/api/proposals/{meeting_id}")
def get_proposal(meeting_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM proposals WHERE id=?", (meeting_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return dict(row)


@app.post("/api/send/{meeting_id}")
def send_proposal(meeting_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM proposals WHERE id=?", (meeting_id,)).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Proposal not found")

    p = dict(row)
    if p["status"] not in ("ready", "failed"):
        raise HTTPException(status_code=400, detail=f"Proposal status is '{p['status']}' — not ready to send")
    if not p.get("lead_email"):
        raise HTTPException(status_code=400, detail="No lead email found for this proposal")
    if not p.get("doc_url"):
        raise HTTPException(status_code=400, detail="No doc URL — proposal not yet generated")

    lead_name = p.get("lead_name") or "there"
    first_name = lead_name.split()[0]
    lead_email = p["lead_email"]
    doc_url = p["doc_url"]

    creds = load_google_creds()
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)

    msg = MIMEMultipart()
    msg["To"] = lead_email
    msg["Subject"] = "Your Repliix Proposal"
    body = (
        f"Hey {first_name},\n\n"
        f"Thanks for your time today. I've put together a proposal based on our conversation.\n\n"
        f"You can view it here: {doc_url}\n\n"
        f"Let me know if you have any questions or want to adjust anything.\n\n"
        f"Best,\nGithui\nRepliix | repliix.com"
    )
    msg.attach(MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()

    sent_at = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE proposals SET status='sent', sent_at=? WHERE id=?",
            (sent_at, meeting_id),
        )
        conn.commit()

    return {"status": "sent", "to": lead_email, "sent_at": sent_at}


@app.post("/api/reprocess/{meeting_id}")
def reprocess_proposal(meeting_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT id FROM proposals WHERE id=?", (meeting_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Proposal not found")
    with get_db() as conn:
        conn.execute(
            "UPDATE proposals SET status='pending', processed_at=NULL, doc_url=NULL, sent_at=NULL WHERE id=?",
            (meeting_id,),
        )
        conn.commit()
    process_proposal(meeting_id)
    return {"status": "reprocessing"}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def startup():
    init_db()
    print(f"[Repliix Proposal Engine] Running at http://localhost:8000")
    print(f"  Token:    {TOKEN_PATH}")
    print(f"  Template: {TEMPLATE_PATH}")
    print(f"  DB:       {DB_PATH}")
    if not FIREFLIES_API_KEY:
        print("  [WARNING] FIREFLIES_API_KEY not set — check ../.env")
    if not OPENROUTER_API_KEY:
        print("  [WARNING] OPENROUTER_API_KEY not set — check ../.env")


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
