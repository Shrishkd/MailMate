"""The one follow-up (Step 9): after N days without a reply, a draft for my approval.

A first email is due for a follow-up when it was really sent (Gmail confirmed it through the
reply sync), at least `follow_up_days` ago, got no reply or bounce, its contact isn't on the
do-not-contact list, and the contact has no follow-up yet. Follow-ups are only offered when the
replies were synced recently, so nobody who already answered gets nudged.

The follow-up is drafted from the "Follow-up" template and goes through the same review and
sending path as any email. It replies in the first email's Gmail thread ("Re: <subject>").
Never more than one per contact: drafting refuses a second, review blocks it, and the database's
one-email-per-kind index refuses it at send time.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from mailmate import db
from mailmate.review import ReviewError
from mailmate.templates import FOLLOW_UP_TEMPLATE, Template, latest_templates, merge, save_template, values_for

DEFAULT_DAYS = 6
SYNC_FRESH_H = 24
DEFAULT_SUBJECT = "Re: (the first email's subject)"
DEFAULT_BODY = """Hey {first_name},

Just bringing my note about the {role} role at {company} back to the top of your inbox, in case it got buried under everything else.

My resume is in the email below; I'd be glad to share anything else that helps.

{signature}"""


def follow_up_template(conn: sqlite3.Connection) -> Template:
    """The newest Follow-up template; version 1 is created with the default text if there is none."""
    for t in latest_templates(conn):
        if t.name == FOLLOW_UP_TEMPLATE:
            return t
    return save_template(conn, FOLLOW_UP_TEMPLATE, DEFAULT_SUBJECT, DEFAULT_BODY)


def follow_up_days(conn: sqlite3.Connection) -> int:
    return int(db.get_settings(conn).get("follow_up_days", DEFAULT_DAYS))


def set_follow_up_days(conn: sqlite3.Connection, days: int) -> None:
    if not 2 <= days <= 30:
        raise ReviewError("follow up after 2 to 30 days")
    db.set_settings(conn, follow_up_days=str(days))


def last_sync(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT created_at FROM events WHERE kind = 'sync_done' ORDER BY id DESC LIMIT 1").fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def sync_is_fresh(conn: sqlite3.Connection, now: datetime) -> bool:
    synced = last_sync(conn)
    return synced is not None and now - synced <= timedelta(hours=SYNC_FRESH_H)


def due(conn: sqlite3.Connection, now: datetime | None = None) -> list[sqlite3.Row]:
    """First emails that may get their one follow-up now (whether or not replies are synced)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=follow_up_days(conn))
    rows = conn.execute(
        "SELECT e.*, c.company, c.name, c.email AS address FROM emails e JOIN contacts c ON c.id = e.contact_id"
        " WHERE e.kind = 'initial' AND e.test_mode = 0 AND e.status = 'sent' AND e.sent_at IS NOT NULL"
        " AND c.status = 'contacted' AND c.email NOT IN (SELECT email FROM suppression)"
        " AND NOT EXISTS (SELECT 1 FROM emails f WHERE f.contact_id = e.contact_id AND f.kind = 'follow_up'"
        "                 AND f.status != 'rejected')"
        " ORDER BY e.sent_at").fetchall()
    return [r for r in rows if datetime.fromisoformat(r["sent_at"].replace("Z", "+00:00")) <= cutoff]


def reply_subject(subject: str) -> str:
    return "Re: " + re.sub(r"^(?:re:\s*)+", "", subject, flags=re.I)


def create_follow_up(conn: sqlite3.Connection, first_email_id: int, now: datetime | None = None) -> int:
    """Draft the follow-up to one first email. Returns the new email's id."""
    now = now or datetime.now(timezone.utc)
    if not sync_is_fresh(conn, now):
        raise ReviewError(f"sync replies first (Replies page): follow-ups need a sync from the last {SYNC_FRESH_H} h, "
                          "so nobody who already answered gets a nudge")
    first = next((r for r in due(conn, now) if r["id"] == first_email_id), None)
    if first is None:
        raise ReviewError(f"email #{first_email_id} isn't due for a follow-up (not sent {follow_up_days(conn)}+ days "
                          "ago without a reply, or the contact already has a follow-up or is blocked)")
    contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (first["contact_id"],)).fetchone()
    template = follow_up_template(conn)
    merged = merge(template, values_for(contact, db.get_settings(conn), ""))
    if not merged.ok:
        raise ReviewError("; ".join(merged.errors))
    with conn:
        cur = conn.execute(
            "INSERT INTO emails (contact_id, template_id, kind, subject, body_text, status, created_at)"
            " VALUES (?, ?, 'follow_up', ?, ?, 'draft', ?)",
            (contact["id"], template.id, reply_subject(first["subject"]), merged.body_text, db.now()))
        db.log_event(conn, "follow_up_drafted", contact_id=contact["id"], email_id=cur.lastrowid,
                     first_email_id=first_email_id, template=f"{template.name} v{template.version}")
    return cur.lastrowid
