"""Handing emails to W2: test batches (Step 6) and real batches (Step 7).

A test batch sends the exact text of 1-3 reviewed emails (same template version) to the test
address, with the resume attached. It doesn't use up anyone's one email and doesn't change the
emails it copies. After I've checked what arrived (PDF, label, no n8n footer), I mark that
template version as test-checked; real sending requires that mark.

A real batch goes to the recruiters themselves, only within hard limits checked right before
sending: test-checked template (rule 2), daily cap, sending window on weekdays (the whole batch
must finish inside it), one batch at a time, every email still approved and not blocked, and a
typed confirmation of the number of emails (rule 1). Its emails become 'queued'; W3 (Step 8)
later learns from Gmail which were sent. A failure that may have sent something keeps them
'queued' until I've checked n8n, so nothing is ever sent twice.
"""

import json
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from mailmate import db, followups, review
from mailmate.n8n import N8nClient, N8nError
from mailmate.templates import merge, to_html, values_for

MAX_TEST_EMAILS = 3
REPEAT_COOLDOWN_S = 300         # the same test again this soon is a double click, not a decision
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
    thread_to_reply = None
    if any(r["kind"] == "follow_up" for r in rows):
        thread_to_reply = self_test_message(conn, test_to)
        if thread_to_reply is None:
            raise SendError("a follow-up test replies in a thread to your test address: send one real self-test "
                            "email first (step 4) and sync replies")
    return {
        "batch_id": batch_id,
        "test_mode": True,
        "sender_name": settings.get("my_name", ""),
        "spacing_min_s": TEST_SPACING_S[0],
        "spacing_max_s": TEST_SPACING_S[1],
        "emails": [{"email_id": f"test-{batch_id}-{r['id']}", "to": test_to, "subject": r["subject"],
                    "body_text": r["body_text"], "body_html": to_html(r["body_text"]),
                    **({"reply_to_message_id": thread_to_reply} if r["kind"] == "follow_up" else {})}
                   for r in sorted(rows, key=lambda r: email_ids.index(r["id"]))],
    }


def self_test_message(conn: sqlite3.Connection, test_to: str) -> str | None:
    """Gmail id of my latest real, synced first email to my own test address."""
    row = conn.execute("SELECT e.gmail_message_id FROM emails e JOIN contacts c ON c.id = e.contact_id"
                       " WHERE e.kind = 'initial' AND e.test_mode = 0 AND e.gmail_message_id IS NOT NULL"
                       " AND lower(c.email) = lower(?) ORDER BY e.sent_at DESC LIMIT 1", (test_to,)).fetchone()
    return row[0] if row else None


def first_email_message(conn: sqlite3.Connection, contact_id: int) -> str | None:
    """Gmail id of the contact's first email, which a follow-up replies to (known after a sync)."""
    row = conn.execute("SELECT gmail_message_id FROM emails WHERE contact_id = ? AND kind = 'initial'"
                       " AND test_mode = 0 AND gmail_message_id IS NOT NULL", (contact_id,)).fetchone()
    return row[0] if row else None


def send_test(conn: sqlite3.Connection, client: N8nClient, email_ids: list[int], test_to: str,
              resume: bytes, resume_name: str) -> TestBatch:
    """Create a test batch of drafts/approved emails, hand it to W2. The batch row records the outcome."""
    template_id = conn.execute("SELECT template_id FROM emails WHERE id = ?", (email_ids[0],)).fetchone()
    if template_id is None:
        raise SendError("that email doesn't exist")
    return _submit_test(conn, client, template_id[0], email_ids, test_to, resume, resume_name,
                        lambda batch_id: build_test_payload(conn, email_ids, test_to, batch_id))


def send_follow_up_template_test(conn: sqlite3.Connection, client: N8nClient, test_to: str,
                                 resume: bytes, resume_name: str) -> TestBatch:
    """Test the Follow-up template before any follow-up is due: it is filled in for my self-test
    contact and sent as a reply in my self-test thread, so both the text and the threading show."""
    template = followups.follow_up_template(conn)
    first = conn.execute("SELECT e.*, c.id AS cid FROM emails e JOIN contacts c ON c.id = e.contact_id"
                         " WHERE e.kind = 'initial' AND e.test_mode = 0 AND e.gmail_message_id IS NOT NULL"
                         " AND lower(c.email) = lower(?) ORDER BY e.sent_at DESC LIMIT 1", (test_to,)).fetchone()
    if first is None:
        raise SendError("send one real self-test email first (step 4) and sync replies: the follow-up test "
                        "replies in that thread")
    contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (first["cid"],)).fetchone()
    settings = db.get_settings(conn)
    merged = merge(template, values_for(contact, settings, ""))
    if not merged.ok:
        raise SendError("; ".join(merged.errors))

    def payload(batch_id: int) -> dict:
        return {"batch_id": batch_id, "test_mode": True, "sender_name": settings.get("my_name", ""),
                "spacing_min_s": TEST_SPACING_S[0], "spacing_max_s": TEST_SPACING_S[1],
                "emails": [{"email_id": f"test-{batch_id}-follow-up", "to": test_to,
                            "subject": followups.reply_subject(first["subject"]), "body_text": merged.body_text,
                            "body_html": to_html(merged.body_text), "reply_to_message_id": first["gmail_message_id"]}]}

    return _submit_test(conn, client, template.id, [-template.id], test_to, resume, resume_name, payload)


def _submit_test(conn, client, template_id, email_ids, test_to, resume, resume_name, build) -> TestBatch:
    check_resume(resume)
    if (repeat := recent_identical_test(conn, email_ids)) is not None:
        raise SendError(f"this exact test went out {repeat} s ago (a double click?); check {test_to} first. "
                        f"The same test can be sent again after {REPEAT_COOLDOWN_S // 60} minutes")
    with conn:
        batch_id = conn.execute("INSERT INTO batches (template_id, test_mode, status, created_at) VALUES (?, 1, 'draft', ?)",
                                (template_id, db.now())).lastrowid
    try:
        payload = build(batch_id)
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
    return TestBatch(batch_id=batch_id, template_id=template_id, email_ids=email_ids, to=test_to)


def recent_identical_test(conn: sqlite3.Connection, email_ids: list[int]) -> int | None:
    """Seconds since the same set of emails was last submitted as a test, if within the cooldown."""
    now = datetime.now(timezone.utc)
    for row in conn.execute("SELECT detail, created_at FROM events WHERE kind = 'test_batch_submitted'"
                            " ORDER BY id DESC LIMIT 20"):
        age = (now - datetime.fromisoformat(row["created_at"])).total_seconds()
        if age < REPEAT_COOLDOWN_S and sorted(json.loads(row["detail"]).get("email_ids", [])) == sorted(email_ids):
            return int(age)
    return None


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


# --- real batches -------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))   # no daylight saving, so a fixed offset is exact
MAX_BATCH = 20                                   # W2 refuses more, whatever the cap says
RUN_MARGIN_S = 300                               # a batch counts as running a bit past its last send
DEFAULT_RULES = {"daily_cap": 20, "spacing_min_s": 180, "spacing_max_s": 420,
                 "window_start": "09:30", "window_end": "18:00"}


class SendRules(BaseModel):
    daily_cap: int
    spacing_min_s: int
    spacing_max_s: int
    window_start: time
    window_end: time


def send_rules(conn: sqlite3.Connection) -> SendRules:
    stored = db.get_settings(conn)
    values = {k: stored.get(k, str(v)) for k, v in DEFAULT_RULES.items()}
    return SendRules(daily_cap=int(values["daily_cap"]), spacing_min_s=int(values["spacing_min_s"]),
                     spacing_max_s=int(values["spacing_max_s"]),
                     window_start=time.fromisoformat(values["window_start"]),
                     window_end=time.fromisoformat(values["window_end"]))


def save_send_rules(conn: sqlite3.Connection, rules: SendRules) -> None:
    if not 1 <= rules.daily_cap <= 50:
        raise SendError("the daily cap must be between 1 and 50")
    if not 120 <= rules.spacing_min_s <= rules.spacing_max_s <= 3600:
        raise SendError("spacing must be at least 2 minutes, the maximum at least the minimum, at most 60 minutes")
    if rules.window_start >= rules.window_end:
        raise SendError("the sending window must start before it ends")
    db.set_settings(conn, daily_cap=str(rules.daily_cap), spacing_min_s=str(rules.spacing_min_s),
                    spacing_max_s=str(rules.spacing_max_s), window_start=rules.window_start.strftime("%H:%M"),
                    window_end=rules.window_end.strftime("%H:%M"))
    with conn:
        db.log_event(conn, "send_rules_saved", **{k: str(v) for k, v in rules.model_dump().items()})


def queued_today(conn: sqlite3.Connection, now: datetime) -> int:
    """Real emails handed to W2 on today's date in India. Released emails don't count."""
    today = now.astimezone(IST).date()
    rows = conn.execute("SELECT queued_at FROM emails WHERE test_mode = 0 AND queued_at IS NOT NULL")
    return sum(datetime.fromisoformat(r[0]).astimezone(IST).date() == today for r in rows)


def running_batch(conn: sqlite3.Connection, now: datetime, rules: SendRules) -> tuple[int, datetime] | None:
    """(batch id, when it should be done) for a real batch that may still be sending."""
    for b in conn.execute("SELECT b.id, b.submitted_at, COUNT(e.id) AS n FROM batches b"
                          " JOIN emails e ON e.batch_id = b.id WHERE b.test_mode = 0 AND b.status = 'submitted'"
                          " GROUP BY b.id ORDER BY b.id DESC"):
        done_by = (datetime.fromisoformat(b["submitted_at"])
                   + timedelta(seconds=(b["n"] - 1) * rules.spacing_max_s + RUN_MARGIN_S))
        if done_by > now:
            return b["id"], done_by
    return None


class BatchPlan(BaseModel):
    email_ids: list[int]
    problems: list[str] = []
    self_test: bool = False       # every recipient is my own test address
    finishes_by: datetime | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def plan_real_batch(conn: sqlite3.Connection, email_ids: list[int], now: datetime, test_to: str) -> BatchPlan:
    """Every rule, checked against the database as it is now. Nothing is changed here."""
    rules = send_rules(conn)
    plan = BatchPlan(email_ids=email_ids)
    problems = plan.problems
    limit = min(MAX_BATCH, rules.daily_cap)
    if not 1 <= len(email_ids) <= limit:
        problems.append(f"a real batch has 1 to {limit} emails")
        return plan
    marks = ",".join("?" * len(email_ids))
    rows = conn.execute(
        f"SELECT e.*, c.email AS address, t.name AS template_name, t.version, t.test_checked_at FROM emails e"
        f" JOIN contacts c ON c.id = e.contact_id JOIN templates t ON t.id = e.template_id WHERE e.id IN ({marks})",
        email_ids).fetchall()
    if len(rows) != len(set(email_ids)):
        problems.append("some of these emails don't exist")
        return plan
    for r in rows:
        if r["status"] != "approved":
            problems.append(f"#{r['id']} is {r['status']}, not approved")
        if r["kind"] == "follow_up" and first_email_message(conn, r["contact_id"]) is None:
            problems.append(f"#{r['id']}: the first email's Gmail thread isn't known yet; sync replies first")
        for p in review.check_draft(conn, r["id"]).blocking:
            problems.append(f"#{r['id']}: {p.text}")
    if len({r["template_id"] for r in rows}) != 1:
        problems.append("one template version per batch")
    for r in {r["template_id"]: r for r in rows}.values():
        if not r["test_checked_at"]:
            problems.append(f"{r['template_name']} v{r['version']} has no checked test email yet (rule 2)")

    plan.self_test = all(r["address"].lower() == test_to.lower() for r in rows)
    local = now.astimezone(IST)
    plan.finishes_by = now + timedelta(seconds=(len(rows) - 1) * rules.spacing_max_s)
    if not plan.self_test:
        if local.weekday() >= 5:
            problems.append("real emails go out Monday to Friday only (India time)")
        elif not rules.window_start <= local.time() < rules.window_end:
            problems.append(f"outside the sending window ({rules.window_start:%H:%M}-{rules.window_end:%H:%M} IST); "
                            f"it's {local:%H:%M} now")
        else:
            end = datetime.combine(local.date(), rules.window_end, IST)
            if plan.finishes_by > end:
                fits = 1 + int((end - now).total_seconds() // rules.spacing_max_s)
                problems.append(f"{len(rows)} emails at up to {rules.spacing_max_s // 60} min apart could run until "
                                f"{plan.finishes_by.astimezone(IST):%H:%M}, past the window's end at "
                                f"{rules.window_end:%H:%M}; send at most {fits} now")
    already = queued_today(conn, now)
    if already + len(rows) > rules.daily_cap:
        problems.append(f"daily cap: {already} already went out today and the cap is {rules.daily_cap}, so at most "
                        f"{max(rules.daily_cap - already, 0)} more today")
    if running := running_batch(conn, now, rules):
        problems.append(f"batch #{running[0]} may still be sending until about "
                        f"{running[1].astimezone(IST):%H:%M}; one batch at a time")
    return plan


class RealBatch(BaseModel):
    batch_id: int
    email_ids: list[int]
    finishes_by: datetime


def send_real(conn: sqlite3.Connection, client: N8nClient, email_ids: list[int], *, confirmed: int,
              test_to: str, resume: bytes, resume_name: str, now: datetime | None = None) -> RealBatch:
    """Queue approved emails and hand them to W2 for real. `confirmed` is the number I typed."""
    now = now or datetime.now(timezone.utc)
    if confirmed != len(email_ids):
        raise SendError(f"type the number of emails ({len(email_ids)}) to confirm")
    check_resume(resume)
    plan = plan_real_batch(conn, email_ids, now, test_to)
    if not plan.ok:
        raise SendError("not sent: " + "; ".join(plan.problems))
    rules = send_rules(conn)
    stamp = now.isoformat(timespec="seconds")
    rows = conn.execute(f"SELECT e.*, c.email AS address FROM emails e JOIN contacts c ON c.id = e.contact_id"
                        f" WHERE e.id IN ({','.join('?' * len(email_ids))})", email_ids).fetchall()
    rows = sorted(rows, key=lambda r: email_ids.index(r["id"]))
    with conn:   # one transaction: the batch and its queued emails exist together or not at all
        batch_id = conn.execute("INSERT INTO batches (template_id, test_mode, status, created_at)"
                                " VALUES (?, 0, 'draft', ?)", (rows[0]["template_id"], stamp)).lastrowid
        for r in rows:   # test_mode = 0 brings the database's one-email-per-contact index into force
            conn.execute("UPDATE emails SET status = 'queued', test_mode = 0, batch_id = ?, queued_at = ?"
                         " WHERE id = ? AND status = 'approved'", (batch_id, stamp, r["id"]))
        db.log_event(conn, "real_batch_queued", batch_id=batch_id, email_ids=email_ids, confirmed=confirmed)
    payload = {
        "batch_id": batch_id, "test_mode": False, "sender_name": db.get_settings(conn).get("my_name", ""),
        "spacing_min_s": rules.spacing_min_s, "spacing_max_s": rules.spacing_max_s,
        "emails": [{"email_id": str(r["id"]), "to": r["address"], "subject": r["subject"],
                    "body_text": r["body_text"], "body_html": to_html(r["body_text"]),
                    **({"reply_to_message_id": first_email_message(conn, r["contact_id"])}
                       if r["kind"] == "follow_up" else {})} for r in rows],
    }
    try:
        client.send_batch(payload, resume, resume_name)
    except N8nError as exc:
        with conn:
            conn.execute("UPDATE batches SET status = 'failed' WHERE id = ?", (batch_id,))
            db.log_event(conn, "real_batch_failed", batch_id=batch_id, error=str(exc), maybe_sent=exc.maybe_sent)
        if exc.maybe_sent:
            raise SendError(f"{exc} The emails stay 'queued' until you've checked: if n8n shows nothing was "
                            f"sent, release batch #{batch_id} below.") from None
        release_batch(conn, batch_id, nothing_sent_confirmed=True, reason="W2 refused the batch")
        raise SendError(f"W2 refused the batch, nothing was sent: {exc}") from None
    with conn:
        conn.execute("UPDATE batches SET status = 'submitted', submitted_at = ? WHERE id = ?", (stamp, batch_id))
        db.log_event(conn, "real_batch_submitted", batch_id=batch_id)
    return RealBatch(batch_id=batch_id, email_ids=email_ids, finishes_by=plan.finishes_by)


def release_batch(conn: sqlite3.Connection, batch_id: int, *, nothing_sent_confirmed: bool, reason: str = "") -> int:
    """A failed batch's queued emails go back to approved. Only when nothing was sent."""
    if not nothing_sent_confirmed:
        raise SendError("check n8n -> Executions first and confirm that nothing was sent")
    batch = conn.execute("SELECT status FROM batches WHERE id = ? AND test_mode = 0", (batch_id,)).fetchone()
    if batch is None or batch["status"] != "failed":
        raise SendError("only a failed real batch can be released")
    with conn:
        n = conn.execute("UPDATE emails SET status = 'approved', test_mode = 1, batch_id = NULL, queued_at = NULL"
                         " WHERE batch_id = ? AND status = 'queued'", (batch_id,)).rowcount
        db.log_event(conn, "real_batch_released", batch_id=batch_id, emails=n, reason=reason)
    return n


def mark_stopped(conn: sqlite3.Connection, batch_id: int) -> None:
    """I stopped the execution in n8n. Its emails stay queued: some went out, and W3 (Step 8) tells which."""
    with conn:
        changed = conn.execute("UPDATE batches SET status = 'stopped' WHERE id = ? AND test_mode = 0"
                               " AND status = 'submitted'", (batch_id,)).rowcount
        if not changed:
            raise SendError("only a submitted real batch can be marked as stopped")
        db.log_event(conn, "real_batch_stopped", batch_id=batch_id)


def real_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT b.*, t.name AS template_name, t.version, COUNT(e.id) AS emails,"
        " COALESCE(SUM(e.status = 'queued'), 0) AS queued FROM batches b JOIN templates t ON t.id = b.template_id"
        " LEFT JOIN emails e ON e.batch_id = b.id WHERE b.test_mode = 0 GROUP BY b.id ORDER BY b.id DESC").fetchall()


def sendable_emails(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Approved emails whose template version has a checked test."""
    return conn.execute(
        "SELECT e.id, e.template_id, c.company, c.name, c.email, t.name AS template_name, t.version FROM emails e"
        " JOIN contacts c ON c.id = e.contact_id JOIN templates t ON t.id = e.template_id"
        " WHERE e.status = 'approved' AND t.test_checked_at IS NOT NULL ORDER BY e.id").fetchall()
