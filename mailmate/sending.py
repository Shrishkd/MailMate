"""Handing emails to W2. Step 6: test batches only, to my own secondary inbox (rule 2).

A test batch sends the exact text of 1-3 reviewed emails (same template version) to the test
address, with the resume attached. It doesn't use up anyone's one email and doesn't change the
emails it copies. After I've checked what arrived (PDF, label, no n8n footer), I mark that
template version as test-checked; real sending (Step 7) requires that mark.
"""

import json
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from mailmate import db
from mailmate.n8n import N8nClient, N8nError

MAX_TEST_EMAILS = 3
TEST_SPACING_S = (60, 120)          # short gaps, so a test shows the throttling without a long wait
MAX_RESUME_BYTES = 5 * 1024 * 1024
RESUME_WARN_BYTES = 1024 * 1024     # bigger attachments look more like spam


class SendError(ValueError):
    """Something the rules don't allow, or a missing piece. The message says what to do."""


# --- resume -------------------------------------------------------------------------------

def check_resume(data: bytes) -> list[str]:
    """Warnings for a valid PDF. Raises SendError if it can't be used."""
    if not data.startswith(b"%PDF-"):
        raise SendError("that file isn't a PDF")
    if len(data) > MAX_RESUME_BYTES:
        raise SendError(f"the PDF is {len(data) / 1024 / 1024:.1f} MB; keep it under 1 MB (5 MB at most)")
    if len(data) > RESUME_WARN_BYTES:
        return [f"the PDF is {len(data) / 1024 / 1024:.1f} MB; under 1 MB is safer with spam filters"]
    return []


def save_resume(conn: sqlite3.Connection, data: bytes, path: Path, original_name: str) -> list[str]:
    warnings = check_resume(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    db.set_settings(conn, resume_name=Path(original_name).name or "resume.pdf")
    with conn:
        db.log_event(conn, "resume_saved", name=original_name, size=len(data))
    return warnings


# --- test batches -------------------------------------------------------------------------

class TestBatch(BaseModel):
    batch_id: int
    template_id: int
    email_ids: list[int]
    to: str


def testable_emails(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Drafts and approved emails, newest template versions first. Any of them can be sent as a test."""
    return conn.execute(
        "SELECT e.id, e.status, e.template_id, e.subject, c.company, c.name, c.email, t.name AS template_name,"
        " t.version FROM emails e JOIN contacts c ON c.id = e.contact_id JOIN templates t ON t.id = e.template_id"
        " WHERE e.status IN ('draft', 'approved') ORDER BY t.version DESC, e.id").fetchall()


def build_test_payload(conn: sqlite3.Connection, email_ids: list[int], test_to: str, batch_id: int) -> dict:
    if not 1 <= len(email_ids) <= MAX_TEST_EMAILS:
        raise SendError(f"a test batch has 1 to {MAX_TEST_EMAILS} emails")
    marks = ",".join("?" * len(email_ids))
    rows = conn.execute(f"SELECT * FROM emails WHERE id IN ({marks}) AND status IN ('draft', 'approved')",
                        email_ids).fetchall()
    if len(rows) != len(set(email_ids)):
        raise SendError("only drafts and approved emails can be sent as a test")
    if len({r["template_id"] for r in rows}) != 1:
        raise SendError("a test batch checks one template version; pick emails from the same version")
    settings = db.get_settings(conn)
    return {
        "batch_id": batch_id,
        "test_mode": True,
        "sender_name": settings.get("my_name", ""),
        "spacing_min_s": TEST_SPACING_S[0],
        "spacing_max_s": TEST_SPACING_S[1],
        "emails": [{"email_id": f"test-{batch_id}-{r['id']}", "to": test_to, "subject": r["subject"],
                    "body_text": r["body_text"]} for r in sorted(rows, key=lambda r: email_ids.index(r["id"]))],
    }


def send_test(conn: sqlite3.Connection, client: N8nClient, email_ids: list[int], test_to: str,
              resume: bytes, resume_name: str) -> TestBatch:
    """Create a test batch, hand it to W2. The batch row records the outcome either way."""
    check_resume(resume)
    template_id = conn.execute("SELECT template_id FROM emails WHERE id = ?", (email_ids[0],)).fetchone()
    if template_id is None:
        raise SendError("that email doesn't exist")
    with conn:
        batch_id = conn.execute("INSERT INTO batches (template_id, test_mode, status, created_at) VALUES (?, 1, 'draft', ?)",
                                (template_id[0], db.now())).lastrowid
    try:
        payload = build_test_payload(conn, email_ids, test_to, batch_id)
    except SendError:
        with conn:
            conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
        raise
    try:
        client.send_batch(payload, resume, resume_name)
    except N8nError as exc:
        with conn:
            conn.execute("UPDATE batches SET status = 'failed' WHERE id = ?", (batch_id,))
            db.log_event(conn, "test_batch_failed", batch_id=batch_id, error=str(exc))
        raise
    with conn:
        conn.execute("UPDATE batches SET status = 'submitted', submitted_at = ? WHERE id = ?", (db.now(), batch_id))
        db.log_event(conn, "test_batch_submitted", batch_id=batch_id, to=test_to, email_ids=email_ids,
                     emails=json.dumps([{"subject": e["subject"]} for e in payload["emails"]]))
    return TestBatch(batch_id=batch_id, template_id=template_id[0], email_ids=email_ids, to=test_to)


def test_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT b.*, t.name AS template_name, t.version, t.test_checked_at FROM batches b"
        " JOIN templates t ON t.id = b.template_id WHERE b.test_mode = 1 ORDER BY b.id DESC").fetchall()


def mark_test_checked(conn: sqlite3.Connection, template_id: int) -> None:
    """I received a test of this template version and it looked right (rule 2)."""
    submitted = conn.execute("SELECT 1 FROM batches WHERE template_id = ? AND test_mode = 1 AND status = 'submitted'",
                             (template_id,)).fetchone()
    if not submitted:
        raise SendError("send a test of this template version first")
    with conn:
        conn.execute("UPDATE templates SET test_checked_at = ? WHERE id = ?", (db.now(), template_id))
        db.log_event(conn, "template_test_checked", template_id=template_id)
