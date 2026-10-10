"""Real batches: every rule that stands between an approved email and a recruiter. W2 is a fake."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from mailmate import db, review, sending
from mailmate.importer import Contact
from mailmate.n8n import N8nClient, N8nSettings
from mailmate.sending import IST, SendError, SendRules
from mailmate.sentence_check import PersonalLine
from mailmate.templates import save_template

PDF = b"%PDF-1.7\n" + b"x" * 1000
TEST_TO = "me.secondary@example.net"
MONDAY_10 = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
SATURDAY_10 = datetime(2026, 10, 10, 10, 0, tzinfo=IST)


@pytest.fixture
def setup(conn):
    db.set_settings(conn, my_name="Shrish", signature="Thanks,\nShrish")
    template = save_template(conn, "cold", "{role} at {company}?", "Hey {first_name},\n\n{company} rocks.\n\n{signature}")
    contacts = [Contact(company=f"Company {i}", role="AI Engineer", email=f"r{i}@nimbuslabs.ai") for i in range(6)]
    contacts.append(Contact(company="Self test", role="AI Engineer", email=TEST_TO))
    db.add_contacts(conn, contacts, "jobs.csv")
    ids = {}
    for c in conn.execute("SELECT id, email FROM contacts ORDER BY id").fetchall():
        email_id = review.create_draft(conn, c["id"], template, PersonalLine())
        review.approve(conn, email_id)
        ids[c["email"]] = email_id
    conn.execute("UPDATE templates SET test_checked_at = ? WHERE id = ?", (db.now(), template.id))
    return template, ids


def recruiters(setup, n):
    return [setup[1][f"r{i}@nimbuslabs.ai"] for i in range(n)]


def w2(*responses, calls=None):
    queue, calls = list(responses), ([] if calls is None else calls)

    def handler(request):
        calls.append(request)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return N8nClient(N8nSettings(base_url="https://demo.app.n8n.cloud", token="tok"),
                     transport=httpx.MockTransport(handler), sleep=lambda s: None)


def ok_for(batch_id, n):
    return httpx.Response(200, json={"accepted": n, "batch_id": batch_id})


def send(conn, ids, *, now=MONDAY_10, client=None, confirmed=None, calls=None):
    batch_id = (conn.execute("SELECT COALESCE(MAX(id), 0) FROM batches").fetchone()[0]) + 1
    client = client or w2(ok_for(batch_id, len(ids)), calls=calls)
    return sending.send_real(conn, client, ids, confirmed=len(ids) if confirmed is None else confirmed,
                             test_to=TEST_TO, resume=PDF, resume_name="cv.pdf", now=now)


def statuses(conn, ids):
    return [conn.execute("SELECT status FROM emails WHERE id = ?", (i,)).fetchone()[0] for i in ids]


def payload_of(request) -> dict:
    body = request.content.decode("utf-8", "replace")
    start = body.index('name="payload"') + len('name="payload"')
    return json.loads(body[start:].split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0])


# --- the happy path ---------------------------------------------------------------------------

def test_a_real_batch_goes_to_the_recruiters_and_queues_the_emails(conn, setup):
    ids, calls = recruiters(setup, 2), []
    batch = send(conn, ids, calls=calls)
    payload = payload_of(calls[0])
    assert payload["test_mode"] is False and payload["batch_id"] == batch.batch_id
    assert [e["to"] for e in payload["emails"]] == ["r0@nimbuslabs.ai", "r1@nimbuslabs.ai"]
    assert (payload["spacing_min_s"], payload["spacing_max_s"]) == (180, 420)
    assert payload["emails"][0]["body_html"].startswith("<p>Hey,</p>")
    rows = conn.execute("SELECT status, test_mode, batch_id, queued_at FROM emails WHERE id IN (?, ?)", ids).fetchall()
    assert all((r[0], r[1], r[2]) == ("queued", 0, batch.batch_id) and r[3] for r in rows)
    assert conn.execute("SELECT status FROM batches WHERE id = ?", (batch.batch_id,)).fetchone()[0] == "submitted"


# --- confirmation and approval ------------------------------------------------------------------

def test_the_typed_number_must_match(conn, setup):
    ids, calls = recruiters(setup, 2), []
    with pytest.raises(SendError, match=r"type the number of emails \(2\)"):
        send(conn, ids, confirmed=1, calls=calls)
    assert calls == [] and statuses(conn, ids) == ["approved", "approved"]


def test_only_approved_emails(conn, setup):
    [email_id] = recruiters(setup, 1)
    review.unapprove(conn, email_id)
    assert "is draft, not approved" in sending.plan_real_batch(conn, [email_id], MONDAY_10, TEST_TO).problems[0]


def test_the_template_needs_a_checked_test(conn, setup):
    conn.execute("UPDATE templates SET test_checked_at = NULL")
    problems = sending.plan_real_batch(conn, recruiters(setup, 1), MONDAY_10, TEST_TO).problems
    assert "no checked test email yet (rule 2)" in problems[0]


def test_an_address_suppressed_after_approval_is_refused(conn, setup):
    [email_id] = recruiters(setup, 1)
    db.suppress(conn, "r0@nimbuslabs.ai", "not_interested")
    problems = sending.plan_real_batch(conn, [email_id], MONDAY_10, TEST_TO).problems
    assert f"#{email_id}: r0@nimbuslabs.ai is on the do-not-contact list" in problems


# --- time rules ---------------------------------------------------------------------------------

def test_weekends_are_refused(conn, setup):
    problems = sending.plan_real_batch(conn, recruiters(setup, 1), SATURDAY_10, TEST_TO).problems
    assert problems == ["real emails go out Monday to Friday only (India time)"]


@pytest.mark.parametrize("hour, minute", [(9, 0), (18, 0), (22, 30)])
def test_outside_the_window_is_refused(conn, setup, hour, minute):
    now = MONDAY_10.replace(hour=hour, minute=minute)
    problems = sending.plan_real_batch(conn, recruiters(setup, 1), now, TEST_TO).problems
    assert problems[0].startswith("outside the sending window (09:30-18:00 IST)")


def test_the_whole_batch_must_finish_inside_the_window(conn, setup):
    now = MONDAY_10.replace(hour=17, minute=40)          # 20 min left, up to 7 min per gap
    problems = sending.plan_real_batch(conn, recruiters(setup, 5), now, TEST_TO).problems
    assert "could run until 18:08, past the window's end at 18:00; send at most 3 now" in problems[0]
    assert sending.plan_real_batch(conn, recruiters(setup, 3), now, TEST_TO).ok


def test_a_self_test_to_my_own_address_may_go_any_time(conn, setup):
    self_test = [setup[1][TEST_TO]]
    plan = sending.plan_real_batch(conn, self_test, SATURDAY_10.replace(hour=23), TEST_TO)
    assert plan.ok and plan.self_test
    mixed = sending.plan_real_batch(conn, self_test + recruiters(setup, 1), SATURDAY_10, TEST_TO)
    assert not mixed.ok and not mixed.self_test


# --- the daily cap can't be exceeded ------------------------------------------------------------

def test_the_daily_cap_cant_be_exceeded(conn, setup):
    sending.save_send_rules(conn, SendRules(daily_cap=3, spacing_min_s=180, spacing_max_s=420,
                                            window_start=sending.time(9, 30), window_end=sending.time(18, 0)))
    send(conn, recruiters(setup, 2))
    later = MONDAY_10 + timedelta(hours=1)                # the first batch is done by then
    problems = sending.plan_real_batch(conn, recruiters(setup, 4)[2:], later, TEST_TO).problems
    assert problems == ["daily cap: 2 already went out today and the cap is 3, so at most 1 more today"]
    with pytest.raises(SendError, match="daily cap"):
        send(conn, recruiters(setup, 4)[2:], now=later)
    send(conn, recruiters(setup, 3)[2:], now=later)       # exactly up to the cap is fine
    with pytest.raises(SendError, match="at most 0 more today"):
        send(conn, recruiters(setup, 4)[3:], now=later + timedelta(hours=1))
    assert sending.queued_today(conn, later) == 3


def test_the_cap_resets_the_next_day_in_india(conn, setup):
    sending.save_send_rules(conn, SendRules(daily_cap=1, spacing_min_s=180, spacing_max_s=420,
                                            window_start=sending.time(9, 30), window_end=sending.time(18, 0)))
    send(conn, recruiters(setup, 1))
    assert sending.plan_real_batch(conn, recruiters(setup, 2)[1:], MONDAY_10 + timedelta(days=1), TEST_TO).ok


def test_a_batch_can_never_exceed_the_cap_or_20(conn, setup):
    sending.save_send_rules(conn, SendRules(daily_cap=2, spacing_min_s=180, spacing_max_s=420,
                                            window_start=sending.time(9, 30), window_end=sending.time(18, 0)))
    assert sending.plan_real_batch(conn, recruiters(setup, 3), MONDAY_10, TEST_TO).problems == [
        "a real batch has 1 to 2 emails"]
    with pytest.raises(SendError, match="between 1 and 50"):
        sending.save_send_rules(conn, SendRules(daily_cap=0, spacing_min_s=180, spacing_max_s=420,
                                                window_start=sending.time(9, 30), window_end=sending.time(18, 0)))


def test_one_batch_at_a_time(conn, setup):
    send(conn, recruiters(setup, 2))
    problems = sending.plan_real_batch(conn, recruiters(setup, 3)[2:], MONDAY_10 + timedelta(minutes=5), TEST_TO).problems
    assert problems[0].startswith("batch #1 may still be sending until about 10:12")


# --- failures never send twice ------------------------------------------------------------------

def test_a_refused_batch_goes_back_to_approved(conn, setup):
    ids = recruiters(setup, 2)
    with pytest.raises(SendError, match="W2 refused the batch, nothing was sent"):
        send(conn, ids, client=w2(httpx.Response(500, text="Error in workflow")))
    assert statuses(conn, ids) == ["approved", "approved"]
    assert tuple(conn.execute("SELECT test_mode, queued_at FROM emails WHERE id = ?", (ids[0],)).fetchone()) == (1, None)
    assert sending.queued_today(conn, MONDAY_10) == 0
    send(conn, ids)                                         # and can be sent again


def test_an_unclear_failure_keeps_the_emails_queued_until_checked(conn, setup):
    ids = recruiters(setup, 2)
    with pytest.raises(SendError, match="stay 'queued' until you've checked"):
        send(conn, ids, client=w2(httpx.ReadTimeout("slow")))
    assert statuses(conn, ids) == ["queued", "queued"]
    with pytest.raises(SendError, match="confirm that nothing was sent"):
        sending.release_batch(conn, 1, nothing_sent_confirmed=False)
    assert sending.release_batch(conn, 1, nothing_sent_confirmed=True) == 2
    assert statuses(conn, ids) == ["approved", "approved"]


def test_a_submitted_batch_cant_be_released(conn, setup):
    batch = send(conn, recruiters(setup, 1))
    with pytest.raises(SendError, match="only a failed real batch"):
        sending.release_batch(conn, batch.batch_id, nothing_sent_confirmed=True)


def test_stopping_a_batch_keeps_its_emails_queued(conn, setup):
    ids = recruiters(setup, 2)
    batch = send(conn, ids)
    sending.mark_stopped(conn, batch.batch_id)
    assert conn.execute("SELECT status FROM batches WHERE id = ?", (batch.batch_id,)).fetchone()[0] == "stopped"
    assert statuses(conn, ids) == ["queued", "queued"]
    assert sending.running_batch(conn, MONDAY_10, sending.send_rules(conn)) is None


def test_a_queued_email_cant_be_queued_again(conn, setup):
    ids = recruiters(setup, 1)
    send(conn, ids)
    problems = sending.plan_real_batch(conn, ids, MONDAY_10 + timedelta(hours=1), TEST_TO).problems
    assert any("is queued, not approved" in p for p in problems)
