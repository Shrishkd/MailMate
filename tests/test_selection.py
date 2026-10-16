"""One recruiter per company at a time, High priority first: the company rule, suggestions, priority."""

from datetime import datetime, timedelta

import httpx
import pytest

from mailmate import db, review, selection, sending
from mailmate.importer import Contact, analyse, normalize_priority
from mailmate.n8n import N8nClient, N8nSettings
from mailmate.sending import IST
from mailmate.sentence_check import PersonalLine
from mailmate.templates import save_template
from tests.test_importer import csv_bytes

PDF = b"%PDF-1.7\n" + b"x" * 100
TEST_TO = "me.secondary@example.net"
MONDAY_10 = datetime(2026, 10, 12, 10, 0, tzinfo=IST)

PEOPLE = [  # company, email, priority (file order = this order)
    ("Fractal Analytics", "f1@fractal.example", "medium"),
    ("Fractal Analytics", "f2@fractal.example", "high"),
    ("Fractal Analytics", "f3@fractal.example", "high"),
    ("NVIDIA", "n1@nvidia.example", "high"),
    ("NVIDIA", "n2@nvidia.example", ""),
    ("Oracle", "o1@oracle.example", ""),
    ("Lexsi.ai", "l1@lexsi.example", "medium"),
    ("Self", TEST_TO, "high"),
]


@pytest.fixture
def world(conn):
    """Everybody imported and approved, template test-checked. Returns {email: email id}."""
    db.set_settings(conn, my_name="Shrish", signature="Thanks,\nShrish")
    template = save_template(conn, "cold", "{role} at {company}?", "Hey,\n\n{company} rocks.\n\n{signature}")
    db.add_contacts(conn, [Contact(company=c, role="AI Engineer", email=e, priority=p) for c, e, p in PEOPLE], "jobs.csv")
    ids = {}
    for c in conn.execute("SELECT id, email FROM contacts ORDER BY id").fetchall():
        email_id = review.create_draft(conn, c["id"], template, PersonalLine())
        review.approve(conn, email_id)
        ids[c["email"]] = email_id
    conn.execute("UPDATE templates SET test_checked_at = ?", (db.now(),))
    return ids


def send(conn, ids, now=MONDAY_10):
    batch_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM batches").fetchone()[0]
    client = N8nClient(N8nSettings(base_url="https://demo.app.n8n.cloud", token="tok"), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"accepted": len(ids), "batch_id": batch_id})))
    return sending.send_real(conn, client, ids, confirmed=len(ids), test_to=TEST_TO, resume=PDF, resume_name="cv.pdf",
                             now=now)


def mark(conn, email_id, status, sent_at=MONDAY_10):
    conn.execute("UPDATE emails SET status = ?, sent_at = ? WHERE id = ?", (status, sent_at.isoformat(), email_id))


def reply(conn, email_id, category):
    conn.execute("INSERT INTO replies (email_id, gmail_thread_id, from_addr, date, category, created_at)"
                 " VALUES (?, 't', 'x', ?, ?, ?)", (email_id, MONDAY_10.isoformat(), category, db.now()))


def problems(conn, ids, now):
    return sending.plan_real_batch(conn, ids, now, TEST_TO).problems


# --- names and priorities -----------------------------------------------------------------------

@pytest.mark.parametrize("a, b", [("Lexsi.ai", "Lexsi AI"), ("Acme Pvt. Ltd.", "acme"), ("NVIDIA", "Nvidia Inc")])
def test_one_company_written_differently(a, b):
    assert selection.company_key(a) == selection.company_key(b)


@pytest.mark.parametrize("value, expected", [("High", "high"), ("P1", "high"), ("medium", "medium"), ("Med", "medium"),
                                             ("Low", "low"), ("", ""), ("someday", "")])
def test_priority_values(value, expected):
    assert normalize_priority(value) == expected


def test_the_priority_column_is_imported(conn, fake_dns):
    report = analyse("jobs.csv", csv_bytes(["Company", "Email", "Priority"], ["Acme", "a@nimbuslabs.ai", "High"]),
                     conn, fake_dns, default_role="AI Engineer")
    assert report.valid[0].contact.priority == "high"


# --- the company rule in a real batch ----------------------------------------------------------------

def test_two_recruiters_at_one_company_in_a_batch_are_refused(conn, world):
    ids = [world["f1@fractal.example"], world["f2@fractal.example"]]
    assert f"#{ids[0]} and #{ids[1]} are both at Fractal Analytics: one recruiter per company at a time" \
        in problems(conn, ids, MONDAY_10)


def test_a_company_waits_while_its_recruiter_has_a_chance(conn, world):
    """No follow-up: 6 days for the first email to be answered, then 4 for the follow-up's turn."""
    send(conn, [world["n1@nvidia.example"]])
    mark(conn, world["n1@nvidia.example"], "sent")
    later = MONDAY_10 + timedelta(days=2)
    assert "NVIDIA is waiting on n1@nvidia.example (sent 12 Oct, no reply yet; their follow-up goes first) until " \
        "22 Oct" in problems(conn, [world["n2@nvidia.example"]], later)[0]
    assert problems(conn, [world["n2@nvidia.example"]], MONDAY_10 + timedelta(days=7))      # follow-up due, not sent
    assert problems(conn, [world["n2@nvidia.example"]], MONDAY_10 + timedelta(days=10)) == []   # Thursday 22 Oct


def follow_up(conn, world, status, sent_at=None):
    """n1 got a first email on Monday 12 Oct and no answer; their follow-up is in `status`."""
    send(conn, [world["n1@nvidia.example"]])
    mark(conn, world["n1@nvidia.example"], "sent")
    contact_id = conn.execute("SELECT contact_id FROM emails WHERE id = ?", (world["n1@nvidia.example"],)).fetchone()[0]
    conn.execute("INSERT INTO emails (contact_id, template_id, kind, subject, body_text, status, test_mode, queued_at,"
                 " sent_at, created_at) VALUES (?, 1, 'follow_up', 'Re: s', 'b', ?, ?, ?, ?, ?)",
                 (contact_id, status, 0 if status in ("queued", "sent") else 1,
                  sent_at.isoformat() if sent_at else None, sent_at.isoformat() if sent_at else None, db.now()))


def test_the_next_recruiter_waits_4_days_after_the_follow_up(conn, world):
    follow_up(conn, world, "sent", sent_at=MONDAY_10 + timedelta(days=8))           # Tuesday 20 Oct
    found = problems(conn, [world["n2@nvidia.example"]], MONDAY_10 + timedelta(days=11))
    assert "NVIDIA is waiting on n1@nvidia.example (followed up 20 Oct, no reply yet) until 24 Oct" in found[0]
    assert problems(conn, [world["n2@nvidia.example"]], MONDAY_10 + timedelta(days=14)) == []   # Monday 26 Oct


@pytest.mark.parametrize("status, words", [("draft", "drafted, not sent yet"), ("approved", "approved, not sent yet"),
                                           ("queued", "queued")])
def test_a_follow_up_not_sent_yet_keeps_the_company_waiting(conn, world, status, words):
    follow_up(conn, world, status)
    found = problems(conn, [world["n2@nvidia.example"]], MONDAY_10 + timedelta(days=30))
    assert found == [f"#{world['n2@nvidia.example']}: NVIDIA is waiting on n1@nvidia.example (their follow-up is {words})"]


def test_a_rejected_follow_up_doesnt_hold_the_company(conn, world):
    follow_up(conn, world, "rejected")
    assert problems(conn, [world["n2@nvidia.example"]], MONDAY_10 + timedelta(days=10)) == []


def test_a_queued_email_makes_its_company_wait(conn, world):
    send(conn, [world["n1@nvidia.example"]])
    assert "waiting on n1@nvidia.example (queued)" in problems(conn, [world["n2@nvidia.example"]],
                                                                 MONDAY_10 + timedelta(hours=1))[0]


def test_a_bounce_frees_the_company_at_once(conn, world):
    send(conn, [world["n1@nvidia.example"]])
    mark(conn, world["n1@nvidia.example"], "bounced")
    reply(conn, world["n1@nvidia.example"], "bounce")
    assert problems(conn, [world["n2@nvidia.example"]], MONDAY_10 + timedelta(hours=1)) == []


@pytest.mark.parametrize("category", ["interview_request", "not_interested", "question"])
def test_a_real_answer_closes_the_company(conn, world, category):
    send(conn, [world["n1@nvidia.example"]])
    mark(conn, world["n1@nvidia.example"], "replied")
    reply(conn, world["n1@nvidia.example"], category)
    found = problems(conn, [world["n2@nvidia.example"]], MONDAY_10 + timedelta(days=30))
    assert f"NVIDIA is closed: n1@nvidia.example already answered ({category.replace('_', ' ')})" in found[0]


def test_an_auto_reply_doesnt_close_the_company(conn, world):
    send(conn, [world["n1@nvidia.example"]])
    mark(conn, world["n1@nvidia.example"], "sent")
    reply(conn, world["n1@nvidia.example"], "auto_reply")
    assert problems(conn, [world["n2@nvidia.example"]], MONDAY_10 + timedelta(days=10)) == []


def test_my_test_address_never_closes_a_company(conn, world):
    conn.execute("UPDATE contacts SET company = 'Oracle' WHERE email = ?", (TEST_TO,))
    conn.execute("UPDATE emails SET subject = replace(subject, 'Self', 'Oracle'), body_text = replace(body_text, 'Self', 'Oracle')"
                 " WHERE id = ?", (world[TEST_TO],))
    send(conn, [world[TEST_TO]])
    mark(conn, world[TEST_TO], "replied")
    reply(conn, world[TEST_TO], "interview_request")
    assert problems(conn, [world["o1@oracle.example"]], MONDAY_10 + timedelta(hours=1)) == []


# --- suggestions ------------------------------------------------------------------------------------

def test_draft_suggestions_one_per_company_high_priority_first(conn, world):
    conn.execute("UPDATE emails SET status = 'rejected'")
    conn.execute("UPDATE contacts SET status = 'new'")
    candidates = review.contacts_to_draft(conn)
    assert [c["email"] for c in candidates][:3] == ["f2@fractal.example", "f3@fractal.example", "n1@nvidia.example"]
    chosen = selection.suggest_drafts(conn, candidates, MONDAY_10, TEST_TO, limit=10)
    emails = [conn.execute("SELECT email FROM contacts WHERE id = ?", (i,)).fetchone()[0] for i in chosen]
    # High: f2 (Fractal), n1 (NVIDIA); Medium: l1 (Lexsi); none: o1 (Oracle). One per company; never my address.
    assert emails == ["f2@fractal.example", "n1@nvidia.example", "l1@lexsi.example", "o1@oracle.example"]
    assert len(selection.suggest_drafts(conn, candidates, MONDAY_10, TEST_TO, limit=2)) == 2


def test_draft_suggestions_skip_companies_already_in_the_queue_or_waiting(conn, world):
    conn.execute("UPDATE emails SET status = 'rejected' WHERE id NOT IN (?, ?)",
                 (world["f1@fractal.example"], world["n1@nvidia.example"]))
    conn.execute("UPDATE contacts SET status = 'new' WHERE email NOT IN ('f1@fractal.example', 'n1@nvidia.example')")
    send(conn, [world["n1@nvidia.example"]])          # NVIDIA waits; Fractal has f1 approved in the queue
    chosen = selection.suggest_drafts(conn, review.contacts_to_draft(conn), MONDAY_10, TEST_TO, limit=10)
    companies = {conn.execute("SELECT company FROM contacts WHERE id = ?", (i,)).fetchone()[0] for i in chosen}
    assert companies == {"Oracle", "Lexsi.ai"}


def test_send_suggestions_one_per_free_company_by_priority(conn, world):
    candidates = sending.sendable_emails(conn)
    chosen = selection.suggest_send(conn, candidates, MONDAY_10, TEST_TO, limit=20)
    assert chosen == [world["f2@fractal.example"], world["n1@nvidia.example"], world["l1@lexsi.example"],
                      world["o1@oracle.example"]]
    assert sending.plan_real_batch(conn, chosen, MONDAY_10, TEST_TO).ok      # a suggestion is always sendable
    assert selection.suggest_send(conn, candidates, MONDAY_10, TEST_TO, limit=2) == chosen[:2]
    assert selection.suggest_send(conn, candidates, MONDAY_10, TEST_TO, limit=0) == []


def test_capacity_now(conn, world):
    assert sending.capacity_now(conn, MONDAY_10.replace(day=10)) == (0, "weekend: real emails go out Monday to Friday")
    assert sending.capacity_now(conn, MONDAY_10.replace(hour=8))[0] == 0
    assert sending.capacity_now(conn, MONDAY_10) == (20, "20 left under today's cap")
    assert sending.capacity_now(conn, MONDAY_10.replace(hour=17, minute=40)) == (3, "3 fit before 18:00")
    send(conn, [world["o1@oracle.example"]])
    assert sending.capacity_now(conn, MONDAY_10 + timedelta(minutes=1))[0] == 0       # that batch is still running
    assert sending.capacity_now(conn, MONDAY_10 + timedelta(hours=1)) == (19, "19 left under today's cap")
