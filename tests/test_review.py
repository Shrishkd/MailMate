"""The review queue: drafting, checks on every approval, overrides, rejection, approve-all."""

import json

import pytest

from mailmate import db, review
from mailmate.importer import Contact
from mailmate.n8n import Source
from mailmate.review import ReviewError
from mailmate.sentence_check import PersonalLine
from mailmate.templates import OPT_OUT_LINE, save_template

BODY = """Hey {first_name},

I saw that {company} is hiring for the {role} role.

{personal_line}

I am {my_name}.

{signature}"""

NEWS = Source(id=1, title="Nimbus Labs opens research lab in Pune", url="https://news.example/nimbus",
              content="Nimbus Labs has opened a research lab in Pune focused on Indic speech models.")
GOOD_LINE = "Nimbus Labs opening a Pune lab for Indic speech models fits the problems I most want to work on."


@pytest.fixture
def setup(conn):
    db.set_settings(conn, my_name="Shrish", signature="Thanks,\nShrish")
    template = save_template(conn, "cold", "{role} at {company}?", BODY)
    contacts = [Contact(company="Nimbus Labs", role="GenAI Engineer", email="priya@nimbuslabs.ai", name="Priya Sharma"),
                Contact(company="Quillstack", role="AI Engineer", email="neha@quillstack.io", name="Neha")]
    db.add_contacts(conn, contacts, "jobs.csv")
    ids = {r["email"]: r["id"] for r in conn.execute("SELECT id, email FROM contacts")}
    return template, ids


def line(sentence=GOOD_LINE, sources=(NEWS,)):
    return PersonalLine(sentence=sentence, sources=list(sources), model="gpt-oss:120b")


def draft(conn, setup, email="priya@nimbuslabs.ai", personal=None):
    template, ids = setup
    return review.create_draft(conn, ids[email], template, personal or line())


def status(conn, email_id):
    return conn.execute("SELECT status FROM emails WHERE id = ?", (email_id,)).fetchone()[0]


# --- drafting -------------------------------------------------------------------------------

def test_a_draft_is_the_merged_email(conn, setup):
    email_id = draft(conn, setup)
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    assert row["subject"] == "GenAI Engineer at Nimbus Labs?"
    assert row["body_text"].startswith("Hey Priya,") and GOOD_LINE in row["body_text"]
    assert OPT_OUT_LINE in row["body_text"] and row["status"] == "draft"
    assert json.loads(row["sources"])[0]["url"] == NEWS.url
    assert conn.execute("SELECT status FROM contacts WHERE email = 'priya@nimbuslabs.ai'").fetchone()[0] == "drafted"
    assert review.check_draft(conn, email_id).clean


def test_draft_without_a_personal_line(conn, setup):
    email_id = draft(conn, setup, personal=PersonalLine())
    check = review.check_draft(conn, email_id)
    assert check.clean and "no personal line" in check.notes[0]


def test_only_contacts_without_an_email_can_be_drafted(conn, setup):
    draft(conn, setup)
    assert [c["email"] for c in review.contacts_to_draft(conn)] == ["neha@quillstack.io"]
    with pytest.raises(ReviewError, match="already has an email"):
        draft(conn, setup)


def test_suppressed_contacts_are_not_drafted(conn, setup):
    db.suppress(conn, "neha@quillstack.io", "not_interested")
    assert [c["email"] for c in review.contacts_to_draft(conn)] == ["priya@nimbuslabs.ai"]
    with pytest.raises(ReviewError, match="do-not-contact"):
        draft(conn, setup, email="neha@quillstack.io")


# --- approving --------------------------------------------------------------------------------

def test_a_clean_draft_is_approved(conn, setup):
    email_id = draft(conn, setup)
    review.approve(conn, email_id)
    assert status(conn, email_id) == "approved"


def test_a_failing_personal_line_needs_a_visible_override(conn, setup):
    email_id = draft(conn, setup, personal=line("I'm excited by Nimbus Labs opening a lab in Pune for Indic speech."))
    check = review.check_draft(conn, email_id)
    assert [p.text for p in check.problems][0].startswith("personal line: it uses flattery")
    with pytest.raises(ReviewError, match="needs an override with a reason"):
        review.approve(conn, email_id)
    assert status(conn, email_id) == "draft"
    review.approve(conn, email_id, override_reason="I like this wording")
    assert status(conn, email_id) == "approved"
    event = conn.execute("SELECT detail FROM events WHERE kind = 'email_approved'").fetchone()[0]
    assert json.loads(event)["reason"] == "I like this wording" and json.loads(event)["overridden"]


def test_suppression_blocks_approval_even_with_an_override(conn, setup):
    email_id = draft(conn, setup)
    db.suppress(conn, "priya@nimbuslabs.ai", "not_interested")
    with pytest.raises(ReviewError, match="can't be approved: priya@nimbuslabs.ai is on the do-not-contact list"):
        review.approve(conn, email_id, override_reason="please")


def test_an_already_contacted_contact_blocks_approval(conn, setup):
    email_id = draft(conn, setup)
    conn.execute("UPDATE contacts SET status = 'contacted' WHERE email = 'priya@nimbuslabs.ai'")
    with pytest.raises(ReviewError, match="already contacted"):
        review.approve(conn, email_id, override_reason="please")


# --- editing re-runs the checks ---------------------------------------------------------------

def test_editing_the_personal_line_rebuilds_the_email_and_rechecks_it(conn, setup):
    email_id = draft(conn, setup)
    review.approve(conn, email_id)
    review.set_personal_line(conn, email_id, "Nimbus Labs just raised money from Sequoia for its Pune lab.")
    assert status(conn, email_id) == "draft"
    body = conn.execute("SELECT body_text FROM emails WHERE id = ?", (email_id,)).fetchone()[0]
    assert "Sequoia" in body
    assert any("'Sequoia' is not in the sources" in p.text for p in review.check_draft(conn, email_id).problems)


def test_clearing_the_personal_line_is_fine(conn, setup):
    email_id = draft(conn, setup)
    review.set_personal_line(conn, email_id, "")
    assert review.check_draft(conn, email_id).clean


@pytest.mark.parametrize("change, problem", [
    (lambda b: b.replace(OPT_OUT_LINE, ""), "the opt-out line is missing"),
    (lambda b: b + "\nP.S. see [link]", "'[link]' in the body looks like a placeholder"),
    (lambda b: b.replace("Nimbus Labs", "your company"), "doesn't mention Nimbus Labs"),
])
def test_hand_edits_are_checked(conn, setup, change, problem):
    email_id = draft(conn, setup, personal=PersonalLine())
    row = conn.execute("SELECT subject, body_text FROM emails WHERE id = ?", (email_id,)).fetchone()
    subject = "Hello" if "mention" in problem else row["subject"]
    review.edit_text(conn, email_id, subject, change(row["body_text"]))
    check = review.check_draft(conn, email_id)
    assert any(problem in p.text for p in check.problems), check.problems
    assert "edited by hand" in " ".join(check.notes)


def test_a_harmless_hand_edit_is_only_a_note(conn, setup):
    email_id = draft(conn, setup)
    row = conn.execute("SELECT subject, body_text FROM emails WHERE id = ?", (email_id,)).fetchone()
    review.edit_text(conn, email_id, row["subject"], row["body_text"].replace("I am Shrish.", "I'm Shrish."))
    check = review.check_draft(conn, email_id)
    assert check.clean and "edited by hand: differs from the template" in check.notes


# --- rejecting --------------------------------------------------------------------------------

def test_reject_lets_the_contact_be_drafted_again(conn, setup):
    email_id = draft(conn, setup)
    review.reject(conn, email_id)
    assert status(conn, email_id) == "rejected"
    assert "priya@nimbuslabs.ai" in [c["email"] for c in review.contacts_to_draft(conn)]
    draft(conn, setup)   # no error


def test_reject_and_never_contact(conn, setup):
    email_id = draft(conn, setup)
    review.reject(conn, email_id, never_contact=True)
    assert db.suppressed(conn)["priya@nimbuslabs.ai"] == "manual"
    assert "priya@nimbuslabs.ai" not in [c["email"] for c in review.contacts_to_draft(conn)]


def test_sent_emails_cant_be_edited(conn, setup):
    email_id = draft(conn, setup)
    conn.execute("UPDATE emails SET status = 'sent' WHERE id = ?", (email_id,))
    with pytest.raises(ReviewError, match="sent and can't be changed"):
        review.set_personal_line(conn, email_id, "x")


# --- approve all ------------------------------------------------------------------------------

def add_real_batches(conn, template_id, n):
    for _ in range(n):
        conn.execute("INSERT INTO batches (template_id, test_mode, status, created_at) VALUES (?, 0, 'done', ?)",
                     (template_id, db.now()))


def test_approve_all_is_locked_for_the_first_two_real_batches(conn, setup):
    template, _ = setup
    draft(conn, setup)
    allowed, why = review.bulk_approval_status(conn, reviewed=10)
    assert not allowed and "first 2 real batches" in why
    with pytest.raises(ReviewError, match="isn't available yet"):
        review.approve_all_clean(conn, reviewed=10)
    add_real_batches(conn, template.id, 1)
    assert not review.bulk_approval_status(conn, reviewed=10)[0]
    add_real_batches(conn, template.id, 1)
    assert review.bulk_approval_status(conn, reviewed=10) == (True, "")


def test_approve_all_needs_five_samples_and_skips_drafts_with_problems(conn, setup):
    template, _ = setup
    add_real_batches(conn, template.id, 2)
    clean = draft(conn, setup)
    flawed = draft(conn, setup, email="neha@quillstack.io", personal=line("I'm excited by Quillstack's new lab."))
    assert "look at 5 drafts first (4 so far)" in review.bulk_approval_status(conn, reviewed=4)[1]
    assert review.approve_all_clean(conn, reviewed=5) == (1, 1)
    assert (status(conn, clean), status(conn, flawed)) == ("approved", "draft")
