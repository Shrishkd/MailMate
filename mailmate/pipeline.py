"""After the reply: where each company stands (talking, screening, interviewing, offer, or closed),
what happens next and when, and my notes.

A company enters the pipeline by itself when one of its recruiters asks for an interview or asks a
question (after a sync); I can add one by hand too (a call that came another way). Every stage change
is logged, and `reached` keeps the furthest stage a company got to, so the Dashboard can count
interviews and offers even after a later rejection.
"""

import sqlite3
from datetime import datetime, timedelta

from mailmate import db
from mailmate.companies import company_key

STAGES = db.PIPELINE_STAGES
OPEN = ("talking", "screening", "interviewing", "offer")
RANK = {"talking": 1, "screening": 2, "interviewing": 3, "offer": 4}
LABELS = {"talking": "💬 Talking", "screening": "📞 Screening call", "interviewing": "🧑‍💻 Interviewing",
          "offer": "🎉 Offer", "rejected": "✖ Rejected", "withdrawn": "↩ Withdrawn"}
STARTS_FROM = ("interview_request", "question")
STALE_AFTER = timedelta(hours=3)     # an interview time this long past: ask what happened


class PipelineError(ValueError):
    pass


def add(conn: sqlite3.Connection, company: str, contact_id: int | None = None, note: str = "",
        source: str = "by hand") -> bool:
    """Put a company in the pipeline as 'talking'. False if it's already there."""
    key = company_key(company)
    if not key:
        raise PipelineError("the company name is empty")
    with conn:
        added = conn.execute(
            "INSERT OR IGNORE INTO pipeline (company_key, company, stage, reached, contact_id, notes, created_at,"
            " updated_at) VALUES (?, ?, 'talking', 1, ?, ?, ?, ?)",
            (key, company.strip(), contact_id, note, db.now(), db.now())).rowcount
        if added:
            db.log_event(conn, "pipeline_added", contact_id=contact_id, company=company.strip(), source=source)
    return bool(added)


def add_from_replies(conn: sqlite3.Connection, test_to: str = "") -> list[str]:
    """Companies whose recruiter asked for an interview or asked a question, not in the pipeline yet."""
    rows = conn.execute(
        "SELECT c.company, c.id AS contact_id, MIN(r.date) AS first_date, r.category FROM replies r"
        " JOIN emails e ON e.id = r.email_id JOIN contacts c ON c.id = e.contact_id"
        f" WHERE e.test_mode = 0 AND r.category IN ({', '.join('?' * len(STARTS_FROM))}) AND lower(c.email) != lower(?)"
        " GROUP BY c.id ORDER BY first_date", (*STARTS_FROM, test_to)).fetchall()
    added = []
    for r in rows:
        note = f"{r['category'].replace('_', ' ')} on {r['first_date'][:10]}"
        if add(conn, r["company"], r["contact_id"], note=note, source="reply"):
            added.append(r["company"])
    return added


def update(conn: sqlite3.Connection, key: str, *, stage: str, next_step: str = "", next_at: datetime | None = None,
           notes: str = "") -> None:
    if stage not in STAGES:
        raise PipelineError(f"unknown stage: {stage}")
    row = conn.execute("SELECT stage, reached FROM pipeline WHERE company_key = ?", (key,)).fetchone()
    if row is None:
        raise PipelineError("that company isn't in the pipeline")
    reached = max(row["reached"], RANK.get(stage, 0))
    with conn:
        conn.execute("UPDATE pipeline SET stage = ?, reached = ?, next_step = ?, next_at = ?, notes = ?, updated_at = ?"
                     " WHERE company_key = ?",
                     (stage, reached, next_step.strip(), next_at.isoformat() if next_at and stage in OPEN else None,
                      notes.strip(), db.now(), key))
        if stage != row["stage"]:
            db.log_event(conn, "pipeline_stage", company_key=key, stage_from=row["stage"], stage_to=stage)


def entries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Everything in the pipeline: open stages first (furthest along first), then closed ones."""
    return conn.execute(
        "SELECT p.*, c.name, c.email, c.role, c.job_url FROM pipeline p LEFT JOIN contacts c ON c.id = p.contact_id"
        " ORDER BY p.stage NOT IN ('talking', 'screening', 'interviewing', 'offer'), p.reached DESC,"
        " p.updated_at DESC").fetchall()


def _at(row) -> datetime | None:
    return datetime.fromisoformat(row["next_at"]) if row["next_at"] else None


def upcoming(conn: sqlite3.Connection, now: datetime, within: timedelta = timedelta(days=7)) -> list[sqlite3.Row]:
    """Open companies with a next step in the coming days (or just now), soonest first."""
    rows = [r for r in entries(conn) if r["stage"] in OPEN and _at(r) and now - STALE_AFTER <= _at(r) <= now + within]
    return sorted(rows, key=_at)


def stale(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    """Open companies whose next step is well past: time to write down what happened."""
    return [r for r in entries(conn) if r["stage"] in OPEN and _at(r) and _at(r) < now - STALE_AFTER]


def reached_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many companies got at least as far as each stage (a later rejection doesn't undo it)."""
    rows = conn.execute("SELECT reached FROM pipeline").fetchall()
    return {stage: sum(r[0] >= rank for r in rows) for stage, rank in RANK.items()}
