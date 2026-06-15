"""
Proposal Generator — Multi-user SaaS
Vercel serverless handler via Mangum
"""
import os
import re
import json
import base64
import hmac
import hashlib
import time
import secrets
from pathlib import Path
from urllib.parse import urlencode
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from contextlib import contextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from mangum import Mangum

import requests as http_requests
import psycopg2
import psycopg2.extras
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY    = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_DB_HOST     = os.environ.get("SUPABASE_DB_HOST", "")
SUPABASE_DB_PASS     = os.environ.get("SUPABASE_DB_PASSWORD", "")
OPENROUTER_API_KEY   = os.environ.get("OPENROUTER_API_KEY", "")
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI", "")
APP_URL              = os.environ.get("APP_URL", "")
SECRET_KEY           = os.environ.get("SECRET_KEY", secrets.token_hex(32))

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send",
]

API_DIR       = Path(__file__).parent
PROJECT_DIR   = API_DIR.parent
TEMPLATE_PATH = PROJECT_DIR / "template.md"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@contextmanager
def get_db():
    conn = psycopg2.connect(
        host=SUPABASE_DB_HOST,
        database="postgres",
        user="postgres",
        password=SUPABASE_DB_PASS,
        port=5432,
        sslmode="require",
        connect_timeout=10,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Auth — verify Supabase JWTs by calling /auth/v1/user
# ---------------------------------------------------------------------------
def verify_supabase_token(token: str) -> dict:
    r = http_requests.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON_KEY},
        timeout=10,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return r.json()

def get_current_user(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization header")
    return verify_supabase_token(authorization.split(" ", 1)[1])

# ---------------------------------------------------------------------------
# OAuth state — HMAC-signed, stateless (no extra DB round-trip)
# ---------------------------------------------------------------------------
def create_oauth_state(user_id: str) -> str:
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + 600})
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), digestmod=hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()

def verify_oauth_state(state: str) -> str:
    try:
        decoded = base64.urlsafe_b64decode(state.encode() + b"==").decode()
        payload, sig = decoded.rsplit("|", 1)
        expected = hmac.new(SECRET_KEY.encode(), payload.encode(), digestmod=hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad sig")
        data = json.loads(payload)
        if data["exp"] < int(time.time()):
            raise ValueError("expired")
        return data["uid"]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid OAuth state: {e}")

# ---------------------------------------------------------------------------
# Google helpers
# ---------------------------------------------------------------------------
def load_user_google_creds(token_json: str) -> Credentials:
    data = json.loads(token_json)
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=GOOGLE_SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
    return creds

def exchange_google_code(code: str) -> dict:
    r = http_requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()

def get_google_email(access_token: str) -> str:
    r = http_requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    return r.json().get("email", "")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Proposal Generator")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------
@app.get("/api/onboarding/status")
def onboarding_status(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM user_profiles WHERE id = %s", (user["id"],))
        profile = cur.fetchone()

    if not profile:
        return {"onboarded": False, "has_fireflies": False, "has_google": False, "webhook_url": None}

    webhook_url = (
        f"{APP_URL}/api/webhook/fireflies?token={profile['webhook_token']}"
        if profile.get("webhook_token") else None
    )
    return {
        "onboarded": bool(profile.get("fireflies_api_key") and profile.get("google_token_json")),
        "has_fireflies": bool(profile.get("fireflies_api_key")),
        "has_google": bool(profile.get("google_token_json")),
        "google_email": profile.get("google_email"),
        "webhook_url": webhook_url,
    }


class FirefliesKeyRequest(BaseModel):
    api_key: str

@app.post("/api/onboarding/fireflies")
def save_fireflies_key(body: FirefliesKeyRequest, user: dict = Depends(get_current_user)):
    api_key = body.api_key.strip()

    # Validate the key against Fireflies
    r = http_requests.post(
        "https://api.fireflies.ai/graphql",
        json={"query": "query { user { name email } }"},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code != 200 or r.json().get("errors"):
        raise HTTPException(status_code=400, detail="Invalid Fireflies API key")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_profiles (id, email, fireflies_api_key)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET fireflies_api_key = EXCLUDED.fireflies_api_key
        """, (user["id"], user.get("email", ""), api_key))

    return {"success": True}


@app.get("/api/auth/google")
def start_google_oauth(user: dict = Depends(get_current_user)):
    state = create_oauth_state(user["id"])
    params = urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return {"auth_url": f"https://accounts.google.com/o/oauth2/v2/auth?{params}"}


@app.get("/api/auth/google/callback")
def google_oauth_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
):
    if error:
        return RedirectResponse(f"{APP_URL}/?google_error={error}")
    if not code or not state:
        return RedirectResponse(f"{APP_URL}/?google_error=missing_params")

    user_id = verify_oauth_state(state)

    try:
        token_data = exchange_google_code(code)
    except Exception:
        return RedirectResponse(f"{APP_URL}/?google_error=token_exchange_failed")

    google_email = get_google_email(token_data["access_token"])
    token_json = json.dumps({
        "token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "scopes": GOOGLE_SCOPES,
    })

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_profiles (id, google_token_json, google_email)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO UPDATE
                SET google_token_json = EXCLUDED.google_token_json,
                    google_email      = EXCLUDED.google_email
        """, (user_id, token_json, google_email))

    return RedirectResponse(f"{APP_URL}/?google=connected")

# ---------------------------------------------------------------------------
# Dashboard — proposals
# ---------------------------------------------------------------------------
@app.get("/api/proposals")
def list_proposals(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, meeting_id, meeting_title, status, doc_url,
                   lead_name, lead_email, created_at, sent_at, error_message
            FROM proposals WHERE user_id = %s ORDER BY created_at DESC LIMIT 50
        """, (user["id"],))
        rows = cur.fetchall()
    return {"proposals": [dict(r) for r in rows]}


@app.get("/api/transcripts")
def list_transcripts(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT fireflies_api_key FROM user_profiles WHERE id = %s", (user["id"],))
        profile = cur.fetchone()

    if not profile or not profile["fireflies_api_key"]:
        raise HTTPException(400, "Fireflies API key not configured")

    query = "query { transcripts(limit: 15) { id title date duration organizer_email } }"
    r = http_requests.post(
        "https://api.fireflies.ai/graphql",
        json={"query": query},
        headers={"Authorization": f"Bearer {profile['fireflies_api_key']}", "Content-Type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    return {"transcripts": r.json().get("data", {}).get("transcripts", [])}


class ProcessRequest(BaseModel):
    meeting_id: str

@app.post("/api/process")
def process_manual(body: ProcessRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM user_profiles WHERE id = %s", (user["id"],))
        profile = cur.fetchone()

    if not profile or not profile["fireflies_api_key"]:
        raise HTTPException(400, "Fireflies API key not configured")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO proposals (user_id, meeting_id, status)
            VALUES (%s, %s, 'processing')
            ON CONFLICT (user_id, meeting_id)
            DO UPDATE SET status = 'processing', error_message = NULL
            RETURNING id
        """, (user["id"], body.meeting_id))
        proposal_id = str(cur.fetchone()[0])

    background_tasks.add_task(
        run_proposal_pipeline,
        proposal_id, body.meeting_id, user["id"],
        profile["fireflies_api_key"], profile.get("google_token_json"),
    )
    return {"status": "processing", "proposal_id": proposal_id}


class SendRequest(BaseModel):
    proposal_id: str

@app.post("/api/send")
def send_proposal_email(body: SendRequest, user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM proposals WHERE id = %s AND user_id = %s", (body.proposal_id, user["id"]))
        proposal = cur.fetchone()
        cur.execute("SELECT google_token_json FROM user_profiles WHERE id = %s", (user["id"],))
        profile = cur.fetchone()

    if not proposal:
        raise HTTPException(404, "Proposal not found")
    if not proposal["lead_email"]:
        raise HTTPException(400, "No lead email on this proposal")
    if not profile or not profile["google_token_json"]:
        raise HTTPException(400, "Google account not connected")

    creds = load_user_google_creds(profile["google_token_json"])
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)

    first_name = (proposal["lead_name"] or "there").split()[0]
    msg = MIMEMultipart()
    msg["To"] = proposal["lead_email"]
    msg["Subject"] = "Your Proposal"
    msg.attach(MIMEText(
        f"Hey {first_name},\n\n"
        f"Thanks for your time. Here's the proposal from our conversation:\n\n"
        f"{proposal['doc_url']}\n\n"
        f"Let me know if you have any questions.\n\nBest,",
        "plain",
    ))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE proposals SET status = 'sent', sent_at = NOW() WHERE id = %s", (body.proposal_id,))

    return {"status": "sent"}

# ---------------------------------------------------------------------------
# Fireflies webhook — fires automatically when a meeting is transcribed
# ---------------------------------------------------------------------------
class WebhookPayload(BaseModel):
    meetingId: Optional[str] = None
    meeting_id: Optional[str] = None
    eventType: Optional[str] = None

@app.post("/api/webhook/fireflies")
def fireflies_webhook(
    payload: WebhookPayload,
    background_tasks: BackgroundTasks,
    token: str = Query(None),
):
    if not token:
        raise HTTPException(400, "Missing token")

    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM user_profiles WHERE webhook_token = %s", (token,))
        profile = cur.fetchone()

    if not profile:
        raise HTTPException(404, "Unknown webhook token")

    meeting_id = payload.meetingId or payload.meeting_id
    if not meeting_id:
        return {"status": "ignored", "reason": "no meetingId"}
    if not profile.get("fireflies_api_key"):
        return {"status": "ignored", "reason": "Fireflies key not configured"}

    user_id = str(profile["id"])

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO proposals (user_id, meeting_id, status)
            VALUES (%s, %s, 'processing')
            ON CONFLICT (user_id, meeting_id)
            DO UPDATE SET status = 'processing', error_message = NULL
            RETURNING id
        """, (user_id, meeting_id))
        proposal_id = str(cur.fetchone()[0])

    # Return 200 immediately so Fireflies doesn't time out waiting
    background_tasks.add_task(
        run_proposal_pipeline,
        proposal_id, meeting_id, user_id,
        profile["fireflies_api_key"], profile.get("google_token_json"),
    )
    return {"status": "accepted", "proposal_id": proposal_id}

# ---------------------------------------------------------------------------
# Proposal pipeline
# ---------------------------------------------------------------------------
def run_proposal_pipeline(proposal_id: str, meeting_id: str, user_id: str,
                           fireflies_key: str, google_token_json: str):
    try:
        _process(proposal_id, meeting_id, fireflies_key, google_token_json)
    except Exception as e:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE proposals SET status = 'failed', error_message = %s WHERE id = %s",
                (str(e)[:500], proposal_id),
            )


def _process(proposal_id: str, meeting_id: str, fireflies_key: str, google_token_json: str):
    transcript = _fetch_transcript(meeting_id, fireflies_key)
    if not transcript:
        raise ValueError("Transcript not found")

    meeting_title = transcript.get("title", "Discovery Call")
    sentences     = transcript.get("sentences", [])

    if len(sentences) < 20:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE proposals SET status = 'skipped', error_message = 'Call too short' WHERE id = %s",
                (proposal_id,),
            )
        return

    organizer  = (transcript.get("organizer_email") or "").lower()
    lead_email = None
    for p in (transcript.get("participants") or []):
        email = (p if isinstance(p, str) else "").lower()
        if email and email != organizer:
            lead_email = email
            break

    proposal_data = _extract_with_ai(transcript)

    if "METADATA" in proposal_data and isinstance(proposal_data["METADATA"], dict):
        for k, v in proposal_data["METADATA"].items():
            proposal_data.setdefault(k, v)

    def ensure_str(val):
        return "\n".join(str(i) for i in val) if isinstance(val, list) else (str(val) if val is not None else "")

    if not proposal_data.get("CLIENT_NAME") or "githui" in proposal_data.get("CLIENT_NAME", "").lower():
        org_name = organizer.split("@")[0].lower()
        for sp in (transcript.get("speakers") or []):
            if org_name not in sp.get("name", "").lower():
                proposal_data["CLIENT_NAME"]       = sp.get("name", "Valued Client")
                proposal_data["CLIENT_FIRST_NAME"] = sp.get("name", "there").split()[0]
                break

    proposal_data["PREPARED_DATE"] = datetime.utcnow().strftime("%B %d, %Y")

    filled = _load_template()
    dyn    = proposal_data.get("DYNAMIC_CONTENT") or {}

    if dyn.get("REASONS_WHY_CONTENT"):
        m = re.search(r"(Some reasons why:)(.*?)(With that out of the way)", filled, re.DOTALL)
        if m:
            filled = filled.replace(m.group(2), "\n\n" + ensure_str(dyn["REASONS_WHY_CONTENT"]) + "\n\n")
    if dyn.get("GOALS_CONTENT"):
        filled = _replace_section(filled, "## Your Goals",          ensure_str(dyn["GOALS_CONTENT"]))
    if dyn.get("PROBLEM_FACTORS_CONTENT"):
        filled = _replace_section(filled, "## Problem Factors",      ensure_str(dyn["PROBLEM_FACTORS_CONTENT"]))
    if dyn.get("PROPOSED_SOLUTIONS_CONTENT"):
        filled = _replace_section(filled, "## Proposed Solutions",   ensure_str(dyn["PROPOSED_SOLUTIONS_CONTENT"]))
    if dyn.get("PITCH_GOAL_STATEMENT"):
        proposal_data["PITCH_GOAL_STATEMENT"] = ensure_str(dyn["PITCH_GOAL_STATEMENT"])

    for key, value in proposal_data.items():
        if key in ("DYNAMIC_CONTENT", "METADATA"):
            continue
        filled = filled.replace(f"{{{{{key}}}}}", str(value))
    filled = re.sub(r"\{\{.*?\}\}", "", filled)

    client_name = proposal_data.get("CLIENT_NAME", "Client")
    doc_title   = f"Proposal — {client_name} — {datetime.utcnow().strftime('%Y-%m-%d')}"
    doc_url     = None

    if google_token_json:
        creds   = load_user_google_creds(google_token_json)
        doc_url = _create_google_doc(doc_title, filled, creds)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE proposals
            SET status = %s, meeting_title = %s, doc_url = %s, lead_name = %s, lead_email = %s
            WHERE id = %s
        """, (
            "ready" if doc_url else "ready_no_doc",
            meeting_title, doc_url,
            proposal_data.get("CLIENT_NAME"), lead_email,
            proposal_id,
        ))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fetch_transcript(meeting_id: str, api_key: str) -> dict:
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
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise ValueError(f"Fireflies error: {data['errors']}")
    return data.get("data", {}).get("transcript")


def _load_template() -> str:
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    if "## Template Content" in content:
        return content.split("## Template Content")[1].strip()
    return content


def _replace_section(content: str, header: str, new_content: str) -> str:
    pattern = f"({re.escape(header)})(.*?)(?=\n## |\\Z)"
    m = re.search(pattern, content, re.DOTALL)
    if m:
        return content.replace(m.group(0), f"{header}\n\n{new_content}\n\n")
    return content


_EXTRACTION_PROMPT = """Analyze this discovery call transcript and extract proposal data.

Meeting Title: {title}
{speaker_info}
{summary_info}

TRANSCRIPT:
{transcript_text}

Return JSON only (no markdown fences):
{{
  "CLIENT_NAME": "full name of the client (not the salesperson)",
  "CLIENT_FIRST_NAME": "first name only",
  "PRODUCT_TYPE": "SAAS or SERVICE",
  "PRICING_MODEL": "RETAINER or REV_SHARE",
  "PRICING_CONTEXT": "specific pricing details discussed",
  "CUSTOMER_GOAL_COUNT": "e.g. ten",
  "CUSTOMER_GOAL_TIMEFRAME": "e.g. 3 months",
  "DYNAMIC_CONTENT": {{
    "REASONS_WHY_CONTENT": "3-4 bullet points why client is well positioned",
    "GOALS_CONTENT": "bullet points of what they want to achieve",
    "PROBLEM_FACTORS_CONTENT": "3 paragraphs on their bottlenecks. **bold** key phrases",
    "PROPOSED_SOLUTIONS_CONTENT": "Phase 1: Setup\\nPhase 2: Launch\\nPhase 3: Optimize",
    "PITCH_GOAL_STATEMENT": "one confident sentence on the achievable outcome"
  }}
}}"""


def _extract_with_ai(transcript_data: dict) -> dict:
    from openai import OpenAI

    title     = transcript_data.get("title", "Discovery Call")
    sentences = transcript_data.get("sentences", [])
    speakers  = transcript_data.get("speakers", [])
    organizer = transcript_data.get("organizer_email", "")
    summary   = transcript_data.get("summary") or {}

    transcript_text = " ".join(s.get("text", "") for s in sentences if s.get("text"))
    org_name    = organizer.split("@")[0].lower() if organizer else ""
    client_sp   = next((sp for sp in speakers if org_name not in sp.get("name", "").lower()), None)

    speaker_info = (f"Potential Client: {client_sp['name']}" if client_sp else "")
    if speakers:
        speaker_info += f"\nAll Speakers: {', '.join(s.get('name','?') for s in speakers)}"

    summary_info = ""
    if summary.get("overview"):
        summary_info += f"Overview: {summary['overview']}\n"
    if summary.get("keywords"):
        summary_info += f"Keywords: {', '.join(summary['keywords'][:10])}"

    prompt = _EXTRACTION_PROMPT.format(
        title=title,
        speaker_info=speaker_info,
        summary_info=summary_info,
        transcript_text=transcript_text[:20000],
    )

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)
    resp = client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Extract proposal data from a discovery call. Return only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=2000,
    )

    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").lstrip("JSON").strip()
    return json.loads(raw)


def _create_google_doc(title: str, content: str, creds: Credentials) -> str:
    docs  = build("docs",  "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    doc    = docs.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]
    drive.permissions().create(fileId=doc_id, body={"type": "anyone", "role": "reader"}).execute()

    HEADING_PREFIXES = [("### ", 3), ("## ", 2), ("# ", 1)]
    LEVEL_TO_STYLE   = {1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3"}

    lines          = content.split("\n")
    clean_lines    = []
    heading_ranges = []
    bold_ranges    = []
    idx = 1

    for line in lines:
        stripped      = line.strip()
        heading_level = 0
        work_line     = line

        for prefix, level in HEADING_PREFIXES:
            if stripped.startswith(prefix):
                heading_level = level
                work_line     = stripped[len(prefix):]
                break

        clean_line = ""
        i = 0
        while i < len(work_line):
            if work_line[i:i+2] == "**":
                close = work_line.find("**", i + 2)
                if close != -1:
                    bold_text = work_line[i+2:close]
                    b_start   = idx + len(clean_line)
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
        idx += len(clean_line) + 1

    clean_text = "\n".join(clean_lines)

    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": 1}, "text": clean_text}}]},
    ).execute()

    style_reqs = [
        {"updateParagraphStyle": {
            "range": {"startIndex": s, "endIndex": e},
            "paragraphStyle": {"namedStyleType": LEVEL_TO_STYLE[l]},
            "fields": "namedStyleType",
        }}
        for s, e, l in heading_ranges
    ]
    if style_reqs:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": style_reqs}).execute()

    bold_reqs = [
        {"updateTextStyle": {
            "range": {"startIndex": s, "endIndex": e},
            "textStyle": {"bold": True},
            "fields": "bold",
        }}
        for s, e in bold_ranges
    ]
    if bold_reqs:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": bold_reqs}).execute()

    return f"https://docs.google.com/document/d/{doc_id}/edit"


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "supabase": bool(SUPABASE_URL),
        "openrouter": bool(OPENROUTER_API_KEY),
        "google": bool(GOOGLE_CLIENT_ID),
        "db": bool(SUPABASE_DB_HOST),
    }


handler = Mangum(app, lifespan="off")
