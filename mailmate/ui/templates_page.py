"""3 Templates: your details, the email templates, a preview with a W1 personal line."""

import streamlit as st

from mailmate import db
from mailmate.config import ConfigError, n8n_settings
from mailmate.n8n import N8nClient, N8nError
from mailmate.sentence_check import PersonalLine, is_job_ad, personalize_checked
from mailmate.templates import FIELDS, Template, check_template, latest_templates, merge, save_template, values_for
from mailmate.ui import common as ui


SAMPLE_CONTACT = {"name": "Priya Sharma", "company": "Example Corp", "role": "Generative AI Engineer"}


SAMPLE_LINE = "I also saw that Example Corp recently launched an AI assistant for home buyers."


def _ask_w1(contact, label: str, my_name: str) -> None:
    """Button callback: runs before the page redraws, so it can fill the personal-line box."""
    st.session_state.w1 = {"for": label, "error": "", "result": None}
    try:
        client = N8nClient(n8n_settings())
    except ConfigError as exc:
        st.session_state.w1["error"] = str(exc)
        return
    try:
        with st.spinner(f"W1 is researching {contact['company']} (usually 10-40 s, twice that if it retries)..."):
            line = personalize_checked(client.personalize, contact, my_name=my_name)
    except N8nError as exc:
        st.session_state.w1["error"] = str(exc)
        return
    finally:
        client.close()
    st.session_state.w1["result"] = line
    st.session_state.personal_line = line.sentence


def _show_w1_result(label: str) -> None:
    w1 = st.session_state.get("w1")
    if not w1 or w1["for"] != label:
        return
    if w1["error"]:
        st.error(f"W1 failed: {w1['error']}")
        return
    line: PersonalLine = w1["result"]
    if line.sentence:
        st.success(f"Personal line passed every check (attempt {len(line.attempts)} of 2).")
    elif line.attempts and not line.attempts[0].raw and not line.attempts[0].error:
        st.warning("W1 found nothing clearly about this company, so this email has no personal line.")
    else:
        st.warning("No personal line passed the checks, so this email goes without one.")
    for n, attempt in enumerate(line.attempts, 1):
        with st.expander(f"Attempt {n}: {'accepted' if not attempt.problems else 'rejected'}",
                         expanded=bool(attempt.problems)):
            if attempt.raw:
                st.text(attempt.raw)
            for problem in attempt.problems:
                st.markdown(f"- :red[{problem}]")
            for s in attempt.sources:
                st.markdown(f"[S{s.id}] [{s.title}]({s.url})" + ("  · *job ad, doesn't count*" if is_job_ad(s.url) else ""))
    st.caption(f"Written by {line.model or 'W1'}; checked by MailMate. The Review page checks every email "
               "again before it can be approved.")


def page_templates():
    conn = ui.conn()
    ui.header("3 · Templates", "The emails' wording. Saving a change makes a new version; every version must pass a "
              "test send (5 Send → Test email) before it can go to recruiters.")
    settings = db.get_settings(conn)
    ui.make_applied_template()
    with st.expander("Your details: {my_name} and {signature}", expanded=not settings.get("my_name")):
        with st.form("details"):
            my_name = st.text_input("Your name", settings.get("my_name", ""))
            signature = st.text_area("Signature", settings.get("signature", ""), height=130,
                                     help="Replaces {signature}. The opt-out line is added just above it.")
            if st.form_submit_button("Save details"):
                if ui.did(lambda: db.set_settings(conn, my_name=my_name, signature=signature), "save your details"):
                    ui.flash("Details saved.")
                    st.rerun()

    templates = latest_templates(conn)
    choice = st.selectbox("Template", [t.name for t in templates] + ["+ New template"])
    current = next((t for t in templates if t.name == choice), None)
    edit, preview = st.columns(2, gap="large")

    with edit:
        name = st.text_input("Name", current.name if current else "", disabled=current is not None,
                             key=f"name_{choice}")
        subject = st.text_input("Subject", current.subject if current else "", key=f"subject_{choice}")
        body = st.text_area("Body", current.body if current else "", height=560, key=f"body_{choice}")
        st.caption("Merge fields: " + " ".join(f"`{{{f}}}`" for f in FIELDS)
                   + ". The opt-out line is added automatically above `{signature}`.")
        problems = check_template(subject, body)
        for problem in problems:
            st.error(problem)
        if current:
            st.caption(f"Version {current.version}. Saving a change creates version {current.version + 1}; "
                       "earlier versions are kept for the reply-rate comparison.")
        if st.button("Save template", type="primary", disabled=bool(problems) or not name.strip()):
            try:
                saved = save_template(conn, name, subject, body)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(f"Saved '{saved.name}', version {saved.version}. **Next:** send it to yourself on "
                           "5 Send → Test email; drafts made from older versions can be rebuilt on 4 Review.")
                ui.link("send", "5 Send: test this version", tab="Test email")

    with preview:
        st.subheader("Preview")
        contacts = conn.execute("SELECT * FROM contacts ORDER BY id DESC LIMIT 200").fetchall()
        options = {f"{c['company']} · {c['role']} · {c['email']}": c for c in contacts}
        if not options:
            st.caption("No contacts imported yet, so the preview uses a sample contact.")
            options = {"Sample: Priya Sharma, Example Corp": SAMPLE_CONTACT}
        label = st.selectbox("Preview with", list(options))
        contact = options[label]
        st.session_state.setdefault("personal_line", SAMPLE_LINE)
        personal = st.text_input("Personal line", key="personal_line",
                                 help="W1 writes one per contact. Clear it to see the email without one.")
        st.button("Ask W1 for this contact's personal line", on_click=_ask_w1,
                  args=(contact, label, settings.get("my_name", "")))
        _show_w1_result(label)
        email = merge(Template(name=name or "draft", subject=subject, body=body),
                      values_for(contact, settings, personal))
        if not email.ok:
            for error in email.errors:
                st.warning(error)
            return
        st.text(f"Subject: {email.subject}")
        as_text, as_html = st.tabs(["Plain text (what gets sent)", "HTML"])
        as_text.code(email.body_text, language=None, wrap_lines=True)
        as_html.html(email.body_html)
