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
    assert [(i["company"], i["applied"], i["links"]) for i in items] == [          # alphabetical, never re-sorted
        ("Lexsi.ai", False, ["https://lexsi.ai/j/1", "https://lexsi.ai/j/2"]), ("Oracle", True, [])]
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


def test_saving_the_checklist_changes_only_what_was_changed(conn):
    """The old bug: ticks were matched by row position, and rows moved after a save."""
    db.add_contacts(conn, [Contact(company=c, role="AI", email=f"x@{c.lower()}.example")
                           for c in ("Alpha", "Beta", "Gamma", "Delta")], "jobs.csv")
    companies.set_applied(conn, "Gamma", True)
    shown = {i["company"]: i["applied"] for i in companies.checklist(conn)}
    assert list(shown) == ["Alpha", "Beta", "Delta", "Gamma"]
    shown["Beta"] = True                                       # tick Beta, leave the rest as shown
    assert companies.save_checklist(conn, shown) == ["Beta"]
    assert [i["company"] for i in companies.checklist(conn)] == ["Alpha", "Beta", "Delta", "Gamma"]   # nothing moved
    assert companies.save_checklist(conn, shown) == []         # saving the same screen again changes nothing
    assert companies.applied_keys(conn) == {"beta", "gamma"}


def test_the_checklist_shows_when_a_company_was_first_drafted(conn):
    from mailmate import review
    from mailmate.sentence_check import PersonalLine
    from mailmate.templates import save_template
    db.set_settings(conn, my_name="Shrish", signature="Thanks")
    template = save_template(conn, "cold", "{role} at {company}?", "Hi,\n\n{company}.\n\n{signature}")
    db.add_contacts(conn, [Contact(company="Alpha", role="AI", email="a@alpha.example"),
                           Contact(company="Beta", role="AI", email="b@beta.example")], "jobs.csv")
    alpha = conn.execute("SELECT id FROM contacts WHERE company = 'Alpha'").fetchone()[0]
    review.create_draft(conn, alpha, template, PersonalLine())
    drafted = {i["company"]: i["drafted_at"] for i in companies.checklist(conn)}
    assert drafted["Alpha"] and drafted["Beta"] is None


# --- the 'Applied outreach' template ------------------------------------------------------------

MY_OPENING = ("I recently came across the news that {company} is hiring for the {role} role, and given that "
              "building AI agents and GenAI products is right in my wheelhouse, I couldn't just scroll past.")


def test_the_applied_variant_rewords_my_opening():
    from mailmate.templates import Template, applied_variant
    base = Template(name="cold", subject="Can I take just 0.35% of your day?",
                    body="Hey {first_name},\n\n" + MY_OPENING + "\n\n{personal_line}\n\n{signature}")
    subject, body = applied_variant(base)
    assert subject == base.subject
    assert ("I recently applied for the {role} role at {company} through your careers page, and given that "
            "building AI agents and GenAI products is right in my wheelhouse, I wanted to reach out to you directly "
            "as well.") in body
    assert "came across the news" not in body and body.startswith("Hey {first_name},")


def test_a_differently_worded_template_gets_the_sentence_after_the_greeting():
    from mailmate.templates import Template, applied_variant
    _, body = applied_variant(Template(name="x", subject="s", body="Hi {first_name},\n\nText.\n\n{signature}"))
    assert body.startswith("Hi {first_name},\n\nI recently applied for the {role} role at {company} "
                           "through your careers page and wanted to reach out to you directly as well.")


def test_drafts_at_applied_companies_use_the_applied_template(conn):
    from mailmate import review
    from mailmate.sentence_check import PersonalLine
    from mailmate.templates import APPLIED_TEMPLATE, save_template
    db.set_settings(conn, my_name="Shrish", signature="Thanks")
    cold = save_template(conn, "cold", "{role} at {company}?", "Hey,\n\n" + MY_OPENING + "\n\n{signature}")
    db.add_contacts(conn, [Contact(company="Alpha", role="Agentic AI Intern", email="a@alpha.example"),
                           Contact(company="Beta", role="AI Engineer", email="b@beta.example")], "jobs.csv")
    companies.set_applied(conn, "Alpha", True)
    alpha, beta = (conn.execute("SELECT * FROM contacts WHERE company = ?", (c,)).fetchone() for c in ("Alpha", "Beta"))
    assert review.template_for(conn, beta, cold).name == "cold"
    applied = review.template_for(conn, alpha, cold)
    assert applied.name == APPLIED_TEMPLATE and applied.version == 1
    assert review.template_for(conn, alpha, cold).id == applied.id          # created once, then reused
    email_id = review.create_draft(conn, alpha["id"], applied, PersonalLine())
    body = conn.execute("SELECT body_text FROM emails WHERE id = ?", (email_id,)).fetchone()[0]
    assert "I recently applied for the Agentic AI Intern role at Alpha through your careers page" in body
