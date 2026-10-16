"""The reply desk: recruiters' emails I still owe an answer, most urgent first.

An interview request should be answered within 12 hours, a question within a day. W4 can suggest an
answer and save it as a Gmail draft in the same conversation; I read it in Gmail and send it myself.
MailMate never sends an answer. Answering the latest email in a conversation answers the ones before it.
"""

import re
import sqlite3
from datetime import datetime, timedelta

from mailmate import db
from mailmate.templates import first_name

NEEDS_ANSWER = ("interview_request", "question", "other")
ANSWER_WITHIN = {"interview_request": timedelta(hours=12), "question": timedelta(hours=24),
                 "other": timedelta(hours=48)}
HANDLED = ("answered", "no_answer_needed")
MAX_ANSWER = 6000
_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,80}\]")


def _when(row) -> datetime:
    return datetime.fromisoformat(row["date"].replace("Z", "+00:00"))


def open_replies(conn: sqlite3.Connection, test_to: str = "") -> list[sqlite3.Row]:
    """The latest unanswered email in each conversation that needs an answer: interview requests first,
    then the oldest. Replies to my test address don't count."""
    rows = conn.execute(
        "SELECT r.*, e.subject AS my_subject, e.body_text AS my_body, e.contact_id, c.company, c.name, c.role,"
        " c.job_url, c.email AS recruiter FROM replies r JOIN emails e ON e.id = r.email_id"
        " JOIN contacts c ON c.id = e.contact_id"
        f" WHERE e.test_mode = 0 AND r.handled = '' AND r.category IN ({', '.join('?' * len(NEEDS_ANSWER))})"
        " AND lower(c.email) != lower(?) ORDER BY r.date", (*NEEDS_ANSWER, test_to)).fetchall()
    latest = {r["gmail_thread_id"]: r for r in rows}         # later rows replace earlier ones
    return sorted(latest.values(), key=lambda r: (r["category"] != "interview_request", r["date"]))


def overdue(row, now: datetime) -> bool:
    return now - _when(row) > ANSWER_WITHIN.get(row["category"], timedelta(hours=48))


def age(row, now: datetime) -> str:
    hours = int((now - _when(row)).total_seconds() // 3600)
    return f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"


def mark(conn: sqlite3.Connection, reply_id: int, how: str) -> None:
    """Answered (or needs no answer): this email and the earlier unanswered ones in its conversation."""
    if how not in HANDLED:
        raise ValueError(f"unknown way to handle a reply: {how}")
    row = conn.execute("SELECT gmail_thread_id, date FROM replies WHERE id = ?", (reply_id,)).fetchone()
    if row is None:
        raise ValueError(f"reply {reply_id} doesn't exist")
    with conn:
        conn.execute("UPDATE replies SET handled = ?, handled_at = ? WHERE gmail_thread_id = ? AND date <= ?"
                     " AND handled = ''", (how, db.now(), row["gmail_thread_id"], row["date"]))
        db.log_event(conn, "reply_handled", reply_id=reply_id, how=how)


def reopen(conn: sqlite3.Connection, reply_id: int) -> None:
    with conn:
        conn.execute("UPDATE replies SET handled = '', handled_at = NULL WHERE id = ?", (reply_id,))
        db.log_event(conn, "reply_reopened", reply_id=reply_id)


def write_request(row, settings: dict[str, str], notes: str = "") -> dict:
    """What W4 needs to suggest an answer."""
    return {"their_message": row["body_text"] or row["snippet"], "their_name": first_name(row["name"] or ""),
            "company": row["company"], "role": row["role"], "my_name": settings.get("my_name", ""),
            "my_email": row["my_body"], "notes": notes.strip()}


def check_answer(text: str) -> list[str]:
    """Why this answer can't be saved as a draft yet ([] = it can)."""
    problems = []
    if not text.strip():
        problems.append("the answer is empty")
    if found := _PLACEHOLDER.findall(text):
        problems.append("fill in or remove " + ", ".join(dict.fromkeys(found)))
    if "{" in text or "}" in text:
        problems.append("a '{' or '}' is left in the text")
    if len(text) > MAX_ANSWER:
        problems.append(f"the answer is longer than {MAX_ANSWER} characters")
    return problems


def record_draft(conn: sqlite3.Connection, reply_id: int, text: str, draft_id: str) -> None:
    with conn:
        conn.execute("UPDATE replies SET answer_text = ?, gmail_draft_id = ? WHERE id = ?", (text, draft_id, reply_id))
        db.log_event(conn, "reply_draft_saved", reply_id=reply_id, draft_id=draft_id)
