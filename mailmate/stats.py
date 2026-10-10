"""Dashboard numbers, computed from the database only (Step 10).

Counts are per contact, real sends only: my own test address and test sends never count.
"Sent" means Gmail confirmed it (the reply sync matched its thread); emails still in W2's queue
are shown separately. A reply is any answer except an auto-reply or a bounce; "positive" is an
interview request. Rates are out of contacts sent to.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel

from mailmate.sending import IST

SENT = ("sent", "replied", "bounced")
NOT_A_REPLY = ("auto_reply", "bounce")


class Funnel(BaseModel):
    uploaded: int = 0
    approved: int = 0
    queued: int = 0          # handed to W2, not yet confirmed by a sync
    sent: int = 0
    replied: int = 0
    positive: int = 0
    bounced: int = 0

    def rate(self, part: int) -> float | None:
        return part / self.sent if self.sent else None


class TemplateRow(BaseModel):
    template: str
    sent: int
    replied: int
    positive: int
    bounced: int
    replied_after_follow_up: int

    @property
    def reply_rate(self) -> float | None:
        return self.replied / self.sent if self.sent else None


def _real_contacts(test_to: str) -> tuple[str, tuple]:
    return "lower(c.email) != lower(?)", (test_to,)


def funnel(conn: sqlite3.Connection, test_to: str) -> Funnel:
    where, args = _real_contacts(test_to)

    def count(sql: str) -> int:
        return conn.execute(sql.format(where=where), args).fetchone()[0]

    first = "SELECT COUNT(DISTINCT c.id) FROM contacts c JOIN emails e ON e.contact_id = c.id AND e.kind = 'initial'"
    reply = ("SELECT COUNT(DISTINCT c.id) FROM contacts c JOIN emails e ON e.contact_id = c.id"
             " JOIN replies r ON r.email_id = e.id WHERE e.test_mode = 0 AND {where}")
    return Funnel(
        uploaded=count("SELECT COUNT(*) FROM contacts c WHERE {where}"),
        approved=count(first + " WHERE e.status IN ('approved', 'queued', 'sent', 'replied', 'bounced') AND {where}"),
        queued=count(first + " WHERE e.status = 'queued' AND e.test_mode = 0 AND {where}"),
        sent=count(first + f" WHERE e.status IN {SENT} AND e.test_mode = 0 AND {{where}}"),
        replied=count(reply + f" AND r.category NOT IN {NOT_A_REPLY}"),
        positive=count(reply + " AND r.category = 'interview_request'"),
        bounced=count(first + " WHERE e.status = 'bounced' AND e.test_mode = 0 AND {where}"),
    )


def reply_categories(conn: sqlite3.Connection, test_to: str) -> dict[str, int]:
    where, args = _real_contacts(test_to)
    rows = conn.execute("SELECT r.category, COUNT(*) FROM replies r JOIN emails e ON e.id = r.email_id"
                        f" JOIN contacts c ON c.id = e.contact_id WHERE e.test_mode = 0 AND {where}"
                        " GROUP BY r.category ORDER BY COUNT(*) DESC, r.category", args).fetchall()
    return {r[0]: r[1] for r in rows}


def per_template(conn: sqlite3.Connection, test_to: str) -> list[TemplateRow]:
    """Each first-email template version: how its sent emails did. A reply counts for the first
    email's version; 'after follow-up' = the contact's first real reply came after the follow-up."""
    where, args = _real_contacts(test_to)
    rows = conn.execute(
        f"SELECT t.name, t.version, e.id, e.status, e.contact_id FROM emails e JOIN templates t ON t.id = e.template_id"
        f" JOIN contacts c ON c.id = e.contact_id WHERE e.kind = 'initial' AND e.test_mode = 0"
        f" AND e.status IN {SENT} AND {where} ORDER BY t.name, t.version", args).fetchall()
    table: dict[str, TemplateRow] = {}
    for r in rows:
        label = f"{r['name']} v{r['version']}"
        row = table.setdefault(label, TemplateRow(template=label, sent=0, replied=0, positive=0, bounced=0,
                                                  replied_after_follow_up=0))
        row.sent += 1
        row.bounced += r["status"] == "bounced"
        replies = conn.execute(f"SELECT category, date FROM replies WHERE email_id = ? AND category NOT IN {NOT_A_REPLY}"
                               " ORDER BY date", (r["id"],)).fetchall()
        if replies:
            row.replied += 1
            row.positive += any(x["category"] == "interview_request" for x in replies)
            follow_up = conn.execute("SELECT sent_at FROM emails WHERE contact_id = ? AND kind = 'follow_up'"
                                     " AND test_mode = 0 AND sent_at IS NOT NULL", (r["contact_id"],)).fetchone()
            if follow_up and _ts(replies[0]["date"]) > _ts(follow_up["sent_at"]):
                row.replied_after_follow_up += 1
    return list(table.values())


def sends_per_day(conn: sqlite3.Connection, test_to: str, days: int = 14,
                  today: date | None = None) -> list[tuple[date, int]]:
    """Real emails Gmail confirmed as sent (first emails and follow-ups), per India date, zeros included."""
    today = today or datetime.now(timezone.utc).astimezone(IST).date()
    where, args = _real_contacts(test_to)
    counts: dict[date, int] = {}
    for (sent_at,) in conn.execute(f"SELECT e.sent_at FROM emails e JOIN contacts c ON c.id = e.contact_id"
                                   f" WHERE e.test_mode = 0 AND e.sent_at IS NOT NULL AND {where}", args):
        day = _ts(sent_at).astimezone(IST).date()
        counts[day] = counts.get(day, 0) + 1
    return [(today - timedelta(days=i), counts.get(today - timedelta(days=i), 0)) for i in range(days - 1, -1, -1)]


def recent_replies(conn: sqlite3.Connection, test_to: str, limit: int = 10) -> list[sqlite3.Row]:
    where, args = _real_contacts(test_to)
    return conn.execute(
        "SELECT c.company, c.name, c.email, r.date, r.category, r.confidence, r.snippet FROM replies r"
        f" JOIN emails e ON e.id = r.email_id JOIN contacts c ON c.id = e.contact_id"
        f" WHERE e.test_mode = 0 AND {where} ORDER BY r.date DESC LIMIT ?", (*args, limit)).fetchall()


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
