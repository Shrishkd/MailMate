"""The reply desk: what still needs an answer, in what order, when it's overdue; W4's two calls."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from mailmate import db, replydesk
from mailmate.n8n import N8nClient, N8nError, N8nSettings

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
TEST_TO = "me.secondary@example.net"


@pytest.fixture
def desk(conn):
    """Three conversations: Acme asked for an interview 14 h ago (and wrote again 2 h ago), Beta asked a
    question 3 h ago, Gamma sent an auto-reply. Plus a reply to my own test address."""
    conn.execute("INSERT INTO templates (name, version, subject, body, created_at) VALUES ('t', 1, 's', 'b', ?)",
                 (db.now(),))

    def email(company, address, name="Priya Sharma"):
        conn.execute("INSERT INTO contacts (company, role, email, name, status, created_at) VALUES (?, 'AI Engineer',"
                     " ?, ?, 'replied', ?)", (company, address, name, db.now()))
        contact = conn.execute("SELECT MAX(id) FROM contacts").fetchone()[0]
        conn.execute("INSERT INTO emails (contact_id, template_id, subject, body_text, status, test_mode, created_at)"
                     " VALUES (?, 1, 'AI Engineer?', 'My email to them', 'replied', 0, ?)", (contact, db.now()))
        return conn.execute("SELECT MAX(id) FROM emails").fetchone()[0]

    def reply(email_id, thread, category, hours_ago, body="Can we talk on Friday?"):
        conn.execute("INSERT INTO replies (email_id, gmail_thread_id, from_addr, date, snippet, body_text, category,"
                     " created_at) VALUES (?, ?, 'x@y', ?, ?, ?, ?, ?)",
                     (email_id, thread, (NOW - timedelta(hours=hours_ago)).isoformat(), body[:40], body, category,
                      db.now()))
        return conn.execute("SELECT MAX(id) FROM replies").fetchone()[0]

    acme = email("Acme", "priya@acme.example")
    ids = {"acme_first": reply(acme, "t-acme", "interview_request", 14),
           "acme_again": reply(acme, "t-acme", "other", 2, "Any update?"),
           "beta": reply(email("Beta", "raj@beta.example", "Raj"), "t-beta", "question", 3),
           "gamma": reply(email("Gamma", "hr@gamma.example", "HR Team"), "t-gamma", "auto_reply", 1),
           "self": reply(email("Self", TEST_TO, "Me"), "t-self", "interview_request", 1)}
    return ids


def test_one_entry_per_conversation_interview_requests_first(conn, desk):
    rows = replydesk.open_replies(conn, TEST_TO)
    assert [r["id"] for r in rows] == [desk["beta"], desk["acme_again"]]   # Acme's latest is 'other' now
    conn.execute("UPDATE replies SET category = 'interview_request' WHERE id = ?", (desk["acme_again"],))
    assert [r["id"] for r in replydesk.open_replies(conn, TEST_TO)] == [desk["acme_again"], desk["beta"]]


def test_overdue_after_12_hours_for_an_interview_request_a_day_for_a_question(conn, desk):
    first = conn.execute("SELECT * FROM replies WHERE id = ?", (desk["acme_first"],)).fetchone()
    beta = conn.execute("SELECT * FROM replies WHERE id = ?", (desk["beta"],)).fetchone()
    assert replydesk.overdue(first, NOW) and replydesk.age(first, NOW) == "14 h ago"
    assert not replydesk.overdue(beta, NOW)
    assert replydesk.overdue(beta, NOW + timedelta(hours=22))


def test_answering_the_latest_answers_the_earlier_ones_too(conn, desk):
    replydesk.mark(conn, desk["acme_again"], "answered")
    handled = dict(conn.execute("SELECT id, handled FROM replies WHERE gmail_thread_id = 't-acme'").fetchall())
    assert handled == {desk["acme_first"]: "answered", desk["acme_again"]: "answered"}
    assert [r["id"] for r in replydesk.open_replies(conn, TEST_TO)] == [desk["beta"]]
    replydesk.reopen(conn, desk["acme_again"])
    assert desk["acme_again"] in [r["id"] for r in replydesk.open_replies(conn, TEST_TO)]
    with pytest.raises(ValueError):
        replydesk.mark(conn, desk["beta"], "ignored")


def test_the_request_to_w4_has_what_it_needs(conn, desk):
    row = next(r for r in replydesk.open_replies(conn, TEST_TO) if r["id"] == desk["beta"])
    request = replydesk.write_request(row, {"my_name": "Asha Rao"}, notes=" free Tue 3-5 pm ")
    assert request == {"their_message": "Can we talk on Friday?", "their_name": "Raj", "company": "Beta",
                       "role": "AI Engineer", "my_name": "Asha Rao", "my_email": "My email to them",
                       "notes": "free Tue 3-5 pm"}


@pytest.mark.parametrize("text, problem", [
    ("", "empty"), ("Hi, I'm free [two times].", "[two times]"), ("Hi {first_name}", "'{'"), ("x" * 6001, "longer"),
])
def test_an_answer_with_gaps_cant_be_saved(text, problem):
    assert any(problem in p for p in replydesk.check_answer(text))
    assert replydesk.check_answer("Hi Raj,\nTuesday 3 pm works.\n\nBest regards,\nAsha") == []


# --- W4 calls ------------------------------------------------------------------------------------

def client(answer, seen: list | None = None, status=200):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        assert request.url.path == "/webhook/MailMate-reply"
        return httpx.Response(status, json=answer)
    return N8nClient(N8nSettings(base_url="https://demo.app.n8n.cloud", token="tok"),
                     transport=httpx.MockTransport(handle), retry_delays=())


def test_write_reply_sends_the_request_and_returns_the_text():
    seen = []
    text = client({"text": "Hi Raj, ...", "model": "gpt-oss:120b"}, seen).write_reply({"their_message": "hi"})
    assert text.text == "Hi Raj, ..." and seen == [{"action": "write", "their_message": "hi"}]


def test_save_reply_draft_returns_the_draft_and_w4_errors_are_explained():
    seen = []
    draft = client({"draft_id": "r-123", "to": "raj@beta.example"}, seen).save_reply_draft("18f2a9", "Hi Raj")
    assert draft.draft_id == "r-123" and seen == [{"action": "draft", "thread_id": "18f2a9", "text": "Hi Raj"}]
    with pytest.raises(N8nError, match="W4: Gmail did not save the draft"):
        client({"error": "Gmail did not save the draft: quota"}).save_reply_draft("18f2a9", "Hi")
    with pytest.raises(N8nError, match="Drafts before saving again"):
        client({}, status=502).save_reply_draft("18f2a9", "Hi")
