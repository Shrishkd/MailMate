"""Dashboard numbers against a database whose contents are known row by row (hand-counted)."""

from datetime import date

import pytest

from mailmate import db, stats
from mailmate.templates import save_template

TEST_TO = "me.secondary@example.net"


@pytest.fixture
def known(conn):
    """
    contact      template  first email  replies                                  follow-up
    ----------   --------  -----------  ---------------------------------------  ---------------------
    a  Alpha     v1        sent         interview_request (12 Oct)               -
    b  Beta      v1        sent         auto_reply, then question (13 Oct)       -
    c  Gamma     v1        bounced      bounce                                   -
    d  Delta     v2        sent         not_interested (20 Oct)                  sent 18 Oct (before)
    e  Echo      v2        sent         -                                        sent 18 Oct, no reply
    f  Foxtrot   v2        queued       -                                        -
    g  Golf      v2        approved     -                                        -
    h  Hotel     v2        draft        -                                        -
    i  India     -         -            -                                        -
    t  my test   v2        replied      interview_request  <- never counts       -
    plus a test-mode copy to my address and a rejected draft for India: both never count.
    """
    v1 = save_template(conn, "cold", "s1 {company}", "Hi {company}.\n\n{signature}")
    v2 = save_template(conn, "cold", "s2 {company}", "Hello {company}.\n\n{signature}")

    def contact(key, status):
        conn.execute("INSERT INTO contacts (company, role, email, status, created_at) VALUES (?, 'AI Engineer', ?, ?, ?)",
                     (key.title(), f"{key}@co.example" if key != "t" else TEST_TO, status, db.now()))
        return conn.execute("SELECT MAX(id) FROM contacts").fetchone()[0]

    def email(contact_id, template, status, kind="initial", test_mode=0, sent_at=None):
        conn.execute("INSERT INTO emails (contact_id, template_id, kind, subject, body_text, status, test_mode, sent_at,"
                     " created_at) VALUES (?, ?, ?, 's', 'b', ?, ?, ?, ?)",
                     (contact_id, template.id, kind, status, test_mode, sent_at, db.now()))
        return conn.execute("SELECT MAX(id) FROM emails").fetchone()[0]

    def reply(email_id, category, day):
        conn.execute("INSERT INTO replies (email_id, gmail_thread_id, from_addr, date, category, confidence, snippet,"
                     " created_at) VALUES (?, ?, 'x', ?, ?, 0.9, ?, ?)",
                     (email_id, f"t{email_id}", f"2026-10-{day:02d}T06:00:00Z", category, category, db.now()))

    sent = lambda day: f"2026-10-{day:02d}T05:00:00Z"   # 10:30 IST, same India date  # noqa: E731
    a = email(contact("alpha", "replied"), v1, "replied", sent_at=sent(10))
    reply(a, "interview_request", 12)
    b = email(contact("beta", "replied"), v1, "replied", sent_at=sent(10))
    reply(b, "auto_reply", 11)
    reply(b, "question", 13)
    c = email(contact("gamma", "bounced"), v1, "bounced", sent_at=sent(10))
    reply(c, "bounce", 10)
    d_contact = contact("delta", "replied")
    d = email(d_contact, v2, "replied", sent_at=sent(11))
    email(d_contact, v2, "sent", kind="follow_up", sent_at=sent(18))
    reply(d, "not_interested", 20)
    e_contact = contact("echo", "followed_up")
    email(e_contact, v2, "sent", sent_at=sent(11))
    email(e_contact, v2, "sent", kind="follow_up", sent_at=sent(18))
    email(contact("foxtrot", "drafted"), v2, "queued")
    email(contact("golf", "drafted"), v2, "approved", test_mode=1)
    email(contact("hotel", "drafted"), v2, "draft", test_mode=1)
    india = contact("india", "new")
    email(india, v2, "rejected", test_mode=1)
    t_contact = contact("t", "replied")
    t = email(t_contact, v2, "replied", sent_at=sent(12))
    reply(t, "interview_request", 12)
    email(t_contact, v2, "sent", test_mode=1, sent_at=sent(12))    # a test copy: never counts
    return conn


def test_the_funnel_matches_a_hand_count(known):
    f = stats.funnel(known, TEST_TO)
    # uploaded a-i = 9; approved a-g = 7 (h is a draft); queued f; sent a-e = 5 (c bounced counts as sent)
    # replied a, b, d = 3 (b's auto-reply alone wouldn't count, its question does); positive a; bounced c
    assert f.model_dump() == {"uploaded": 9, "approved": 7, "queued": 1, "sent": 5, "replied": 3,
                              "positive": 1, "bounced": 1}
    assert (f.rate(f.replied), f.rate(f.bounced), f.rate(f.positive)) == (0.6, 0.2, 0.2)


def test_reply_categories_count_every_reply_message(known):
    assert stats.reply_categories(known, TEST_TO) == {"auto_reply": 1, "bounce": 1, "interview_request": 1,
                                                      "not_interested": 1, "question": 1}


def test_per_template_version(known):
    rows = {r.template: r for r in stats.per_template(known, TEST_TO)}
    assert list(rows) == ["cold v1", "cold v2"]
    v1, v2 = rows["cold v1"], rows["cold v2"]
    assert (v1.sent, v1.replied, v1.positive, v1.bounced, v1.replied_after_follow_up) == (3, 2, 1, 1, 0)
    assert (v2.sent, v2.replied, v2.positive, v2.bounced, v2.replied_after_follow_up) == (2, 1, 0, 0, 1)
    assert v1.reply_rate == pytest.approx(2 / 3) and v2.reply_rate == 0.5


def test_sends_per_day_in_india_time_with_empty_days(known):
    days = dict(stats.sends_per_day(known, TEST_TO, days=10, today=date(2026, 10, 19)))
    assert len(days) == 10 and min(days) == date(2026, 10, 10)
    # 10 Oct: a, b, c; 11 Oct: d, e; 18 Oct: the two follow-ups; my test address never counts
    assert {d: n for d, n in days.items() if n} == {date(2026, 10, 10): 3, date(2026, 10, 11): 2,
                                                   date(2026, 10, 18): 2}


def test_recent_replies_newest_first_without_my_own(known):
    rows = stats.recent_replies(known, TEST_TO, limit=3)
    assert [(r["company"], r["category"]) for r in rows] == [
        ("Delta", "not_interested"), ("Beta", "question"), ("Alpha", "interview_request")]


def test_an_empty_database_gives_zeros_not_errors(conn):
    f = stats.funnel(conn, TEST_TO)
    assert f.sent == 0 and f.rate(f.replied) is None
    assert stats.per_template(conn, TEST_TO) == [] and stats.reply_categories(conn, TEST_TO) == {}
    assert all(n == 0 for _, n in stats.sends_per_day(conn, TEST_TO))
