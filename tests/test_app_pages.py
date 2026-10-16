"""The app itself, run by Streamlit's AppTest against a small known database: every page and tab
renders without an error, the labels say the right thing, the Review buttons work in one run, and a
page that breaks shows a readable message. Nothing touches the network (the no_network fixture)."""

from datetime import datetime, timedelta, timezone

import pytest
from streamlit.testing.v1 import AppTest

from mailmate import autosync, companies, config, db, health, openings, review, stats
from mailmate.n8n import N8nClient, ReplyDraft, ReplyText, SendAccepted
from mailmate.sentence_check import PersonalLine
from mailmate.templates import save_template

TEST_TO = "me.secondary@example.net"
ENV = {"N8N_BASE_URL": "http://localhost:5678", "N8N_WEBHOOK_TOKEN": "test-token", "MAILMATE_TEST_ADDRESS": TEST_TO}

PAGES_AND_TABS = [
    ("home", None), ("upload", None),
    ("contacts", "Contacts"), ("contacts", "Applied checklist"), ("contacts", "Openings"), ("contacts", "Do-not-contact"),
    ("templates", None),
    ("review", "Create drafts"), ("review", "Follow-ups"), ("review", "Review queue"),
    ("send", "Resume"), ("send", "Test email"), ("send", "Real sending"), ("send", "Batches"),
    ("replies", "Reply desk"), ("replies", "Sync"), ("replies", "All replies"), ("pipeline", None), ("dashboard", None),
]
TAB_KEYS = {"contacts": "contacts_tab", "review": "review_tab", "send": "send_tab", "replies": "replies_tab"}
ALL_GOOD = [health.Check(name="n8n", ok=True, detail="running"), health.Check(name="Gmail", ok=True, detail="today")]


def _script(page):
    """What `streamlit run app.py` does, for one page (AppTest runs this function's body as the script)."""
    import streamlit as st

    from mailmate.ui.main import main
    st.session_state["runs"] = st.session_state.get("runs", 0) + 1
    main(only=page)


def ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")


@pytest.fixture
def ids(tmp_path, monkeypatch):
    """
    contact        status in MailMate                         what the app should make of it
    -------------  -----------------------------------------  ----------------------------------------
    Nimbus Labs    new, High priority                         offered on Create drafts
    Quill Stack    draft (applied by direct link)             first in the Review queue, [Applied]
    Orbit AI       draft                                      next in the Review queue
    Delta Cloud    approved                                   waits on 5 Send
    Echo Data      first email sent 10 days ago, no reply     follow-up due
    Foxtrot        replied: interview request yesterday       a reply to answer
    Gamma          bounced                                    do-not-contact
    Hotel          marked contacted by hand, no email         "contacted (outside MailMate)"
    India          queued in batch #1, two days ago           "sync to confirm"
    """
    path = tmp_path / "mailmate.db"
    conn = db.connect(path)
    db.set_settings(conn, my_name="Asha Rao", signature="Asha Rao\nasha.example.dev", default_role="AI Engineer")
    template = save_template(conn, "Cold outreach", "Quick note for {company}",
                             "Hi {first_name},\n\n{personal_line}\n\nI'd love to talk about the {role} role at "
                             "{company}.\n\n{signature}")

    def contact(company, status="new", priority=""):
        conn.execute("INSERT INTO contacts (company, role, email, name, priority, status, created_at)"
                     " VALUES (?, 'AI Engineer', ?, 'Sam Lee', ?, ?, ?)",
                     (company, f"sam@{company.split()[0].lower()}.example", priority, status, ago(days=12)))
        return conn.execute("SELECT MAX(id) FROM contacts").fetchone()[0]

    def sent_email(contact_id, status, days, batch_id=None):
        conn.execute("INSERT INTO emails (contact_id, template_id, subject, body_text, status, test_mode, batch_id,"
                     " gmail_thread_id, created_at, approved_at, queued_at, sent_at)"
                     " VALUES (?, ?, 's', 'b', ?, 0, ?, ?, ?, ?, ?, ?)",
                     (contact_id, template.id, status, batch_id, f"thread{contact_id}", ago(days=days + 1),
                      ago(days=days + 1), ago(days=days), None if status == "queued" else ago(days=days)))
        return conn.execute("SELECT MAX(id) FROM emails").fetchone()[0]

    out = {"nimbus": contact("Nimbus Labs", priority="high")}
    companies.set_applied(conn, "Quill Stack", True, source="test")
    for key, company in (("quill", "Quill Stack"), ("orbit", "Orbit AI"), ("delta", "Delta Cloud")):
        out[key] = review.create_draft(conn, contact(company), template, PersonalLine())
    review.approve(conn, out["delta"])
    out["echo"] = sent_email(contact("Echo Data", "contacted"), "sent", days=10)
    foxtrot = sent_email(contact("Foxtrot", "replied"), "replied", days=3)
    conn.execute("INSERT INTO replies (email_id, gmail_thread_id, from_addr, date, snippet, body_text, category,"
                 " confidence, created_at) VALUES (?, ?, 'sam@foxtrot.example', ?, 'Can we talk Friday?',"
                 " 'Can we talk Friday?', 'interview_request', 0.9, ?)", (foxtrot, f"thread{foxtrot}", ago(days=1),
                                                                       ago(days=1)))
    gamma = contact("Gamma", "bounced")
    sent_email(gamma, "bounced", days=5)
    db.suppress(conn, "sam@gamma.example", "bounce")
    contact("Hotel", "contacted")
    conn.execute("INSERT INTO batches (template_id, test_mode, status, created_at, submitted_at)"
                 " VALUES (?, 0, 'submitted', ?, ?)", (template.id, ago(days=2), ago(days=2)))
    sent_email(contact("India", "drafted"), "queued", days=2, batch_id=1)
    conn.commit()
    conn.close()

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% test resume\n")
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "RESUME_PATH", resume)
    monkeypatch.setattr(config, "read_env_file", lambda *args, **kwargs: dict(ENV))
    # No real health check or background sync in these tests: they would go to the network.
    monkeypatch.setattr(health, "check_all", lambda *args, **kwargs: list(ALL_GOOD))
    out["background_syncs"] = []
    monkeypatch.setattr(autosync, "start", lambda *args, **kwargs: out["background_syncs"].append(args) or True)
    out["foxtrot_reply"] = 1
    return out


def run(page: str, tab: str | None = None) -> AppTest:
    at = AppTest.from_function(_script, args=(page,), default_timeout=60)
    if tab:
        at.session_state[f"goto_{TAB_KEYS[page]}"] = tab
    return at.run()


def texts(at: AppTest) -> str:
    return "\n".join(e.value for kind in ("markdown", "success", "info", "warning", "error", "caption")
                     for e in getattr(at, kind))


@pytest.mark.parametrize(("page", "tab"), PAGES_AND_TABS)
def test_every_page_and_tab_renders_without_an_error(ids, page, tab):
    at = run(page, tab)
    assert not at.exception, at.exception
    assert not [e.value for e in at.error if "went wrong" in e.value]
    assert at.title, "every page has a title"


def test_home_lists_what_needs_doing_most_urgent_first(ids):
    steps = [m.value for m in run("home").markdown if m.value[:2].rstrip(".").isdigit() or m.value.startswith("👉")]
    joined = "\n".join(steps)
    assert steps[0].startswith("👉 1.")
    assert "Sync replies" in joined and "#1" in joined           # India's batch went out two days ago
    assert "**Answer 1 recruiter(s)**, 1 overdue (1 interview request(s))" in joined   # Foxtrot, a day ago
    assert "Review 2 draft(s)" in joined
    assert "Draft 1 follow-up(s)" in joined                       # Echo, sent ten days ago
    assert joined.index("Sync replies") < joined.index("Review 2 draft(s)")


def test_contacts_shows_where_each_email_is(ids):
    table = run("contacts", "Contacts").dataframe[0].value
    status = dict(zip(table["Company"], table["Status"]))
    assert status["Nimbus Labs"] == "🆕 new"
    assert status["Quill Stack"] == "📝 draft"
    assert status["Delta Cloud"] == "✅ approved (not sent)"
    assert status["India"] == "📤 queued (sync to confirm)"
    assert status["Hotel"] == "📨 contacted (outside MailMate)"
    assert status["Gamma"] == "⛔ bounced"
    assert all(table["Next step"])


def test_review_labels_say_applied_or_new(ids):
    options = run("review", "Review queue").selectbox(key="review_email").options
    assert any("[Applied]" in o and "Quill Stack" in o for o in options)
    assert any("[New]" in o and "Orbit AI" in o for o in options)


def test_approve_takes_one_run_and_the_next_draft_opens_in_its_place(ids):
    at = run("review", "Review queue")
    assert at.selectbox(key="review_email").value == ids["quill"]
    runs = at.session_state["runs"]
    at.button(key=f"approve_{ids['quill']}").click().run()
    assert at.session_state["runs"] == runs + 1                  # no second run, so no dimmed half-page
    assert at.selectbox(key="review_email").value == ids["orbit"]
    assert [t.value for t in at.toast] == [f"Approved #{ids['quill']}. Now showing #{ids['orbit']}."]
    assert not at.exception


def test_reject_moves_on_and_unapprove_stays(ids):
    at = run("review", "Review queue")
    at.button(key=f"reject_{ids['quill']}").click().run()
    assert at.selectbox(key="review_email").value == ids["orbit"]
    at.selectbox(key="review_email").set_value(ids["delta"]).run()
    at.button(key=f"unapprove_{ids['delta']}").click().run()
    assert at.selectbox(key="review_email").value == ids["delta"]
    assert "draft again" in at.toast[0].value


def test_a_refused_approval_is_shown_at_the_email(ids):
    at = run("review", "Review queue")
    conn = db.connect(config.DB_PATH)
    db.suppress(conn, "sam@quill.example", "manual")                # someone said no meanwhile
    conn.close()
    at.button(key=f"approve_{ids['quill']}").click().run()
    assert any("Couldn't approve it" in e.value for e in at.error)
    assert at.selectbox(key="review_email").value == ids["quill"]   # still on it, nothing changed


def test_a_page_that_breaks_shows_a_readable_message(ids, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("the funnel query failed")
    monkeypatch.setattr(stats, "funnel", broken)
    at = run("dashboard")
    assert any("Something went wrong on this page" in e.value and "funnel query failed" in e.value for e in at.error)
    details = next(x for x in at.expander if x.label == "Technical details")
    assert len(at.exception) == len(details.exception) == 1       # the trace only inside "Technical details"


def test_opening_the_app_makes_the_days_backup_next_to_the_database(ids):
    run("home")
    copies = list((config.DB_PATH.parent / "backups").glob("mailmate-*.db"))
    assert len(copies) == 1
    run("contacts", "Contacts")
    assert len(list((config.DB_PATH.parent / "backups").glob("mailmate-*.db"))) == 1   # once a day


def test_a_missing_env_value_is_explained_not_a_crash(ids, monkeypatch):
    monkeypatch.setattr(config, "read_env_file", lambda *args, **kwargs: {})
    at = run("home")
    assert not at.exception
    assert any("Setup problem" in e.value and ".env" in e.value for e in at.error)


def test_home_starts_a_background_sync_when_the_last_one_is_old(ids):
    run("home")
    assert len(ids["background_syncs"]) == 1                      # India went out two days ago, never synced


def test_a_health_problem_comes_first_with_its_fix(ids, monkeypatch):
    down = [health.Check(name="n8n", ok=False, detail="no answer from http://localhost:5678 (ConnectError)",
                         fix="Start Docker Desktop, then in PowerShell: `docker start n8n` (wait ~20 s).")]
    monkeypatch.setattr(health, "check_all", lambda *args, **kwargs: down)
    first = next(m.value for m in run("home").markdown if m.value.startswith("👉"))
    assert "n8n: no answer" in first and "docker start n8n" in first


def test_the_reply_desk_suggests_saves_a_gmail_draft_and_marks_answered(ids, monkeypatch):
    asked, saved = [], []
    monkeypatch.setattr(N8nClient, "write_reply", lambda self, request: asked.append(request) or ReplyText(
        text="Hi Sam,\nFriday works. [a time that suits you]\n\nBest regards,\nAsha Rao"))
    monkeypatch.setattr(N8nClient, "save_reply_draft", lambda self, thread, text: saved.append((thread, text)) or
                        ReplyDraft(draft_id="r-1", to="sam@foxtrot.example"))
    at = run("replies", "Reply desk")
    reply = ids["foxtrot_reply"]
    at.text_area(key=f"notes_{reply}").set_value("free Friday 3-5 pm IST")
    at.button(key=f"suggest_{reply}").click().run()
    assert asked[0]["notes"] == "free Friday 3-5 pm IST" and asked[0]["their_message"] == "Can we talk Friday?"
    assert at.button(key=f"draft_{reply}").disabled                    # a [placeholder] is left
    at.text_area(key=f"answer_{reply}").set_value("Hi Sam,\nFriday at 3 pm IST works.\n\nBest regards,\nAsha Rao")
    at.run()
    at.button(key=f"draft_{reply}").click().run()
    conn = db.connect(config.DB_PATH)
    thread = conn.execute("SELECT gmail_thread_id FROM replies WHERE id = ?", (reply,)).fetchone()[0]
    conn.close()
    assert saved == [(thread, "Hi Sam,\nFriday at 3 pm IST works.\n\nBest regards,\nAsha Rao")]
    assert any("Draft saved in Gmail" in s.value for s in at.success)
    at.button(key=f"answered_{reply}").click().run()
    assert any("Nothing to answer" in s.value for s in at.success)
    conn = db.connect(config.DB_PATH)
    assert tuple(conn.execute("SELECT handled, gmail_draft_id FROM replies").fetchone()) == ("answered", "r-1")
    conn.close()


def test_a_batch_can_be_scheduled_for_the_next_morning(ids, monkeypatch):
    conn = db.connect(config.DB_PATH)
    conn.execute("UPDATE templates SET test_checked_at = ?", (db.now(),))     # Delta's version is tested
    conn.execute("UPDATE batches SET status = 'done'")                         # nothing else running
    conn.commit()
    conn.close()
    handed = []
    monkeypatch.setattr(N8nClient, "send_batch", lambda self, payload, resume, name: handed.append(payload) or
                        SendAccepted(accepted=len(payload["scheduled_emails"]), batch_id=payload["batch_id"]))
    at = run("send", "Real sending")
    next(r for r in at.radio if r.label == "When").set_value("Schedule for later").run()
    start_day = next(d for d in at.date_input if d.label == "Start on").value
    picked = at.multiselect[0]
    picked.set_value([ids["delta"]]).run()
    next(t for t in at.text_input if t.label == "Type 1 to confirm").set_value("1").run()
    next(b for b in at.button if b.label == "Schedule 1 real email(s)").click().run()
    assert not at.exception, at.exception
    assert [e.value for e in at.error] == []
    assert [h["start_at"] for h in handed] == [f"{start_day.isoformat()}T04:00:00+00:00"]   # 09:30 IST, window opens
    assert any("holds it until" in s.value for s in at.success)


def test_the_openings_tab_watches_job_links_and_new_openings_become_job_links(ids, monkeypatch):
    conn = db.connect(config.DB_PATH)
    conn.execute("UPDATE contacts SET job_url = 'https://jobs.lever.co/Orbit/abc' WHERE company = 'Orbit AI'")
    conn.commit()
    conn.close()
    monkeypatch.setattr(openings, "fetch", lambda board, client: [
        openings.Opening(title="Applied AI Engineer", url="https://jobs.lever.co/Orbit/new", location="Remote"),
        openings.Opening(title="Office Manager", url="https://jobs.lever.co/Orbit/office")])
    at = run("contacts", "Openings")
    assert any("Orbit AI" in t.value for t in at.toast)                   # watched from its job link
    next(b for b in at.button if b.label == "Check for new openings").click().run()
    assert any("1 new opening(s)" in s.value for s in at.success)
    assert any("Applied AI Engineer" in m.value for m in at.markdown)
    assert not any("Office Manager" in m.value for m in at.markdown)      # not an AI title
    steps = "".join(m.value for m in run("home").markdown)
    assert "1 new opening(s)** at Orbit AI" in steps
    at = run("contacts", "Openings")
    next(b for b in at.button if b.label == "Use as job link").click().run()
    assert not at.exception, at.exception
    conn = db.connect(config.DB_PATH)
    assert conn.execute("SELECT job_url FROM contacts WHERE company = 'Orbit AI'").fetchone()[0] == \
        "https://jobs.lever.co/Orbit/abc"                                 # it had a link already: kept
    conn.close()


def test_an_interview_request_enters_the_pipeline_and_an_interview_shows_on_home(ids):
    at = run("pipeline")
    assert any("**Foxtrot**" in m.value for m in at.markdown)          # added from its interview request
    at.selectbox(key="pipeline_pick").set_value("foxtrot").run()
    next(s for s in at.selectbox if s.label == "Stage").set_value("interviewing")
    next(t for t in at.text_input if t.label == "Next step").set_value("Technical round")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(timezone(timedelta(hours=5, minutes=30)))
    next(d for d in at.date_input if d.label == "When (date)").set_value(tomorrow.date())
    next(b for b in at.button if b.label == "Save").click().run()
    assert not at.exception, at.exception
    assert any("Foxtrot: 🧑‍💻 Interviewing" in t.value for t in at.toast)
    steps = [m.value for m in run("home").markdown]
    assert any("Foxtrot:** Technical round" in m for m in steps)
