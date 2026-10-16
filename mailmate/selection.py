"""Who goes next: one recruiter per company at a time; companies I applied to by direct link first,
then High priority.

Recruiters at one company talk to each other. Several near-identical emails on one morning read as
mass mailing and use up every chance at that company at once. So a company is:

- waiting  while one of its recruiters has a chance to answer: a first email queued; sent without an
           answer less than `follow_up_days` + 4 days ago; a follow-up drafted, approved or queued;
           or a follow-up sent less than 4 days ago. The follow-up to the first recruiter goes first,
           so two people at one company never get an email on the same day;
- closed   once one of its recruiters really answered (interview request, question, not interested,
           other): you're in a conversation, or they said no. Auto-replies don't count;
- free     otherwise. A bounce frees it at once: that address was dead, try the next recruiter.

My own test address never counts. Suggestions pre-tick a sensible set; I can still change it, but a
real batch that breaks the company rule is refused (see sending.plan_real_batch).
"""

import sqlite3
from datetime import datetime, timedelta

from pydantic import BaseModel

from mailmate import followups
from mailmate.companies import applied_keys, company_key  # noqa: F401  (company_key is used by sending)

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2, "": 3}
_IST_OFFSET = timedelta(hours=5, minutes=30)


def priority_rank(priority: str | None) -> int:
    return PRIORITY_ORDER.get(priority or "", 3)


def order_key(applied: set[str]):
    """Applied-by-link companies first, then High, Medium, Low, none; then file order."""
    return lambda c: (company_key(c["company"]) not in applied, priority_rank(c["priority"]), c["contact_id"]
                      if "contact_id" in c.keys() else c["id"])


GAP_AFTER_FOLLOW_UP_DAYS = 4     # the next recruiter at a company waits this long after a follow-up


class CompanyBlock(BaseModel):
    company: str
    who: str
    detail: str
    closed: bool = False               # someone there really answered
    until: datetime | None = None      # waiting until this moment; None while waiting = until something happens

    def describe(self) -> str:
        if self.closed:
            return f"closed: {self.who} already answered ({self.detail})"
        if self.until is None:
            return f"waiting on {self.who} ({self.detail})"
        return f"waiting on {self.who} ({self.detail}) until {_day(self.until)}"

    def outlasts(self, other: "CompanyBlock | None") -> bool:
        """Whether this block keeps the company waiting longer than `other`."""
        if other is None or self.closed:
            return True
        if other.closed or other.until is None:
            return False
        return self.until is None or self.until > other.until


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _day(moment: datetime) -> str:
    return f"{moment + _IST_OFFSET:%d %b}"


def company_blocks(conn: sqlite3.Connection, now: datetime, test_to: str) -> dict[str, CompanyBlock]:
    """Company key -> why no new first email may go there now."""
    days = timedelta(days=followups.follow_up_days(conn))
    gap = timedelta(days=GAP_AFTER_FOLLOW_UP_DAYS)
    rows = conn.execute(
        "SELECT e.id, e.status, e.queued_at, e.sent_at, c.company, c.name, c.email, f.status AS follow_up,"
        " f.queued_at AS follow_up_queued_at, f.sent_at AS follow_up_sent_at FROM emails e"
        " JOIN contacts c ON c.id = e.contact_id"
        " LEFT JOIN emails f ON f.contact_id = e.contact_id AND f.kind = 'follow_up' AND f.status != 'rejected'"
        " WHERE e.kind = 'initial' AND e.test_mode = 0 AND e.status IN ('queued', 'sent', 'replied')"
        " AND lower(c.email) != lower(?)", (test_to,)).fetchall()
    blocks: dict[str, CompanyBlock] = {}
    for r in rows:
        block = CompanyBlock(company=r["company"], who=r["name"] or r["email"], detail="")
        if r["status"] == "replied":
            reply = conn.execute("SELECT category FROM replies WHERE email_id = ? AND category NOT IN"
                                 " ('auto_reply', 'bounce') ORDER BY date LIMIT 1", (r["id"],)).fetchone()
            block.closed, block.detail = True, (reply[0] if reply else "a reply").replace("_", " ")
        elif r["status"] == "queued":
            block.detail = "queued"
        elif r["follow_up"] in ("draft", "approved"):
            block.detail = f"their follow-up is {'drafted' if r['follow_up'] == 'draft' else 'approved'}, not sent yet"
        elif r["follow_up"] == "queued" and not r["follow_up_sent_at"]:
            block.detail = "their follow-up is queued"
        elif r["follow_up"]:
            followed = _ts(r["follow_up_sent_at"] or r["follow_up_queued_at"])
            block.detail, block.until = f"followed up {_day(followed)}, no reply yet", followed + gap
        else:
            sent = _ts(r["sent_at"] or r["queued_at"])
            block.detail = f"sent {_day(sent)}, no reply yet; their follow-up goes first"
            block.until = sent + days + gap
        if (block.closed or block.until is None or block.until > now) and block.outlasts(blocks.get(company_key(r["company"]))):
            blocks[company_key(r["company"])] = block
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
    for c in sorted(candidates, key=order_key(applied_keys(conn))):
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
    ordered = sorted(candidates, key=order_key(applied_keys(conn)))
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
