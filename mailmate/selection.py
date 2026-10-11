"""Who goes next: one recruiter per company at a time, High priority first.

Recruiters at one company talk to each other. Several near-identical emails on one morning read as
mass mailing and use up every chance at that company at once. So a company is:

- waiting  while one of its recruiters has a first email queued, or sent less than
           `follow_up_days` ago without an answer: nobody else there is emailed until then;
- closed   once one of its recruiters really answered (interview request, question, not interested,
           other): you're in a conversation, or they said no. Auto-replies don't count;
- free     otherwise. A bounce frees it at once: that address was dead, try the next recruiter.

My own test address never counts. Suggestions pre-tick a sensible set; I can still change it, but a
real batch that breaks the company rule is refused (see sending.plan_real_batch).
"""

import re
import sqlite3
from datetime import datetime, timedelta

from pydantic import BaseModel

from mailmate import followups

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2, "": 3}
_LEGAL_SUFFIXES = {"inc", "ltd", "llc", "pvt", "private", "limited", "corp", "corporation", "co", "plc", "gmbh"}
_IST_OFFSET = timedelta(hours=5, minutes=30)


def company_key(name: str) -> str:
    """'Lexsi.ai' and 'Lexsi AI' are one company; 'Acme Pvt. Ltd.' and 'Acme' too."""
    words = re.findall(r"[a-z0-9]+", name.lower())
    return " ".join([w for w in words if w not in _LEGAL_SUFFIXES] or words)


def priority_rank(priority: str | None) -> int:
    return PRIORITY_ORDER.get(priority or "", 3)


class CompanyBlock(BaseModel):
    company: str
    who: str
    detail: str
    until: datetime | None = None      # waiting until this moment; None = closed

    def describe(self) -> str:
        if self.until is None:
            return f"closed: {self.who} already answered ({self.detail})"
        return f"waiting on {self.who} ({self.detail}) until {(self.until + _IST_OFFSET):%d %b}"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def company_blocks(conn: sqlite3.Connection, now: datetime, test_to: str) -> dict[str, CompanyBlock]:
    """Company key -> why no new first email may go there now."""
    days = followups.follow_up_days(conn)
    rows = conn.execute(
        "SELECT e.id, e.status, e.queued_at, e.sent_at, c.company, c.name, c.email FROM emails e"
        " JOIN contacts c ON c.id = e.contact_id WHERE e.kind = 'initial' AND e.test_mode = 0"
        " AND e.status IN ('queued', 'sent', 'replied') AND lower(c.email) != lower(?)", (test_to,)).fetchall()
    blocks: dict[str, CompanyBlock] = {}
    for r in rows:
        key, who = company_key(r["company"]), r["name"] or r["email"]
        if r["status"] == "replied":
            reply = conn.execute("SELECT category FROM replies WHERE email_id = ? AND category NOT IN"
                                 " ('auto_reply', 'bounce') ORDER BY date LIMIT 1", (r["id"],)).fetchone()
            blocks[key] = CompanyBlock(company=r["company"], who=who,
                                       detail=(reply[0] if reply else "a reply").replace("_", " "))
            continue
        if key in blocks and blocks[key].until is None:
            continue                                         # already closed
        since = r["sent_at"] or r["queued_at"]
        until = _ts(since) + timedelta(days=days)
        if r["status"] == "queued" or until > now:
            detail = "queued" if r["status"] == "queued" else f"sent {(_ts(since) + _IST_OFFSET):%d %b}, no reply yet"
            current = blocks.get(key)
            if current is None or until > current.until:
                blocks[key] = CompanyBlock(company=r["company"], who=who, detail=detail, until=until)
    return blocks


def _pending_companies(conn: sqlite3.Connection, test_to: str) -> set[str]:
    """Companies with a first email already drafted or approved for one of their recruiters."""
    rows = conn.execute("SELECT c.company FROM emails e JOIN contacts c ON c.id = e.contact_id"
                        " WHERE e.kind = 'initial' AND e.status IN ('draft', 'approved')"
                        " AND lower(c.email) != lower(?)", (test_to,)).fetchall()
    return {company_key(r[0]) for r in rows}


def suggest_drafts(conn: sqlite3.Connection, candidates: list, now: datetime, test_to: str, limit: int) -> list[int]:
    """Contact ids to draft next: one per company, High priority first, then file order; companies
    that are waiting, closed or already have a draft in the queue are left out."""
    blocked = set(company_blocks(conn, now, test_to)) | _pending_companies(conn, test_to)
    chosen, seen = [], set()
    for c in sorted(candidates, key=lambda c: (priority_rank(c["priority"]), c["id"])):
        key = company_key(c["company"])
        if key in blocked or key in seen or c["email"].lower() == test_to.lower():
            continue
        chosen.append(c["id"])
        seen.add(key)
        if len(chosen) >= limit:
            break
    return chosen


def suggest_send(conn: sqlite3.Connection, candidates: list, now: datetime, test_to: str, limit: int) -> list[int]:
    """Approved email ids for the next real batch. Follow-ups first (they're time-bound and go to
    the same person); otherwise first emails, one per free company, High priority first. One
    template version per batch: the version of the first pick."""
    if limit <= 0 or not candidates:
        return []
    follow_ups = [c for c in candidates if c["kind"] == "follow_up"]
    if follow_ups:
        template_id = follow_ups[0]["template_id"]
        return [c["id"] for c in follow_ups if c["template_id"] == template_id][:limit]
    blocked = company_blocks(conn, now, test_to)
    ordered = sorted(candidates, key=lambda c: (priority_rank(c["priority"]), c["id"]))
    chosen, seen, template_id = [], set(), None
    for c in ordered:
        key = company_key(c["company"])
        if key in blocked or key in seen or c["email"].lower() == test_to.lower():
            continue
        if template_id is None:
            template_id = c["template_id"]
        if c["template_id"] != template_id:
            continue
        chosen.append(c["id"])
        seen.add(key)
        if len(chosen) >= limit:
            break
    return chosen
