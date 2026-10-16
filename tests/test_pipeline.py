"""The pipeline after a reply: who enters it, stages, the furthest stage reached, what's coming up."""

from datetime import datetime, timedelta, timezone

import pytest

from mailmate import pipeline
from mailmate.companies import company_key
from mailmate.pipeline import PipelineError
from tests.test_sync import TEST_TO, reply, run, sent, thread  # noqa: F401  (sent is a fixture)

NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)


def test_a_sync_puts_interview_requests_and_questions_in_the_pipeline(conn, sent):
    report = run(conn,
                 thread("priya@nimbuslabs.ai", "Nimbus Labs", 1, reply("interview_request")),
                 thread("neha@quillstack.io", "Quillstack", 2, reply("auto_reply", frm="neha@quillstack.io")),
                 thread("jobs@deadcorp.com", "Deadcorp", 3, reply("not_interested", frm="jobs@deadcorp.com")),
                 thread(TEST_TO, "Self test", 4, reply("question", frm=TEST_TO)))
    assert report.pipeline_added == ["Nimbus Labs"]
    entry = pipeline.entries(conn)[0]
    assert (entry["company"], entry["stage"], entry["reached"], entry["email"]) == \
        ("Nimbus Labs", "talking", 1, "priya@nimbuslabs.ai")
    assert entry["notes"].startswith("interview request on 2026-10-12")
    assert run(conn, thread("priya@nimbuslabs.ai", "Nimbus Labs", 1, reply("question", minutes=90))).pipeline_added == []


def test_adding_by_hand_once(conn):
    assert pipeline.add(conn, "Acme Pvt. Ltd.", note="call via a friend")
    assert not pipeline.add(conn, "acme")                              # same company, written differently
    with pytest.raises(PipelineError):
        pipeline.add(conn, "   ")


def test_stages_move_and_the_furthest_one_is_kept(conn):
    pipeline.add(conn, "Acme")
    pipeline.update(conn, "acme", stage="interviewing", next_step="System design round",
                    next_at=NOW + timedelta(days=2), notes="Ask about the team size")
    pipeline.update(conn, "acme", stage="rejected", next_at=NOW + timedelta(days=9))
    row = pipeline.entries(conn)[0]
    assert (row["stage"], row["reached"], row["next_at"]) == ("rejected", 3, None)   # a closed stage has no next date
    changes = conn.execute("SELECT detail FROM events WHERE kind = 'pipeline_stage' ORDER BY id").fetchall()
    assert [c[0] for c in changes] == ['{"company_key": "acme", "stage_from": "talking", "stage_to": "interviewing"}',
                                       '{"company_key": "acme", "stage_from": "interviewing", "stage_to": "rejected"}']
    assert pipeline.reached_counts(conn) == {"talking": 1, "screening": 1, "interviewing": 1, "offer": 0}
    with pytest.raises(PipelineError):
        pipeline.update(conn, "acme", stage="hired")
    with pytest.raises(PipelineError):
        pipeline.update(conn, "nobody", stage="talking")


def test_whats_coming_up_and_what_needs_an_update(conn):
    for name, hours in (("Soon", 5), ("Later", 24 * 10), ("Just now", -1), ("Past", -30)):
        pipeline.add(conn, name)
        pipeline.update(conn, company_key(name), stage="screening", next_at=NOW + timedelta(hours=hours))
    assert [r["company"] for r in pipeline.upcoming(conn, NOW)] == ["Just now", "Soon"]
    assert [r["company"] for r in pipeline.stale(conn, NOW)] == ["Past"]
