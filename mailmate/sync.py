"""Applying W3's answer: which emails went out, who replied, who bounced (rule 5's automatic part).

Gmail is the record of what was sent. A thread is matched to a real email the first time by
address, subject and time (sent after the email was queued); its Gmail thread id is stored, so
later syncs match by id. Then:

- bounce (recognised by plain rules in W3)   -> email + contact 'bounced', do-not-contact
- not_interested, confidence >= 0.6          -> 'replied', do-not-contact
- not_interested, less sure                  -> 'replied', flagged for me to look at
- interview_request / question / other       -> 'replied'
- auto_reply (out of office)                 -> stored, nothing changes

Replies to test sends are sorted and shown but change nothing. My own test address never goes on
the do-not-contact list automatically. Running sync twice changes nothing the second time.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from mailmate import db
from mailmate.n8n import SyncResult, SyncThread

SUPPRESS_CONFIDENCE = 0.6
MATCH_SLACK_S = 120                  # Gmail's timestamp vs the moment MailMate queued the email
NOT_FOUND_AFTER_S = 3600             # a queued email still not in Gmail this long after its batch ended
_RANK = {"queued": 0, "sent": 1, "replied": 2, "bounced": 3}   # statuses only move forward


class ReplyNote(BaseModel):
    company: str = ""
    to: str
    from_addr: str
    date: str
    category: str
    confidence: float | None = None
    snippet: str = ""
    test: bool = False
    flagged: bool = False


class SyncReport(BaseModel):
    threads: int = 0
    newly_sent: list[str] = []          # addresses whose email Gmail now shows as sent
    new_replies: list[ReplyNote] = []
    test_replies: list[ReplyNote] = []  # replies to test sends: sorted, no effect
    suppressed: list[str] = []
    unknown_threads: list[str] = []     # labelled threads MailMate didn't queue (e.g. labelled by hand)
    not_found: list[str] = []           # queued long ago, still no thread in Gmail
    batches_done: list[int] = []


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _match(conn: sqlite3.Connection, threads: list[SyncThread]) -> dict[str, sqlite3.Row]:
    """thread id -> the real email it belongs to."""
    rows = conn.execute(
        "SELECT e.*, c.email AS address, c.company FROM emails e JOIN contacts c ON c.id = e.contact_id"
        " WHERE e.test_mode = 0 AND e.status IN ('queued', 'sent', 'replied', 'bounced')").fetchall()
    by_thread = {r["gmail_thread_id"]: r for r in rows if r["gmail_thread_id"]}
    waiting = [r for r in rows if not r["gmail_thread_id"]]
    matched = {}
    for t in sorted(threads, key=lambda t: t.sent_at):
        if t.thread_id in by_thread:
            matched[t.thread_id] = by_thread[t.thread_id]
            continue
        for r in waiting:
            if (r["address"].lower() == t.to.lower() and r["subject"] == t.subject
                    and _parse(t.sent_at) >= _parse(r["queued_at"]) - timedelta(seconds=MATCH_SLACK_S)):
                matched[t.thread_id] = r
                waiting.remove(r)
                break
    return matched


def _advance(conn: sqlite3.Connection, email_id: int, contact_id: int, status: str, current: str) -> None:
    if _RANK[status] > _RANK.get(current, 0):
        conn.execute("UPDATE emails SET status = ? WHERE id = ?", (status, email_id))
    contact_status = {"sent": "contacted", "replied": "replied", "bounced": "bounced"}[status]
    conn.execute("UPDATE contacts SET status = ? WHERE id = ? AND status NOT IN ('bounced')"
                 + (" AND status NOT IN ('replied')" if contact_status == "contacted" else ""),
                 (contact_status, contact_id))


def apply_sync(conn: sqlite3.Connection, result: SyncResult, test_to: str, now: datetime | None = None,
               spacing_max_s: int = 420) -> SyncReport:
    now = now or datetime.now(timezone.utc)
    report = SyncReport(threads=len(result.threads))
    matched = _match(conn, result.threads)
    to_suppress: list[tuple[str, str, str]] = []

    with conn:
        for t in result.threads:
            email = matched.get(t.thread_id)
            if email is None:
                notes = [ReplyNote(to=t.to, from_addr=r.from_, date=r.date, category=r.category,
                                   confidence=r.confidence, snippet=r.snippet, test=True) for r in t.replies]
                if t.to.lower() == test_to.lower():
                    report.test_replies += notes
                else:
                    report.unknown_threads.append(f"{t.to}: {t.subject}")
                continue

            status = email["status"]
            if not email["gmail_thread_id"]:
                conn.execute("UPDATE emails SET gmail_thread_id = ?, gmail_message_id = ?, sent_at = ? WHERE id = ?",
                             (t.thread_id, t.sent_message_id, t.sent_at, email["id"]))
                if status == "queued":
                    _advance(conn, email["id"], email["contact_id"], "sent", status)
                    status = "sent"
                    report.newly_sent.append(email["address"])
                db.log_event(conn, "email_sent_confirmed", contact_id=email["contact_id"], email_id=email["id"],
                             thread_id=t.thread_id)

            for r in t.replies:
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO replies (email_id, gmail_thread_id, from_addr, date, snippet, body_text,"
                    " category, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (email["id"], t.thread_id, r.from_, r.date, r.snippet, r.body_text, r.category, r.confidence,
                     db.now())).rowcount
                if not inserted:
                    continue                                   # seen in an earlier sync
                note = ReplyNote(company=email["company"], to=email["address"], from_addr=r.from_, date=r.date,
                                 category=r.category, confidence=r.confidence, snippet=r.snippet)
                if r.category == "bounce":
                    _advance(conn, email["id"], email["contact_id"], "bounced", status)
                    status = "bounced"
                    to_suppress.append((email["address"], "bounce", f"bounced ({t.subject})"))
                elif r.category != "auto_reply":
                    _advance(conn, email["id"], email["contact_id"], "replied", status)
                    status = max(status, "replied", key=lambda s: _RANK.get(s, 0))
                    if r.category == "not_interested":
                        if (r.confidence or 0) >= SUPPRESS_CONFIDENCE:
                            to_suppress.append((email["address"], "not_interested", f"replied: {r.snippet[:80]}"))
                        else:
                            note.flagged = True
                report.new_replies.append(note)
                db.log_event(conn, "reply_received", contact_id=email["contact_id"], email_id=email["id"],
                             category=r.category, confidence=r.confidence, flagged=note.flagged)

        # Batches whose emails all left the 'queued' state are done; long-queued emails are flagged.
        for b in conn.execute("SELECT id, status, submitted_at FROM batches WHERE test_mode = 0"
                              " AND status IN ('submitted', 'stopped')").fetchall():
            emails = conn.execute("SELECT e.status, c.email FROM emails e JOIN contacts c ON c.id = e.contact_id"
                                  " WHERE e.batch_id = ?", (b["id"],)).fetchall()
            queued = [e["email"] for e in emails if e["status"] == "queued"]
            if not queued and b["status"] == "submitted":
                conn.execute("UPDATE batches SET status = 'done' WHERE id = ?", (b["id"],))
                report.batches_done.append(b["id"])
            elif queued and b["submitted_at"]:
                deadline = (_parse(b["submitted_at"])
                            + timedelta(seconds=(len(emails) - 1) * spacing_max_s + NOT_FOUND_AFTER_S))
                if now > deadline:
                    report.not_found += queued

    for address, reason, note in to_suppress:
        if address.lower() == test_to.lower():
            with conn:
                db.log_event(conn, "suppression_skipped", email=address, reason=reason,
                             why="my own test address is never put on the do-not-contact list automatically")
            continue
        if address.lower() not in db.suppressed(conn):
            db.suppress(conn, address, reason, note)
            report.suppressed.append(address)
    return report
