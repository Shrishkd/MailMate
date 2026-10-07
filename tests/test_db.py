import sqlite3

import pytest

from mailmate import db
from mailmate.importer import Contact


def contact(email="priya@nimbuslabs.ai") -> Contact:
    return Contact(company="Nimbus Labs", role="GenAI Engineer", email=email)


def test_add_contacts_logs_an_event_each(conn):
    assert db.add_contacts(conn, [contact(), contact("ravi@nimbuslabs.ai")], "jobs.csv") == (2, [])
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events")]
    assert kinds == ["contact_added", "contact_added"]


def test_same_address_is_never_added_twice(conn):
    db.add_contacts(conn, [contact()], "a.csv")
    assert db.add_contacts(conn, [contact("PRIYA@nimbuslabs.ai")], "b.csv") == (0, ["priya@nimbuslabs.ai"])


def test_do_not_contact_is_rechecked_at_write_time(conn):
    db.suppress(conn, "priya@nimbuslabs.ai", "bounce")
    assert db.add_contacts(conn, [contact()], "a.csv") == (0, ["priya@nimbuslabs.ai"])


def test_events_are_append_only(conn):
    db.add_contacts(conn, [contact()], "a.csv")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET kind = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")


def test_schema_allows_one_real_email_per_kind_per_contact(conn):
    db.add_contacts(conn, [contact()], "a.csv")
    conn.execute("INSERT INTO templates (name, version, subject, body, created_at) VALUES ('t', 1, 's', 'b', ?)",
                 (db.now(),))
    insert = ("INSERT INTO emails (contact_id, template_id, kind, status, test_mode, created_at)"
              " VALUES (1, 1, ?, ?, ?, ?)")
    conn.execute(insert, ("initial", "rejected", 0, db.now()))   # rejected drafts don't count
    conn.execute(insert, ("initial", "sent", 1, db.now()))       # neither do test sends
    conn.execute(insert, ("initial", "sent", 0, db.now()))
    conn.execute(insert, ("follow_up", "approved", 0, db.now()))
    for kind in ("initial", "follow_up"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (kind, "approved", 0, db.now()))


def test_status_values_are_checked(conn):
    db.add_contacts(conn, [contact()], "a.csv")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE contacts SET status = 'emailed'")
