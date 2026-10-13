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
    """Every company among my contacts (one row each) with its applied state and job links."""
    applied = {r["company_key"]: r for r in conn.execute("SELECT * FROM applied_companies")}
    rows: dict[str, dict] = {}
    for c in conn.execute("SELECT company, job_url FROM contacts ORDER BY id"):
        key = company_key(c["company"])
        row = rows.setdefault(key, {"key": key, "company": c["company"], "links": [], "applied": key in applied,
                                    "applied_at": applied[key]["applied_at"] if key in applied else None})
        if c["job_url"] and c["job_url"] not in row["links"]:
            row["links"].append(c["job_url"])
    return sorted(rows.values(), key=lambda r: (not r["applied"], r["company"].lower()))
