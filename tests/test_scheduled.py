"""Scheduled batches: approve in the evening, W2 holds the batch until the next morning. Every rule is
checked as of the start time; one batch at a time includes a scheduled one; a batch that never went out
can be released. W2 is a fake."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from mailmate import db, sending
from mailmate.sending import IST
from tests.test_real_sending import PDF, TEST_TO, ok_for, payload_of, recruiters, setup, statuses, w2  # noqa: F401

MONDAY_20 = datetime(2026, 10, 12, 20, 0, tzinfo=IST)
TUESDAY_10 = datetime(2026, 10, 13, 10, 0, tzinfo=IST)


def schedule(conn, ids, start=TUESDAY_10, now=MONDAY_20, calls=None):
    batch_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM batches").fetchone()[0] + 1
    return sending.send_real(conn, w2(ok_for(batch_id, len(ids)), calls=calls), ids, confirmed=len(ids),
                             test_to=TEST_TO, resume=PDF, resume_name="cv.pdf", now=now, start_at=start)


def synced(conn, at: datetime) -> None:
    conn.execute("INSERT INTO events (kind, created_at) VALUES ('sync_done', ?)",
                 (at.astimezone(timezone.utc).isoformat(),))


@pytest.mark.parametrize("now, expected", [
    (MONDAY_20, datetime(2026, 10, 13, 9, 30, tzinfo=IST)),                               # tomorrow morning
    (datetime(2026, 10, 12, 8, 0, tzinfo=IST), datetime(2026, 10, 12, 9, 30, tzinfo=IST)),  # later today
    (datetime(2026, 10, 16, 19, 0, tzinfo=IST), datetime(2026, 10, 19, 9, 30, tzinfo=IST)),  # Friday -> Monday
])
def test_the_next_window(conn, now, expected):
    assert sending.next_window_start(now, sending.send_rules(conn)) == expected


FRIDAY_20 = datetime(2026, 10, 16, 20, 0, tzinfo=IST)


@pytest.mark.parametrize("now, start, problem", [
    (MONDAY_20, TUESDAY_10, None),
    (FRIDAY_20, datetime(2026, 10, 19, 9, 45, tzinfo=IST), None),                       # Friday night for Monday
    (MONDAY_20, datetime(2026, 10, 13, 8, 0, tzinfo=IST),
     "outside the sending window (09:30-18:00 IST); the start is 08:00"),
    (FRIDAY_20, datetime(2026, 10, 17, 10, 0, tzinfo=IST), "real emails go out Monday to Friday only (India time)"),
    (MONDAY_20, MONDAY_20 + timedelta(days=5), "a batch can be scheduled from a minute to 4 days ahead"),
    (MONDAY_20, MONDAY_20 - timedelta(hours=1), "a batch can be scheduled from a minute to 4 days ahead"),
])
def test_the_rules_are_checked_for_the_start_time(conn, setup, now, start, problem):
    problems = sending.plan_real_batch(conn, recruiters(setup, 2), now, TEST_TO, start_at=start).problems
    assert problems == ([problem] if problem else [])


def test_a_scheduled_batch_is_handed_over_with_its_start_and_holds_the_queue(conn, setup):
    calls = []
    ids = recruiters(setup, 2)
    batch = schedule(conn, ids, calls=calls)
    assert payload_of(calls[0])["start_at"] == "2026-10-13T04:30:00+00:00"        # 10:00 IST
    assert batch.finishes_by == TUESDAY_10 + timedelta(minutes=7)
    row = conn.execute("SELECT status, scheduled_for FROM batches WHERE id = ?", (batch.batch_id,)).fetchone()
    assert tuple(row) == ("submitted", "2026-10-13T04:30:00+00:00")
    assert statuses(conn, ids) == ["queued", "queued"]
    # the daily cap counts them on Tuesday, not Monday
    assert sending.queued_today(conn, MONDAY_20) == 0 and sending.queued_today(conn, TUESDAY_10) == 2
    # one batch at a time: nothing else goes until it's done, and it isn't 'unconfirmed' before its time
    assert sending.capacity_now(conn, TUESDAY_10 + timedelta(minutes=5)) == \
        (0, f"batch #{batch.batch_id} may still be sending until about 10:12")
    later_tonight = MONDAY_20 + timedelta(minutes=30)
    assert sending.busy(conn, later_tonight, sending.send_rules(conn)) == \
        f"batch #{batch.batch_id} is scheduled for Tue 13 Oct 10:00 IST"
    third = recruiters(setup, 3)[2:]
    overlapping = sending.plan_real_batch(conn, third, later_tonight, TEST_TO, start_at=TUESDAY_10 + timedelta(minutes=5))
    assert overlapping.problems == [f"batch #{batch.batch_id} is scheduled for Tue 13 Oct 10:00 IST (until about 10:12); "
                                    "batches don't overlap"]
    after_it = sending.plan_real_batch(conn, third, later_tonight, TEST_TO, start_at=TUESDAY_10 + timedelta(hours=2))
    assert after_it.problems == []                                    # starts after the first one is done
    assert sending.unconfirmed(conn, later_tonight) == (0, [])


def test_a_scheduled_batch_never_carries_plain_emails_so_an_old_w2_refuses_it(conn, setup):
    calls = []
    schedule(conn, recruiters(setup, 1), calls=calls)
    payload = payload_of(calls[0])
    assert "emails" not in payload and [e["to"] for e in payload["scheduled_emails"]] == ["r0@nimbuslabs.ai"]
    old_w2 = w2(httpx.Response(500, json={"code": 0, "message": "No emails in the batch"}))
    with pytest.raises(sending.SendError, match="doesn't know scheduled batches yet: import n8n/workflows"):
        sending.send_real(conn, old_w2, recruiters(setup, 2)[1:], confirmed=1, test_to=TEST_TO, resume=PDF,
                          resume_name="cv.pdf", now=MONDAY_20, start_at=TUESDAY_10 + timedelta(hours=1))
    assert statuses(conn, recruiters(setup, 2)[1:]) == ["approved"]           # refused: nothing queued


def test_sending_now_is_fine_while_a_batch_waits_for_tomorrow(conn, setup):
    schedule(conn, recruiters(setup, 2), start=TUESDAY_10, now=MONDAY_20 - timedelta(hours=6))   # done Monday 14:00
    monday_15 = MONDAY_20 - timedelta(hours=5)
    assert sending.capacity_now(conn, monday_15)[0] > 0
    assert sending.plan_real_batch(conn, recruiters(setup, 3)[2:], monday_15, TEST_TO).problems == []


def test_a_batch_stopped_before_its_time_can_be_released(conn, setup):
    ids = recruiters(setup, 2)
    batch = schedule(conn, ids)
    assert sending.releasable_batches(conn, MONDAY_20 + timedelta(minutes=5)) == []
    conn.execute("INSERT INTO events (kind, batch_id, created_at) VALUES ('real_batch_stopped', ?, ?)",
                 (batch.batch_id, (MONDAY_20 + timedelta(minutes=10)).astimezone(timezone.utc).isoformat()))
    conn.execute("UPDATE batches SET status = 'stopped' WHERE id = ?", (batch.batch_id,))
    assert [b["id"] for b in sending.releasable_batches(conn, MONDAY_20 + timedelta(minutes=11))] == [batch.batch_id]
    assert sending.release_batch(conn, batch.batch_id, nothing_sent_confirmed=True,
                                 now=MONDAY_20 + timedelta(minutes=11)) == 2
    assert statuses(conn, ids) == ["approved", "approved"]


def test_a_batch_that_never_went_out_is_releasable_only_after_a_sync(conn, setup):
    batch = schedule(conn, recruiters(setup, 2))
    wednesday = TUESDAY_10 + timedelta(days=1)
    assert sending.releasable_batches(conn, wednesday) == []          # no sync since: maybe it did go out
    synced(conn, wednesday)
    assert [b["id"] for b in sending.releasable_batches(conn, wednesday)] == [batch.batch_id]
    conn.execute("UPDATE emails SET status = 'sent' WHERE id = ?", (recruiters(setup, 1)[0],))
    assert sending.releasable_batches(conn, wednesday) == []          # one went out: nothing to release
    with pytest.raises(sending.SendError):
        sending.release_batch(conn, batch.batch_id, nothing_sent_confirmed=True, now=wednesday)
    assert db.now()
