"""Companies: one name per company, and the checklist of companies I applied to by direct link.

Applying through the job's own link first and then writing to the recruiter is the strongest
order: the recruiter can find my application. So companies on this checklist go first in every
suggestion (selection.py), ahead of High priority. The checklist is filled from the job list
("Applied" column) and by hand on the Contacts page.
"""

import re
import sqlite3

from mailmate import db

_LEGAL_SUFFIXES = {"inc", "ltd", "llc", "pvt", "private", "limited", "corp", "corporation", "co", "plc", "gmbh"}


def company_key(name: str) -> str:
    """'Lexsi.ai' and 'Lexsi AI' are one company; 'Acme Pvt. Ltd.' and 'Acme' too."""
    words = re.findall(r"[a-z0-9]+", name.lower())
    return " ".join([w for w in words if w not in _LEGAL_SUFFIXES] or words)


def applied_keys(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT company_key FROM applied_companies")}


def set_applied(conn: sqlite3.Connection, company: str, applied: bool, source: str = "checklist") -> None:
    key = company_key(company)
    with conn:
        if applied:
            if conn.execute("INSERT OR IGNORE INTO applied_companies (company_key, company, source, applied_at)"
                            " VALUES (?, ?, ?, ?)", (key, company, source, db.now())).rowcount:
                db.log_event(conn, "company_applied", company=company, source=source)
        elif conn.execute("DELETE FROM applied_companies WHERE company_key = ?", (key,)).rowcount:
            db.log_event(conn, "company_applied_removed", company=company, source=source)


def checklist(conn: sqlite3.Connection) -> list[dict]:
    """Every company among my contacts (one row each): applied state, job links, and when its first
    email was drafted. Always in the same (alphabetical) order, so ticking never moves rows."""
    applied = {r["company_key"]: r for r in conn.execute("SELECT * FROM applied_companies")}
    rows: dict[str, dict] = {}
    for c in conn.execute(
            "SELECT c.company, c.job_url, (SELECT MIN(e.created_at) FROM emails e WHERE e.contact_id = c.id"
            " AND e.kind = 'initial' AND e.status != 'rejected') AS drafted_at FROM contacts c ORDER BY c.id"):
        key = company_key(c["company"])
        row = rows.setdefault(key, {"key": key, "company": c["company"], "links": [], "applied": key in applied,
                                    "applied_at": applied[key]["applied_at"] if key in applied else None,
                                    "drafted_at": None})
        if c["job_url"] and c["job_url"] not in row["links"]:
            row["links"].append(c["job_url"])
        if c["drafted_at"] and (row["drafted_at"] is None or c["drafted_at"] < row["drafted_at"]):
            row["drafted_at"] = c["drafted_at"]
    return sorted(rows.values(), key=lambda r: r["company"].lower())


def save_checklist(conn: sqlite3.Connection, ticked: dict[str, bool]) -> list[str]:
    """{company: ticked} as shown on screen -> store only real changes. Matched by company name, never
    by row position. Returns the companies that changed."""
    current = applied_keys(conn)
    changed = []
    for company, value in ticked.items():
        if bool(value) != (company_key(company) in current):
            set_applied(conn, company, bool(value))
            changed.append(company)
    return changed
