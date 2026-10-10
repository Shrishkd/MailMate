"""Applying W3's answer: matching threads, statuses, replies, bounces, do-not-contact. W3 is faked."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from mailmate import db, review, sending
from mailmate.importer import Contact
from mailmate.n8n import N8nClient, N8nError, N8nSettings, SyncResult
from mailmate.sending import IST
from mailmate.sentence_check import PersonalLine
from mailmate.sync import apply_sync
from mailmate.templates import save_template

PDF = b"%PDF-1.7\n" + b"x" * 100
TEST_TO = "me.secondary@example.net"
MONDAY_10 = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
SUBJECT = "AI Engineer at {company}?"


@pytest.fixture
def sent(conn):
    """Three recruiters and my own test address, all in one real batch queued at Monday 10:00 IST."""
    db.set_settings(conn, my_name="Shrish", signature="Thanks,\nShrish")
    template = save_template(conn, "cold", "{role} at {company}?", "Hey,\n\n{company} rocks.\n\n{signature}")
    people = [("Nimbus Labs", "priya@nimbuslabs.ai"), ("Quillstack", "neha@quillstack.io"),
              ("Deadcorp", "jobs@deadcorp.com"), ("Self test", TEST_TO)]
    db.add_contacts(conn, [Contact(company=c, role="AI Engineer", email=e) for c, e in people], "jobs.csv")
    ids = []
    for c in conn.execute("SELECT id FROM contacts ORDER BY id").fetchall():
        email_id = review.create_draft(conn, c["id"], template, PersonalLine())
        review.approve(conn, email_id)
        ids.append(email_id)
    conn.execute("UPDATE templates SET test_checked_at = ?", (db.now(),))
    client = N8nClient(N8nSettings(base_url="https://demo.app.n8n.cloud", token="tok"),
                       transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"accepted": 4, "batch_id": 1})))
    sending.send_real(conn, client, ids, confirmed=4, test_to=TEST_TO, resume=PDF, resume_name="cv.pdf",
                      now=MONDAY_10 - timedelta(days=7))   # a Monday a week earlier, inside the window
    conn.execute("UPDATE emails SET queued_at = ?", (MONDAY_10.isoformat(),))
    return {e: i for (c, e), i in zip(people, ids)}


def at(minutes: int) -> str:
    return (MONDAY_10 + timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def thread(to, company, minutes, *replies, thread_id=None):
    return {"thread_id": thread_id or f"t-{to}", "sent_message_id": f"m-{to}", "to": to,
            "subject": SUBJECT.format(company=company), "sent_at": at(minutes), "replies": list(replies)}


def reply(category, confidence=0.9, frm="someone@nimbuslabs.ai", minutes=60, snippet="..."):
    return {"message_id": f"r-{category}-{minutes}", "from": frm, "date": at(minutes), "snippet": snippet,
            "body_text": snippet, "category": category, "confidence": confidence}


def run(conn, *threads, now=None):
    return apply_sync(conn, SyncResult.model_validate({"threads": list(threads)}), TEST_TO,
                      now=now or MONDAY_10 + timedelta(hours=3))


def status(conn, email_id):
    row = conn.execute("SELECT e.status, c.status FROM emails e JOIN contacts c ON c.id = e.contact_id"
                       " WHERE e.id = ?", (email_id,)).fetchone()
    return tuple(row)


# --- matching and 'sent' ----------------------------------------------------------------------

def test_a_thread_marks_its_email_sent_and_stores_the_thread_id(conn, sent):
    report = run(conn, thread("priya@nimbuslabs.ai", "Nimbus Labs", 1))
    assert report.newly_sent == ["priya@nimbuslabs.ai"]
    row = conn.execute("SELECT status, gmail_thread_id, gmail_message_id, sent_at FROM emails WHERE id = ?",
                       (sent["priya@nimbuslabs.ai"],)).fetchone()
    assert tuple(row) == ("sent", "t-priya@nimbuslabs.ai", "m-priya@nimbuslabs.ai", at(1))
    assert status(conn, sent["priya@nimbuslabs.ai"]) == ("sent", "contacted")


def test_threads_sent_before_the_email_was_queued_are_not_matched(conn, sent):
    """Test copies to my own address carry the same subject, but went out earlier."""
    report = run(conn, thread(TEST_TO, "Self test", -90, reply("question", frm=TEST_TO), thread_id="old-test"))
    assert report.newly_sent == [] and len(report.test_replies) == 1
    assert status(conn, sent[TEST_TO])[0] == "queued"


def test_a_different_subject_is_not_matched(conn, sent):
    t = thread("priya@nimbuslabs.ai", "Nimbus Labs", 1)
    t["subject"] = "Something else"
    report = run(conn, t)
    assert report.newly_sent == [] and report.unknown_threads == ["priya@nimbuslabs.ai: Something else"]


def test_running_sync_twice_changes_nothing_the_second_time(conn, sent):
    t = thread("priya@nimbuslabs.ai", "Nimbus Labs", 1, reply("interview_request"))
    first = run(conn, t)
    second = run(conn, t)
    assert len(first.new_replies) == 1 and second.new_replies == [] and second.newly_sent == []
    assert conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 1


# --- replies --------------------------------------------------------------------------------------

@pytest.mark.parametrize("category", ["interview_request", "question", "other"])
def test_a_reply_marks_email_and_contact_replied(conn, sent, category):
    report = run(conn, thread("priya@nimbuslabs.ai", "Nimbus Labs", 1, reply(category)))
    assert status(conn, sent["priya@nimbuslabs.ai"]) == ("replied", "replied")
    assert [n.category for n in report.new_replies] == [category] and report.suppressed == []


def test_an_auto_reply_changes_nothing(conn, sent):
    report = run(conn, thread("priya@nimbuslabs.ai", "Nimbus Labs", 1, reply("auto_reply")))
    assert status(conn, sent["priya@nimbuslabs.ai"]) == ("sent", "contacted")
    assert report.new_replies[0].category == "auto_reply"


def test_not_interested_goes_on_the_do_not_contact_list(conn, sent):
    report = run(conn, thread("neha@quillstack.io", "Quillstack", 1, reply("not_interested", 0.95, "neha@quillstack.io")))
    assert report.suppressed == ["neha@quillstack.io"]
    assert db.suppressed(conn)["neha@quillstack.io"] == "not_interested"
    assert status(conn, sent["neha@quillstack.io"]) == ("replied", "replied")


def test_an_unsure_not_interested_is_flagged_not_suppressed(conn, sent):
    report = run(conn, thread("neha@quillstack.io", "Quillstack", 1, reply("not_interested", 0.4)))
    assert report.suppressed == [] and report.new_replies[0].flagged
    assert "neha@quillstack.io" not in db.suppressed(conn)


def test_a_bounce_marks_bounced_and_suppresses(conn, sent):
    report = run(conn, thread("jobs@deadcorp.com", "Deadcorp", 1, reply("bounce", 1, "mailer-daemon@googlemail.com", 2)))
    assert status(conn, sent["jobs@deadcorp.com"]) == ("bounced", "bounced")
    assert db.suppressed(conn)["jobs@deadcorp.com"] == "bounce" and report.suppressed == ["jobs@deadcorp.com"]


def test_a_later_reply_never_undoes_a_bounce(conn, sent):
    run(conn, thread("jobs@deadcorp.com", "Deadcorp", 1, reply("bounce", 1, "mailer-daemon@googlemail.com", 2)))
    run(conn, thread("jobs@deadcorp.com", "Deadcorp", 1, reply("question", minutes=90)))
    assert status(conn, sent["jobs@deadcorp.com"]) == ("bounced", "bounced")


def test_my_own_test_address_is_never_suppressed(conn, sent):
    report = run(conn, thread(TEST_TO, "Self test", 1, reply("not_interested", 0.99, TEST_TO)))
    assert report.suppressed == [] and TEST_TO not in db.suppressed(conn)
    assert status(conn, sent[TEST_TO]) == ("replied", "replied")
    event = conn.execute("SELECT detail FROM events WHERE kind = 'suppression_skipped'").fetchone()[0]
    assert json.loads(event)["email"] == TEST_TO


def test_replies_to_test_sends_change_nothing(conn, sent):
    report = run(conn, thread(TEST_TO, "Self test", -300, reply("not_interested", 0.99, TEST_TO), thread_id="test-copy"))
    assert report.test_replies[0].category == "not_interested" and report.test_replies[0].test
    assert conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 0
    assert report.suppressed == []


# --- batches and lost emails ----------------------------------------------------------------------

def test_a_batch_is_done_when_none_of_its_emails_is_queued(conn, sent):
    threads = [thread(e, c, i + 1) for i, (c, e) in enumerate([("Nimbus Labs", "priya@nimbuslabs.ai"),
               ("Quillstack", "neha@quillstack.io"), ("Deadcorp", "jobs@deadcorp.com"), ("Self test", TEST_TO)])]
    assert run(conn, *threads[:3]).batches_done == []
    assert run(conn, *threads).batches_done == [1]
    assert conn.execute("SELECT status FROM batches WHERE id = 1").fetchone()[0] == "done"


def test_an_email_missing_from_gmail_long_after_its_batch_is_flagged(conn, sent):
    conn.execute("UPDATE batches SET submitted_at = ?", (MONDAY_10.isoformat(),))
    soon = run(conn, thread("priya@nimbuslabs.ai", "Nimbus Labs", 1), now=MONDAY_10 + timedelta(minutes=30))
    assert soon.not_found == []
    late = run(conn, thread("priya@nimbuslabs.ai", "Nimbus Labs", 1), now=MONDAY_10 + timedelta(hours=3))
    assert sorted(late.not_found) == sorted(["neha@quillstack.io", "jobs@deadcorp.com", TEST_TO])


# --- the W3 call ----------------------------------------------------------------------------------

def test_the_sync_call_parses_w3s_answer_and_retries_blips():
    calls = []
    answer = {"threads": [thread("priya@nimbuslabs.ai", "Nimbus Labs", 1, reply("question"))]}
    responses = [httpx.ConnectError("blip"), httpx.Response(200, json=answer)]

    def handler(request):
        calls.append(request)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    client = N8nClient(N8nSettings(base_url="https://demo.app.n8n.cloud", token="tok"),
                       transport=httpx.MockTransport(handler), sleep=lambda s: None)
    result = client.sync(14)
    assert json.loads(calls[-1].content) == {"since_days": 14} and str(calls[-1].url).endswith("/webhook/MailMate-sync")
    assert result.threads[0].replies[0].from_ == "someone@nimbuslabs.ai"


def test_reply_text_is_unescaped_and_loses_the_quoted_original():
    raw = reply("question", snippet="What&#39;s your notice period? On Sat, Oct 10, 2026 at 10:30 AM Shrish wrote: Hey")
    result = SyncResult.model_validate({"threads": [thread("priya@nimbuslabs.ai", "Nimbus Labs", 1, raw)]})
    r = result.threads[0].replies[0]
    assert r.snippet == r.body_text == "What's your notice period?"


def test_a_malformed_w3_answer_is_an_error():
    client = N8nClient(N8nSettings(base_url="https://demo.app.n8n.cloud", token="tok"),
                       transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"threads": [{"to": "x"}]})))
    with pytest.raises(N8nError, match="W3 answered in an unexpected shape"):
        client.sync()
