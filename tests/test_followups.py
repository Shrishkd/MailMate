"""The one follow-up: when it's due, drafting, review, sending in the same thread, never twice."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from mailmate import db, followups, review, sending
from mailmate.importer import Contact
from mailmate.n8n import N8nClient, N8nSettings, SyncResult
from mailmate.review import ReviewError
from mailmate.sending import IST
from mailmate.sentence_check import PersonalLine
from mailmate.sync import apply_sync
from mailmate.templates import FOLLOW_UP_TEMPLATE, OPT_OUT_LINE, save_template

PDF = b"%PDF-1.7\n" + b"x" * 100
TEST_TO = "me.secondary@example.net"
NOW = datetime.now(timezone.utc)
A_MONDAY = datetime(2026, 10, 5, 10, 0, tzinfo=IST)


def fake_w2(calls=None, batch_id=1, n=1):
    calls = [] if calls is None else calls

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"accepted": n, "batch_id": batch_id})
    return N8nClient(N8nSettings(base_url="https://demo.app.n8n.cloud", token="tok"), transport=httpx.MockTransport(handler))


def payload_of(request) -> dict:
    body = request.content.decode("utf-8", "replace")
    part = body[body.index('name="payload"'):]
    return json.loads(part.split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0])


def sync(conn, *threads):
    return apply_sync(conn, SyncResult.model_validate({"threads": list(threads)}), TEST_TO, now=NOW)


def gmail_thread(to, subject, sent_at, replies=(), later_sent=()):
    return {"thread_id": f"t-{to}", "sent_message_id": f"m-{to}", "to": to, "subject": subject,
            "sent_at": sent_at.isoformat(), "replies": list(replies), "later_sent": list(later_sent)}


@pytest.fixture
def first(conn):
    """Priya and Neha each got a first email, Gmail confirmed it (sync), 7 days ago."""
    db.set_settings(conn, my_name="Shrish", signature="Thanks,\nShrish")
    template = save_template(conn, "cold", "{role} at {company}?", "Hey {first_name},\n\n{company} rocks.\n\n{signature}")
    db.add_contacts(conn, [Contact(company="Nimbus Labs", role="AI Engineer", email="priya@nimbuslabs.ai", name="Priya"),
                           Contact(company="Quillstack", role="AI Engineer", email="neha@quillstack.io", name="Neha")],
                    "jobs.csv")
    ids = []
    for c in conn.execute("SELECT id FROM contacts ORDER BY id").fetchall():
        email_id = review.create_draft(conn, c["id"], template, PersonalLine())
        review.approve(conn, email_id)
        ids.append(email_id)
    conn.execute("UPDATE templates SET test_checked_at = ?", (db.now(),))
    sending.send_real(conn, fake_w2(n=2), ids, confirmed=2, test_to=TEST_TO, resume=PDF, resume_name="cv.pdf",
                      now=A_MONDAY)
    sync(conn, gmail_thread("priya@nimbuslabs.ai", "AI Engineer at Nimbus Labs?", A_MONDAY + timedelta(minutes=1)),
         gmail_thread("neha@quillstack.io", "AI Engineer at Quillstack?", A_MONDAY + timedelta(minutes=6)))
    conn.execute("UPDATE emails SET sent_at = ?", ((NOW - timedelta(days=7)).isoformat(),))
    return {"priya": ids[0], "neha": ids[1]}


def follow_up(conn, first_id):
    email_id = followups.create_follow_up(conn, first_id, NOW)
    return email_id, conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()


# --- when a follow-up is due -------------------------------------------------------------------

def test_sent_emails_without_a_reply_are_due_after_the_waiting_days(conn, first):
    assert [r["id"] for r in followups.due(conn, NOW)] == [first["priya"], first["neha"]]
    conn.execute("UPDATE emails SET sent_at = ? WHERE id = ?", ((NOW - timedelta(days=5)).isoformat(), first["neha"]))
    assert [r["id"] for r in followups.due(conn, NOW)] == [first["priya"]]
    followups.set_follow_up_days(conn, 4)
    assert len(followups.due(conn, NOW)) == 2


def test_replied_bounced_and_blocked_contacts_are_not_due(conn, first):
    reply = {"from": "neha@quillstack.io", "date": NOW.isoformat(), "category": "question", "confidence": 0.9}
    sync(conn, gmail_thread("neha@quillstack.io", "AI Engineer at Quillstack?", A_MONDAY, replies=[reply]))
    db.suppress(conn, "priya@nimbuslabs.ai", "manual")
    assert followups.due(conn, NOW) == []


def test_follow_ups_need_a_recent_reply_sync(conn, first):
    with pytest.raises(ReviewError, match="sync replies first"):
        followups.create_follow_up(conn, first["priya"], NOW + timedelta(hours=25))


# --- the follow-up draft ---------------------------------------------------------------------------

def test_the_follow_up_draft_replies_with_re_subject_and_the_default_text(conn, first):
    email_id, row = follow_up(conn, first["priya"])
    assert (row["kind"], row["status"], row["subject"]) == ("follow_up", "draft", "Re: AI Engineer at Nimbus Labs?")
    assert row["body_text"].startswith("Hey Priya,\n\nJust bringing my note about the AI Engineer role at Nimbus Labs")
    assert OPT_OUT_LINE in row["body_text"]
    template = followups.follow_up_template(conn)
    assert (template.name, row["template_id"]) == (FOLLOW_UP_TEMPLATE, template.id)
    check = review.check_draft(conn, email_id)
    assert check.clean and check.notes == [], check
    review.approve(conn, email_id)


def test_the_follow_up_template_cant_be_used_for_first_emails(conn, first):
    db.add_contacts(conn, [Contact(company="Acme", role="AI Engineer", email="x@nimbuslabs.ai")], "more.csv")
    contact_id = conn.execute("SELECT id FROM contacts WHERE email = 'x@nimbuslabs.ai'").fetchone()[0]
    with pytest.raises(ReviewError, match="for follow-ups only"):
        review.create_draft(conn, contact_id, followups.follow_up_template(conn), PersonalLine())


def test_rebuilding_a_follow_up_keeps_its_re_subject(conn, first):
    email_id, _ = follow_up(conn, first["priya"])
    newer = save_template(conn, FOLLOW_UP_TEMPLATE, followups.DEFAULT_SUBJECT, "Hi {first_name},\n\nA nudge about {company}.\n\n{signature}")
    review.rebuild_with_template(conn, email_id, newer)
    row = conn.execute("SELECT subject, body_text FROM emails WHERE id = ?", (email_id,)).fetchone()
    assert row["subject"] == "Re: AI Engineer at Nimbus Labs?" and row["body_text"].startswith("Hi Priya,")


def test_a_reply_after_drafting_blocks_the_follow_up(conn, first):
    email_id, _ = follow_up(conn, first["priya"])
    reply = {"from": "priya@nimbuslabs.ai", "date": NOW.isoformat(), "category": "interview_request", "confidence": 0.9}
    sync(conn, gmail_thread("priya@nimbuslabs.ai", "AI Engineer at Nimbus Labs?", A_MONDAY, replies=[reply]))
    blocking = [p.text for p in review.check_draft(conn, email_id).blocking]
    assert any("has replied since" in p for p in blocking)
    with pytest.raises(ReviewError, match="can't be approved"):
        review.approve(conn, email_id, override_reason="please")


# --- never more than one follow-up per contact ---------------------------------------------------

def test_never_more_than_one_follow_up_per_contact(conn, first):
    email_id, _ = follow_up(conn, first["priya"])
    with pytest.raises(ReviewError, match="isn't due"):                    # 1. drafting refuses
        followups.create_follow_up(conn, first["priya"], NOW)
    # 2. review blocks a second one slipped in by hand
    conn.execute("INSERT INTO emails (contact_id, template_id, kind, subject, body_text, status, created_at)"
                 " SELECT contact_id, template_id, kind, subject, body_text, 'draft', created_at FROM emails WHERE id = ?",
                 (email_id,))
    second = conn.execute("SELECT MAX(id) FROM emails").fetchone()[0]
    assert any("already has another email" in p.text for p in review.check_draft(conn, second).blocking)
    # 3. the database refuses two queued follow-ups for one contact, whatever the Python does
    conn.execute("UPDATE emails SET status = 'queued', test_mode = 0 WHERE id = ?", (email_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE emails SET status = 'queued', test_mode = 0 WHERE id = ?", (second,))


def test_a_rejected_follow_up_can_be_drafted_again(conn, first):
    email_id, _ = follow_up(conn, first["priya"])
    review.reject(conn, email_id)
    follow_up(conn, first["priya"])


# --- sending in the same thread ----------------------------------------------------------------

def test_a_real_follow_up_replies_to_the_first_emails_gmail_message(conn, first):
    email_id, _ = follow_up(conn, first["priya"])
    review.approve(conn, email_id)
    conn.execute("UPDATE templates SET test_checked_at = ?", (db.now(),))
    calls = []
    sending.send_real(conn, fake_w2(calls, batch_id=2), [email_id], confirmed=1, test_to=TEST_TO,
                      resume=PDF, resume_name="cv.pdf", now=A_MONDAY + timedelta(days=7))
    [email] = payload_of(calls[0])["emails"]
    assert email["reply_to_message_id"] == "m-priya@nimbuslabs.ai" and email["to"] == "priya@nimbuslabs.ai"
    assert email["subject"] == "Re: AI Engineer at Nimbus Labs?"


def test_a_follow_up_test_replies_in_my_self_test_thread(conn, first):
    email_id, _ = follow_up(conn, first["priya"])
    with pytest.raises(sending.SendError, match="send one real self-test email first"):
        sending.build_test_payload(conn, [email_id], TEST_TO, batch_id=9)
    conn.execute("INSERT INTO contacts (company, role, email, status, created_at) VALUES ('Self', 'AI Engineer', ?, 'contacted', ?)",
                 (TEST_TO, db.now()))
    self_id = conn.execute("SELECT id FROM contacts WHERE email = ?", (TEST_TO,)).fetchone()[0]
    conn.execute("INSERT INTO emails (contact_id, template_id, kind, subject, body_text, status, test_mode,"
                 " gmail_message_id, sent_at, created_at) VALUES (?, 1, 'initial', 's', 'b', 'replied', 0, 'm-self', ?, ?)",
                 (self_id, NOW.isoformat(), db.now()))
    [email] = sending.build_test_payload(conn, [email_id], TEST_TO, batch_id=9)["emails"]
    assert (email["to"], email["reply_to_message_id"]) == (TEST_TO, "m-self")


def test_sync_confirms_the_follow_up_went_out(conn, first):
    email_id, _ = follow_up(conn, first["priya"])
    review.approve(conn, email_id)
    conn.execute("UPDATE templates SET test_checked_at = ?", (db.now(),))
    queued_at = A_MONDAY + timedelta(days=7)
    sending.send_real(conn, fake_w2(batch_id=2), [email_id], confirmed=1, test_to=TEST_TO,
                      resume=PDF, resume_name="cv.pdf", now=queued_at)
    later = [{"message_id": "m-follow-up", "date": (queued_at + timedelta(minutes=1)).isoformat()}]
    report = sync(conn, gmail_thread("priya@nimbuslabs.ai", "AI Engineer at Nimbus Labs?", A_MONDAY, later_sent=later))
    row = conn.execute("SELECT status, gmail_message_id FROM emails WHERE id = ?", (email_id,)).fetchone()
    assert tuple(row) == ("sent", "m-follow-up") and "priya@nimbuslabs.ai (follow-up)" in report.newly_sent
    assert conn.execute("SELECT status FROM contacts WHERE email = 'priya@nimbuslabs.ai'").fetchone()[0] == "followed_up"
    assert followups.due(conn, NOW) == [r for r in followups.due(conn, NOW) if r["address"] != "priya@nimbuslabs.ai"]


def test_the_follow_up_template_is_tested_as_a_reply_in_my_self_test_thread(conn, first):
    with pytest.raises(sending.SendError, match="send one real self-test email first"):
        sending.send_follow_up_template_test(conn, fake_w2(), TEST_TO, PDF, "cv.pdf")
    conn.execute("INSERT INTO contacts (company, role, email, name, status, created_at)"
                 " VALUES ('Sarvam AI', 'AI Engineer', ?, '', 'replied', ?)", (TEST_TO, db.now()))
    self_id = conn.execute("SELECT id FROM contacts WHERE email = ?", (TEST_TO,)).fetchone()[0]
    conn.execute("INSERT INTO emails (contact_id, template_id, kind, subject, body_text, status, test_mode,"
                 " gmail_message_id, sent_at, created_at) VALUES (?, 1, 'initial', 'Can I take 0.35%?', 'b',"
                 " 'replied', 0, 'm-self', ?, ?)", (self_id, NOW.isoformat(), db.now()))
    template = followups.follow_up_template(conn)
    calls = []
    batch = sending.send_follow_up_template_test(conn, fake_w2(calls, batch_id=2), TEST_TO, PDF, "cv.pdf")
    [email] = payload_of(calls[0])["emails"]
    assert payload_of(calls[0])["test_mode"] is True
    assert (email["to"], email["reply_to_message_id"], email["subject"]) == (TEST_TO, "m-self", "Re: Can I take 0.35%?")
    assert email["body_text"].startswith("Hey,\n\nJust bringing my note about the AI Engineer role at Sarvam AI")
    assert batch.template_id == template.id
    sending.mark_test_checked(conn, template.id)                     # rule 2 for the Follow-up template
    with pytest.raises(sending.SendError, match="double click"):
        sending.send_follow_up_template_test(conn, fake_w2(batch_id=3), TEST_TO, PDF, "cv.pdf")
