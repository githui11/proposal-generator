"""
Run once to create the database schema in Supabase Postgres.
Usage: py setup_db.py
"""
import os
import psycopg2
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

conn = psycopg2.connect(
    host=os.environ["SUPABASE_DB_HOST"],
    database="postgres",
    user="postgres",
    password=os.environ["SUPABASE_DB_PASSWORD"],
    port=5432,
    sslmode="require",
    connect_timeout=15,
)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS user_profiles (
    id          UUID PRIMARY KEY,
    email       TEXT,
    fireflies_api_key TEXT,
    webhook_token TEXT UNIQUE DEFAULT gen_random_uuid()::TEXT,
    google_token_json TEXT,
    google_email TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS proposals (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL,
    meeting_id   TEXT NOT NULL,
    meeting_title TEXT,
    status       TEXT DEFAULT 'processing',
    doc_url      TEXT,
    lead_name    TEXT,
    lead_email   TEXT,
    error_message TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    sent_at      TIMESTAMPTZ,
    UNIQUE(user_id, meeting_id)
);
""")

cur.execute("CREATE INDEX IF NOT EXISTS idx_proposals_user_id ON proposals(user_id);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_profiles_webhook_token ON user_profiles(webhook_token);")

conn.commit()
conn.close()
print("Database schema created successfully.")
