"""The review queue: drafts -> my explicit approval (rule 1). Nothing here sends anything.

A draft is one finished email for one contact: the template merged with the contact's details
and the checked personal line. Every approval re-runs the checks on the text as it is now, so
an edit can't slip past them:

- **Blocking problems** can't be overridden: the address is on the do-not-contact list, or the
  contact already has (or had) an email. Rule 5 is never bent.
- **Other problems** (a personal line that fails its checks, a leftover placeholder, a missing
  opt-out line) block approval unless I tick an override and say why; the reason is logged.

"Approve all" exists only after my first 2 real batches went out, and only after I've looked
at 5 drafts in the queue; it approves only drafts with no problems at all.
"""

import json
import re
import sqlite3

from pydantic import BaseModel

from mailmate import db
from mailmate.n8n import Source
from mailmate.sentence_check import PersonalLine, check_sentence
from mailmate.templates import OPT_OUT_LINE, Template, merge, values_for

INDIVIDUAL_BATCHES = 2      # real batches that must be approved one email at a time
SAMPLES_BEFORE_BULK = 5     # drafts I must look at before "approve all" unlocks

_PLACEHOLDER = re.compile(r"\{[^{}\n]*\}|\[[^\]\n]{0,60}\]")
_DONE_STATUSES = ("contacted", "followed_up", "replied", "bounced")


class ReviewError(ValueError):
    """An action that the rules don't allow. The message says why."""


class Problem(BaseModel):
    text: str
    blocking: bool = False      # True: no override possible


class DraftCheck(BaseModel):
    problems: list[Problem] = []
    notes: list[str] = []       # worth knowing, not a problem

    @property
    def blocking(self) -> list[Problem]:
        return [p for p in self.problems if p.blocking]

    @property
    def clean(self) -> bool:
        return not self.problems


def _email(conn: sqlite3.Connection, email_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT e.*, c.company, c.role, c.requirements, c.email AS address, c.name, c.status AS contact_status,"
        " t.name AS template_name, t.version AS template_version, t.subject AS t_subject, t.body AS t_body"
        " FROM emails e JOIN contacts c ON c.id = e.contact_id JOIN templates t ON t.id = e.template_id"
        " WHERE e.id = ?", (email_id,)).fetchone()
    if row is None:
        raise ReviewError(f"email {email_id} doesn't exist")
    return row


def _sources(row) -> list[Source]:
    return [Source(**s) for s in json.loads(row["sources"] or "[]")]


def _merged(row, settings: dict[str, str], sentence: str):
    template = Template(name=row["template_name"], subject=row["t_subject"], body=row["t_body"])
    return merge(template, values_for(row, settings, sentence))


# --- drafting -----------------------------------------------------------------------------

def contacts_to_draft(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Contacts that can get a first email: new, not on the do-not-contact list."""
    return conn.execute(
        "SELECT * FROM contacts c WHERE c.status = 'new'"
        " AND c.email NOT IN (SELECT email FROM suppression)"
        " AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.contact_id = c.id AND e.status != 'rejected')"
        " ORDER BY c.id").fetchall()


def create_draft(conn: sqlite3.Connection, contact_id: int, template: Template, line: PersonalLine) -> int:
    """Merge one email for one contact and store it as a draft. Returns the email id."""
    contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if contact is None:
        raise ReviewError(f"contact {contact_id} doesn't exist")
    if contact["email"].lower() in db.suppressed(conn):
        raise ReviewError(f"{contact['email']} is on the do-not-contact list")
    if contact["status"] != "new" or conn.execute(
            "SELECT 1 FROM emails WHERE contact_id = ? AND status != 'rejected'", (contact_id,)).fetchone():
        raise ReviewError(f"{contact['email']} already has an email")
    if template.id is None:
        raise ReviewError("save the template before drafting from it")
    settings = db.get_settings(conn)
    merged = merge(template, values_for(contact, settings, line.sentence))
    if not merged.ok:
        raise ReviewError(f"{contact['email']}: " + "; ".join(merged.errors))
    with conn:
        cur = conn.execute(
            "INSERT INTO emails (contact_id, template_id, sentence, sources, subject, body_text, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)",
            (contact_id, template.id, line.sentence, json.dumps([s.model_dump() for s in line.sources]),
             merged.subject, merged.body_text, db.now()))
        conn.execute("UPDATE contacts SET status = 'drafted' WHERE id = ?", (contact_id,))
        db.log_event(conn, "draft_created", contact_id=contact_id, email_id=cur.lastrowid,
                     template=f"{template.name} v{template.version}", model=line.model,
                     attempts=[{"sentence": a.raw, "problems": a.problems} for a in line.attempts])
    return cur.lastrowid


# --- checking -----------------------------------------------------------------------------

def check_draft(conn: sqlite3.Connection, email_id: int) -> DraftCheck:
    row = _email(conn, email_id)
    settings = db.get_settings(conn)
    check = DraftCheck()

    def problem(text: str, blocking: bool = False) -> None:
        check.problems.append(Problem(text=text, blocking=blocking))

    # Rule 5, never overridable.
    if row["address"].lower() in db.suppressed(conn):
        problem(f"{row['address']} is on the do-not-contact list", blocking=True)
    if row["contact_status"] in _DONE_STATUSES:
        problem(f"{row['address']} was already contacted ({row['contact_status'].replace('_', ' ')})", blocking=True)
    other = conn.execute("SELECT id, status FROM emails WHERE contact_id = ? AND id != ? AND kind = ?"
                         " AND status != 'rejected'", (row["contact_id"], email_id, row["kind"])).fetchone()
    if other:
        problem(f"this contact already has another email (#{other['id']}, {other['status']})", blocking=True)

    # The personal line, re-checked against the sources it was written from.
    sentence = row["sentence"]
    if sentence:
        verdict = check_sentence(sentence, _sources(row), company=row["company"], role=row["role"],
                                 requirements=row["requirements"], my_name=settings.get("my_name", ""))
        for p in verdict.problems:
            problem(f"personal line: {p}")
        if sentence not in row["body_text"]:
            check.notes.append("the personal line is no longer in the email text (edited by hand)")
    else:
        check.notes.append("no personal line: the email goes out without one")

    # The final text.
    subject, body = row["subject"], row["body_text"]
    if not subject.strip() or "\n" in subject.strip():
        problem("the subject must be one non-empty line")
    for where, text in (("subject", subject), ("body", body)):
        for placeholder in _PLACEHOLDER.findall(text):
            problem(f"'{placeholder}' in the {where} looks like a placeholder")
    if OPT_OUT_LINE not in body:
        problem("the opt-out line is missing (rule 6)")
    if not re.search(re.escape(row["company"]), subject + body, re.I):
        problem(f"the email doesn't mention {row['company']}")
    merged = _merged(row, settings, sentence)
    if merged.ok and (merged.subject, merged.body_text) != (subject, body):
        check.notes.append("edited by hand: differs from the template")
    return check


# --- editing ------------------------------------------------------------------------------

def _editable(row) -> None:
    if row["status"] not in ("draft", "approved"):
        raise ReviewError(f"email #{row['id']} is {row['status']} and can't be changed")


def set_personal_line(conn: sqlite3.Connection, email_id: int, sentence: str,
                      sources: list[Source] | None = None) -> None:
    """New personal line (typed by me, or a new W1 answer with its sources). The email text is
    rebuilt from the template, so hand edits to subject/body are replaced. Back to draft."""
    row = _email(conn, email_id)
    _editable(row)
    sentence = " ".join(sentence.split())
    merged = _merged(row, db.get_settings(conn), sentence)
    if not merged.ok:
        raise ReviewError("; ".join(merged.errors))
    source_json = row["sources"] if sources is None else json.dumps([s.model_dump() for s in sources])
    with conn:
        conn.execute("UPDATE emails SET sentence = ?, sources = ?, subject = ?, body_text = ?, status = 'draft',"
                     " approved_at = NULL WHERE id = ?",
                     (sentence, source_json, merged.subject, merged.body_text, email_id))
        db.log_event(conn, "draft_edited", contact_id=row["contact_id"], email_id=email_id, what="personal line",
                     sentence=sentence)


def edit_text(conn: sqlite3.Connection, email_id: int, subject: str, body: str) -> None:
    """Hand edit of the final subject and body. Back to draft; the checks run again on approval."""
    row = _email(conn, email_id)
    _editable(row)
    subject, body = " ".join(subject.split()), body.replace("\r\n", "\n").strip()
    with conn:
        conn.execute("UPDATE emails SET subject = ?, body_text = ?, status = 'draft', approved_at = NULL"
                     " WHERE id = ?", (subject, body, email_id))
        db.log_event(conn, "draft_edited", contact_id=row["contact_id"], email_id=email_id, what="text")


# --- deciding -----------------------------------------------------------------------------

def approve(conn: sqlite3.Connection, email_id: int, override_reason: str = "") -> None:
    row = _email(conn, email_id)
    if row["status"] != "draft":
        raise ReviewError(f"email #{email_id} is {row['status']}, not a draft")
    check = check_draft(conn, email_id)
    if check.blocking:
        raise ReviewError("can't be approved: " + "; ".join(p.text for p in check.blocking))
    if check.problems and not override_reason.strip():
        raise ReviewError("has problems; approving it needs an override with a reason: "
                          + "; ".join(p.text for p in check.problems))
    with conn:
        conn.execute("UPDATE emails SET status = 'approved', approved_at = ? WHERE id = ?", (db.now(), email_id))
        db.log_event(conn, "email_approved", contact_id=row["contact_id"], email_id=email_id,
                     overridden=[p.text for p in check.problems], reason=override_reason.strip())


def unapprove(conn: sqlite3.Connection, email_id: int) -> None:
    row = _email(conn, email_id)
    if row["status"] != "approved":
        raise ReviewError(f"email #{email_id} is {row['status']}, not approved")
    with conn:
        conn.execute("UPDATE emails SET status = 'draft', approved_at = NULL WHERE id = ?", (email_id,))
        db.log_event(conn, "email_unapproved", contact_id=row["contact_id"], email_id=email_id)


def reject(conn: sqlite3.Connection, email_id: int, never_contact: bool = False) -> None:
    """Drop the draft. The contact can be drafted again later, unless `never_contact`."""
    row = _email(conn, email_id)
    _editable(row)
    with conn:
        conn.execute("UPDATE emails SET status = 'rejected', approved_at = NULL WHERE id = ?", (email_id,))
        conn.execute("UPDATE contacts SET status = 'new' WHERE id = ? AND status = 'drafted'", (row["contact_id"],))
        db.log_event(conn, "email_rejected", contact_id=row["contact_id"], email_id=email_id,
                     never_contact=never_contact)
    if never_contact:
        db.suppress(conn, row["address"], "manual", note="rejected in the review queue")


# --- approve all --------------------------------------------------------------------------

def bulk_approval_status(conn: sqlite3.Connection, reviewed: int) -> tuple[bool, str]:
    """(allowed, why not). `reviewed`: drafts I've opened in the queue this session."""
    real_batches = conn.execute("SELECT COUNT(*) FROM batches WHERE test_mode = 0 AND status IN ('submitted', 'done')"
                                ).fetchone()[0]
    if real_batches < INDIVIDUAL_BATCHES:
        return False, (f"the first {INDIVIDUAL_BATCHES} real batches are approved one email at a time "
                       f"({real_batches} sent so far)")
    if reviewed < SAMPLES_BEFORE_BULK:
        return False, f"look at {SAMPLES_BEFORE_BULK} drafts first ({reviewed} so far)"
    return True, ""


def approve_all_clean(conn: sqlite3.Connection, reviewed: int) -> tuple[int, int]:
    """Approve every draft that has no problem at all. Returns (approved, left for one-by-one review)."""
    allowed, why = bulk_approval_status(conn, reviewed)
    if not allowed:
        raise ReviewError(f"approve all isn't available yet: {why}")
    approved = left = 0
    for (email_id,) in conn.execute("SELECT id FROM emails WHERE status = 'draft' ORDER BY id").fetchall():
        if check_draft(conn, email_id).clean:
            approve(conn, email_id)
            approved += 1
        else:
            left += 1
    return approved, left
