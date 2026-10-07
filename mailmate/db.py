"""SQLite: MailMate's only store of personal data. n8n workflows keep none.

Safety rules live in the schema where SQLite can enforce them, so a bug in Python can't
quietly break them:
- one contact per address (rule 5: never contact the same address twice),
- at most one first email and one follow-up per contact outside test mode,
- the event timeline is append-only.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CONTACT_STATUSES = (
    "new",          # imported, no email drafted yet
    "drafted",      # an email exists and waits for review
    "contacted",    # first email sent
    "followed_up",  # the one follow-up sent
    "replied",
    "bounced",
)

EMAIL_STATUSES = ("draft", "approved", "rejected", "queued", "sent", "failed", "replied", "bounced")
EMAIL_KINDS = ("initial", "follow_up")
BATCH_STATUSES = ("draft", "submitted", "done", "stopped", "failed")
REPLY_CATEGORIES = ("interview_request", "not_interested", "auto_reply", "bounce", "question", "other")
SUPPRESSION_REASONS = ("not_interested", "bounce", "opt_out", "manual")

_statuses = lambda values: ", ".join(f"'{v}'" for v in values)  # noqa: E731

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS contacts (
    id            INTEGER PRIMARY KEY,
    company       TEXT NOT NULL,
    role          TEXT NOT NULL,
    requirements  TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL DEFAULT '',
    job_url       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'new'
                  CHECK (status IN ({_statuses(CONTACT_STATUSES)})),
    source_file   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    version           INTEGER NOT NULL,
    subject           TEXT NOT NULL,
    body              TEXT NOT NULL,   -- with merge fields like {{first_name}}
    created_at        TEXT NOT NULL,
    test_checked_at   TEXT,            -- rule 2: real sending needs a checked test email first
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY,
    template_id   INTEGER NOT NULL REFERENCES templates(id),
    test_mode     INTEGER NOT NULL DEFAULT 1,   -- dry-run is the default
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ({_statuses(BATCH_STATUSES)})),
    created_at    TEXT NOT NULL,
    submitted_at  TEXT
);

CREATE TABLE IF NOT EXISTS emails (
    id                INTEGER PRIMARY KEY,
    contact_id        INTEGER NOT NULL REFERENCES contacts(id),
    template_id       INTEGER NOT NULL REFERENCES templates(id),
    batch_id          INTEGER REFERENCES batches(id),
    kind              TEXT NOT NULL DEFAULT 'initial'
                      CHECK (kind IN ({_statuses(EMAIL_KINDS)})),
    sentence          TEXT NOT NULL DEFAULT '',     -- the LLM's personal line, after checks
    sources           TEXT NOT NULL DEFAULT '[]',   -- JSON: sources W1 cited
    subject           TEXT NOT NULL DEFAULT '',
    body_text         TEXT NOT NULL DEFAULT '',     -- final text exactly as approved
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ({_statuses(EMAIL_STATUSES)})),
    test_mode         INTEGER NOT NULL DEFAULT 1,
    gmail_thread_id   TEXT,
    gmail_message_id  TEXT,                         -- what a follow-up replies to
    created_at        TEXT NOT NULL,
    approved_at       TEXT,
    sent_at           TEXT
);

-- Rule 5 at the database level: per contact, one first email and one follow-up at most.
-- Drafts and rejected emails don't count; a failed send does (it may have gone out).
CREATE UNIQUE INDEX IF NOT EXISTS one_email_per_kind ON emails(contact_id, kind)
    WHERE test_mode = 0 AND status NOT IN ('draft', 'rejected');

CREATE TABLE IF NOT EXISTS replies (
    id               INTEGER PRIMARY KEY,
    email_id         INTEGER REFERENCES emails(id),
    gmail_thread_id  TEXT NOT NULL,
    from_addr        TEXT NOT NULL,
    date             TEXT NOT NULL,
    snippet          TEXT NOT NULL DEFAULT '',
    body_text        TEXT NOT NULL DEFAULT '',
    category         TEXT NOT NULL CHECK (category IN ({_statuses(REPLY_CATEGORIES)})),
    confidence       REAL,
    created_at       TEXT NOT NULL,
    UNIQUE (gmail_thread_id, from_addr, date)       -- re-running sync never duplicates a reply
);

-- Do-not-contact list. The single source of truth: an address here is never emailed,
-- whether or not it is (still) a contact.
CREATE TABLE IF NOT EXISTS suppression (
    email       TEXT PRIMARY KEY COLLATE NOCASE,
    reason      TEXT NOT NULL CHECK (reason IN ({_statuses(SUPPRESSION_REASONS)})),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- Personal settings (name, signature...). Kept here, not in code, because data/ is git-ignored.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Append-only timeline. The dashboard is computed from it.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    contact_id  INTEGER REFERENCES contacts(id),
    email_id    INTEGER REFERENCES emails(id),
    batch_id    INTEGER REFERENCES batches(id),
    detail      TEXT NOT NULL DEFAULT '{{}}',   -- JSON
    created_at  TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status);
CREATE INDEX IF NOT EXISTS idx_events_contact ON events(contact_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def log_event(conn: sqlite3.Connection, kind: str, *, contact_id: int | None = None,
              email_id: int | None = None, batch_id: int | None = None, **detail) -> None:
    conn.execute(
        "INSERT INTO events (kind, contact_id, email_id, batch_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (kind, contact_id, email_id, batch_id, json.dumps(detail), now()),
    )


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def set_settings(conn: sqlite3.Connection, **values: str) -> None:
    with conn:
        for key, value in values.items():
            conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                         " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (key, value.replace("\r\n", "\n").strip(), now()))


def known_contacts(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Lower-cased address -> the existing contact row."""
    return {r["email"].lower(): r for r in conn.execute("SELECT * FROM contacts")}


def suppressed(conn: sqlite3.Connection) -> dict[str, str]:
    """Lower-cased address -> reason it is on the do-not-contact list."""
    return {r["email"].lower(): r["reason"] for r in conn.execute("SELECT email, reason FROM suppression")}


def suppress(conn: sqlite3.Connection, email: str, reason: str, note: str = "") -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO suppression (email, reason, note, created_at) VALUES (?, ?, ?, ?)",
            (email.strip().lower(), reason, note, now()),
        )
        log_event(conn, "suppressed", email=email.strip().lower(), reason=reason)


def add_contacts(conn: sqlite3.Connection, contacts: list, source_file: str) -> tuple[int, list[str]]:
    """Insert contacts in one transaction. Returns (added, skipped addresses).

    Duplicates and do-not-contact are re-checked here, at write time, not only in the preview:
    the list may have changed since the file was checked.
    """
    blocked = suppressed(conn)
    added, skipped = 0, []
    with conn:
        for c in contacts:
            email = c.email.lower()
            if email in blocked:
                skipped.append(email)
                continue
            try:
                cur = conn.execute(
                    "INSERT INTO contacts (company, role, requirements, email, name, job_url, source_file, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (c.company, c.role, c.requirements, email, c.name, c.job_url, source_file, now()),
                )
            except sqlite3.IntegrityError:  # already a contact
                skipped.append(email)
                continue
            log_event(conn, "contact_added", contact_id=cur.lastrowid, source_file=source_file)
            added += 1
    return added, skipped
