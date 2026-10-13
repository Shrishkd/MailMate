"""Direct apply links and the 'applied by direct link' checklist: import, update, order."""

from datetime import datetime

import pytest

from mailmate import companies, db, review, selection
from mailmate.importer import Contact, analyse, is_applied
from mailmate.sending import IST
from tests.test_importer import csv_bytes

TEST_TO = "me.secondary@example.net"
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=IST)
HEADER = ["Company", "Email", "Direct Apply Link", "Priority", "Applied"]


def rows(*lines):
    return csv_bytes(HEADER, *lines)


# --- reading the sheet ----------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [("Yes", True), ("y", True), ("✓", True), ("Applied", True),
                                             ("12/10/2026", True), ("2026-10-12", True), ("No", False),
                                             ("", False), ("not yet", False), ("maybe", False)])
def test_applied_values(value, expected):
    assert is_applied(value) is expected


def test_direct_apply_link_and_applied_column_are_read(conn, fake_dns):
    report = analyse("jobs.csv", rows(["Nimbus Labs", "priya@nimbuslabs.ai", "https://nimbuslabs.ai/jobs/42", "Medium", "Yes"],
                                      ["Quillstack", "neha@quillstack.io", "https://quillstack.io/apply", "High", ""]),
                     conn, fake_dns, default_role="AI Engineer")
    nimbus, quill = (r.contact for r in report.valid)
    assert (nimbus.job_url, nimbus.applied, quill.applied) == ("https://nimbuslabs.ai/jobs/42", True, False)
    assert report.applied_companies == ["Nimbus Labs"]


def test_a_status_of_applied_means_applied_not_already_emailed(conn, fake_dns):
    report = analyse("jobs.csv", csv_bytes(["Company", "Email", "Status"], ["Nimbus Labs", "priya@nimbuslabs.ai", "Applied"]),
                     conn, fake_dns, default_role="AI Engineer")
    assert report.valid and report.valid[0].contact.applied and report.contacted_elsewhere == []


def test_existing_contacts_get_empty_fields_filled_never_overwritten(conn, fake_dns):
    db.add_contacts(conn, [Contact(company="Nimbus Labs", role="AI Engineer", email="priya@nimbuslabs.ai", priority="high"),
                           Contact(company="Quillstack", role="AI Engineer", email="neha@quillstack.io",
                                   job_url="https://old.example/job")], "first.csv")
    report = analyse("jobs.csv", rows(["Nimbus Labs", "priya@nimbuslabs.ai", "https://nimbuslabs.ai/jobs/42", "Low", "Yes"],
                                      ["Quillstack", "neha@quillstack.io", "https://new.example/job", "Medium", ""]),
                     conn, fake_dns, default_role="AI Engineer")
    assert report.valid == [] and report.applied_companies == ["Nimbus Labs"]
    assert db.fill_contact_blanks(conn, report.known_updates, "jobs.csv") == 2
    got = {r["email"]: (r["job_url"], r["priority"]) for r in conn.execute("SELECT email, job_url, priority FROM contacts")}
    assert got == {"priya@nimbuslabs.ai": ("https://nimbuslabs.ai/jobs/42", "high"),     # priority kept
                   "neha@quillstack.io": ("https://old.example/job", "medium")}         # link kept
    assert db.fill_contact_blanks(conn, report.known_updates, "jobs.csv") == 0           # nothing left to fill


# --- the checklist --------------------------------------------------------------------------------

def test_checklist_one_row_per_company_with_links(conn):
    db.add_contacts(conn, [Contact(company="Lexsi.ai", role="AI", email="a@lexsi.example", job_url="https://lexsi.ai/j/1"),
                           Contact(company="Lexsi AI", role="AI", email="b@lexsi.example", job_url="https://lexsi.ai/j/2"),
                           Contact(company="Oracle", role="AI", email="o@oracle.example")], "jobs.csv")
    companies.set_applied(conn, "Oracle", True)
    items = companies.checklist(conn)
    assert [(i["company"], i["applied"], i["links"]) for i in items] == [
        ("Oracle", True, []), ("Lexsi.ai", False, ["https://lexsi.ai/j/1", "https://lexsi.ai/j/2"])]
    companies.set_applied(conn, "oracle inc", False)                   # same company, written differently
    assert companies.applied_keys(conn) == set()


# --- order: applied first, then priority ----------------------------------------------------------

@pytest.fixture
def people(conn):
    db.add_contacts(conn, [Contact(company=c, role="AI Engineer", email=e, priority=p) for c, e, p in [
        ("NVIDIA", "n@nvidia.example", "high"), ("Oracle", "o@oracle.example", "medium"),
        ("Lexsi.ai", "l@lexsi.example", ""), ("Fractal", "f@fractal.example", "high")]], "jobs.csv")
    return conn


def test_applied_companies_come_first_then_priority(people):
    companies.set_applied(people, "Lexsi AI", True)          # no priority at all, but applied
    companies.set_applied(people, "Oracle", True)            # medium, applied
    order = [c["company"] for c in review.contacts_to_draft(people)]
    assert order == ["Oracle", "Lexsi.ai", "NVIDIA", "Fractal"]
    chosen = selection.suggest_drafts(people, review.contacts_to_draft(people), NOW, TEST_TO, limit=3)
    assert [people.execute("SELECT company FROM contacts WHERE id = ?", (i,)).fetchone()[0] for i in chosen] == [
        "Oracle", "Lexsi.ai", "NVIDIA"]


def test_without_applied_companies_the_order_is_unchanged(people):
    assert [c["company"] for c in review.contacts_to_draft(people)] == ["NVIDIA", "Fractal", "Oracle", "Lexsi.ai"]
