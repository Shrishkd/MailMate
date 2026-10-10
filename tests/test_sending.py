"""Test batches to W2, the resume file, and the no-double-send retry rule. W2 is a fake transport."""

import json

import httpx
import pytest

from mailmate import db, review, sending
from mailmate.config import ConfigError, N8nSettings
from mailmate.config import test_address as read_test_address
from mailmate.importer import Contact
from mailmate.n8n import N8nClient, N8nError
from mailmate.sending import SendError
from mailmate.sentence_check import PersonalLine
from mailmate.templates import save_template

PDF = b"%PDF-1.7\n" + b"x" * 1000
TEST_TO = "me.secondary@example.net"
SETTINGS = N8nSettings(base_url="https://demo.app.n8n.cloud", token="tok")


@pytest.fixture
def emails(conn):
    db.set_settings(conn, my_name="Shrish", signature="Thanks,\nShrish")
    template = save_template(conn, "cold", "{role} at {company}?", "Hey {first_name},\n\n{company} rocks.\n\n{signature}")
    db.add_contacts(conn, [Contact(company="Nimbus Labs", role="AI Engineer", email="priya@nimbuslabs.ai", name="Priya"),
                           Contact(company="Quillstack", role="AI Engineer", email="neha@quillstack.io")], "jobs.csv")
    ids = [r["id"] for r in conn.execute("SELECT id FROM contacts ORDER BY id")]
    return [review.create_draft(conn, i, template, PersonalLine()) for i in ids]


def w2(*responses, calls=None):
    queue, calls = list(responses), ([] if calls is None else calls)

    def handler(request):
        calls.append(request)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return N8nClient(SETTINGS, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def accepted(n, batch_id=1):
    return httpx.Response(200, json={"accepted": n, "batch_id": batch_id})


# --- payload ----------------------------------------------------------------------------------

def test_test_payload_sends_the_exact_text_to_the_test_address_only(conn, emails):
    payload = sending.build_test_payload(conn, emails, TEST_TO, batch_id=7)
    assert payload["test_mode"] is True and payload["batch_id"] == 7 and payload["sender_name"] == "Shrish"
    assert [e["to"] for e in payload["emails"]] == [TEST_TO, TEST_TO]
    first = conn.execute("SELECT subject, body_text FROM emails WHERE id = ?", (emails[0],)).fetchone()
    assert (payload["emails"][0]["subject"], payload["emails"][0]["body_text"]) == tuple(first)
    assert (payload["spacing_min_s"], payload["spacing_max_s"]) == sending.TEST_SPACING_S


def test_each_email_also_goes_as_simple_html(conn, emails):
    """Plain text gets hard-wrapped at ~76 characters on the way, so Gmail shows half-width lines;
    the HTML version uses the full width. Same words, paragraphs kept."""
    [email] = sending.build_test_payload(conn, emails[:1], TEST_TO, batch_id=1)["emails"]
    assert email["body_html"].startswith("<p>Hey Priya,</p>")
    assert "<p>Nimbus Labs rocks.</p>" in email["body_html"] and "Thanks,<br>" in email["body_html"]


@pytest.mark.parametrize("pick, problem", [
    (lambda ids: [], "1 to 3 emails"),
    (lambda ids: ids * 2, "1 to 3 emails"),
    (lambda ids: [999], "only drafts and approved"),
])
def test_test_payload_limits(conn, emails, pick, problem):
    with pytest.raises(SendError, match=problem):
        sending.build_test_payload(conn, pick(emails), TEST_TO, batch_id=1)


def test_one_template_version_per_test_batch(conn, emails):
    newer = save_template(conn, "cold", "{role} at {company}!", "Hi,\n\n{company}.\n\n{signature}")
    review.rebuild_with_template(conn, emails[1], newer)
    with pytest.raises(SendError, match="one template version"):
        sending.build_test_payload(conn, emails, TEST_TO, batch_id=1)


# --- sending to W2 ------------------------------------------------------------------------------

def test_send_test_posts_multipart_with_the_resume(conn, emails):
    calls = []
    batch = sending.send_test(conn, w2(accepted(2), calls=calls), emails, TEST_TO, PDF, "Shrish_CV.pdf")
    [request] = calls
    assert str(request.url).endswith("/webhook/MailMate-send") and request.headers["X-MailMate-Token"] == "tok"
    body = request.content
    assert b'name="resume"; filename="Shrish_CV.pdf"' in body and PDF in body
    assert b'name="payload"' in body and TEST_TO.encode() in body
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch.batch_id,)).fetchone()
    assert (row["test_mode"], row["status"]) == (1, "submitted")
    statuses = {r[0] for r in conn.execute("SELECT status FROM emails")}
    assert statuses == {"draft"}, "a test must not change the emails it copies"


def test_a_failed_send_marks_the_batch_failed(conn, emails):
    with pytest.raises(N8nError):
        sending.send_test(conn, w2(httpx.Response(500, text="Test mode may only send to ...")), emails, TEST_TO,
                          PDF, "cv.pdf")
    assert conn.execute("SELECT status FROM batches").fetchone()[0] == "failed"


def test_a_send_that_may_have_reached_n8n_is_never_retried(conn, emails):
    calls = []
    with pytest.raises(N8nError, match="check n8n -> Executions before trying again"):
        sending.send_test(conn, w2(httpx.ReadTimeout("slow"), accepted(2), calls=calls), emails, TEST_TO, PDF, "cv.pdf")
    assert len(calls) == 1


def test_a_gateway_error_on_send_is_not_retried(conn, emails):
    calls = []
    with pytest.raises(N8nError, match="may have started the workflow"):
        sending.send_test(conn, w2(httpx.Response(502), accepted(2), calls=calls), emails, TEST_TO, PDF, "cv.pdf")
    assert len(calls) == 1


def test_a_send_that_never_connected_is_retried(conn, emails):
    calls = []
    sending.send_test(conn, w2(httpx.ConnectError("no route"), accepted(2), calls=calls), emails, TEST_TO, PDF, "cv.pdf")
    assert len(calls) == 2


def test_w2_must_confirm_the_whole_batch(conn, emails):
    with pytest.raises(N8nError, match="W2 accepted 1 email"):
        sending.send_test(conn, w2(accepted(1)), emails, TEST_TO, PDF, "cv.pdf")


def test_the_same_test_twice_in_a_row_is_refused(conn, emails, monkeypatch):
    """Clicking Send again while the first click is still running must not send twice."""
    calls = []
    sending.send_test(conn, w2(accepted(1), accepted(1), calls=calls), emails[:1], TEST_TO, PDF, "cv.pdf")
    with pytest.raises(SendError, match="this exact test went out 0 s ago"):
        sending.send_test(conn, w2(accepted(1), calls=calls), emails[:1], TEST_TO, PDF, "cv.pdf")
    assert len(calls) == 1
    sending.send_test(conn, w2(accepted(2, batch_id=2), calls=calls), emails, TEST_TO, PDF, "cv.pdf")   # a different test is fine
    monkeypatch.setattr(sending, "REPEAT_COOLDOWN_S", 0)     # after the cooldown it may go again
    sending.send_test(conn, w2(accepted(1, batch_id=3), calls=calls), emails[:1], TEST_TO, PDF, "cv.pdf")
    assert len(calls) == 3


# --- resume ---------------------------------------------------------------------------------

def test_resume_must_be_a_pdf(conn, tmp_path):
    with pytest.raises(SendError, match="isn't a PDF"):
        sending.save_resume(conn, b"PK\x03\x04 docx", tmp_path / "r.pdf", "cv.docx")


def test_resume_size_limits(conn, tmp_path):
    assert sending.save_resume(conn, PDF, tmp_path / "r.pdf", "cv.pdf") == []
    assert (tmp_path / "r.pdf").read_bytes() == PDF and db.get_settings(conn)["resume_name"] == "cv.pdf"
    assert "under 1 MB is safer" in sending.save_resume(conn, PDF + b"x" * 1024 * 1024, tmp_path / "r.pdf", "cv.pdf")[0]
    with pytest.raises(SendError, match="5 MB at most"):
        sending.check_resume(PDF + b"x" * 5 * 1024 * 1024)


# --- rule 2 mark ----------------------------------------------------------------------------

def test_a_template_is_test_checked_only_after_a_submitted_test(conn, emails):
    template_id = conn.execute("SELECT template_id FROM emails WHERE id = ?", (emails[0],)).fetchone()[0]
    with pytest.raises(SendError, match="send a test"):
        sending.mark_test_checked(conn, template_id)
    sending.send_test(conn, w2(accepted(1)), emails[:1], TEST_TO, PDF, "cv.pdf")
    sending.mark_test_checked(conn, template_id)
    assert conn.execute("SELECT test_checked_at FROM templates WHERE id = ?", (template_id,)).fetchone()[0]


def test_test_address_comes_from_env():
    assert read_test_address({"MAILMATE_TEST_ADDRESS": " Me@Example.NET "}) == "me@example.net"
    with pytest.raises(ConfigError, match="MAILMATE_TEST_ADDRESS"):
        read_test_address({})
