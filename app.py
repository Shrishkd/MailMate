"""MailMate app.  Run:  streamlit run app.py

Nothing is sent from here without an explicit approval. Real sending is off by default.
"""

import hashlib
from pathlib import Path

import pandas as pd
import streamlit as st

from mailmate import db
from mailmate.emailcheck import DnsDomainChecker
from mailmate.importer import FIELD_LABELS, SUPPORTED, ImportReport, analyse
from mailmate.templates import FIELDS, Template, check_template, latest_templates, merge, save_template, values_for

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "mailmate.db"

st.set_page_config(page_title="MailMate", page_icon="✉️", layout="wide")
conn = db.connect(DB_PATH)


def _rows_table(rows, with_problems: bool) -> pd.DataFrame:
    records = []
    for r in rows:
        rec = {"Row": r.row, **{FIELD_LABELS[f]: v for f, v in r.raw.items() if f != "requirements"}}
        if with_problems:
            rec["Problems"] = " · ".join(r.errors)
        if r.warnings:
            rec["Warnings"] = " · ".join(r.warnings)
        records.append(rec)
    return pd.DataFrame(records)


def page_upload():
    st.title("Upload a job list")
    st.caption("One row per job: company, role, requirements and recruiter email "
               "(optional: recruiter name, job URL). Nothing is saved until you click Import.")
    uploaded = st.file_uploader("Job list", type=[s.lstrip(".") for s in SUPPORTED])
    if uploaded is None:
        return

    data = uploaded.getvalue()
    key = hashlib.sha256(data).hexdigest()
    if st.session_state.get("report_key") != key:  # DNS checks run once per file, not on every click
        with st.spinner("Reading the file and checking every email domain..."):
            st.session_state.report = analyse(uploaded.name, data, conn, DnsDomainChecker())
        st.session_state.report_key = key
    report: ImportReport = st.session_state.report

    if report.file_errors:
        st.error("**This file can't be imported:**\n\n" + "\n".join(f"- {e}" for e in report.file_errors))
        return

    st.write("**Columns found:** " + ", ".join(f"{FIELD_LABELS[f]} ← *{h}*" for f, h in report.columns.items()))
    if report.ignored_columns:
        st.caption("Ignored columns: " + ", ".join(report.ignored_columns))
    warned = [r for r in report.valid if r.warnings]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows read", len(report.rows))
    c2.metric("Will be imported", len(report.valid))
    c3.metric("Rejected", len(report.rejected))
    c4.metric("Blank rows skipped", report.blank_rows)

    if report.rejected:
        st.subheader(f"Rejected ({len(report.rejected)})")
        st.caption("These rows won't be imported. Fix them in your file and upload it again.")
        rejected = _rows_table(report.rejected, with_problems=True)
        st.dataframe(rejected, hide_index=True, width="stretch")
        st.download_button("Download the rejected rows (CSV)", rejected.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"{Path(report.filename).stem}_rejected.csv", mime="text/csv")
    if warned:
        st.subheader(f"Imported with warnings ({len(warned)})")
        st.dataframe(_rows_table(warned, with_problems=False), hide_index=True, width="stretch")
    if report.valid:
        with st.expander(f"All {len(report.valid)} rows that will be imported"):
            st.dataframe(pd.DataFrame([r.contact.model_dump() for r in report.valid]),
                         hide_index=True, width="stretch")

    if st.button(f"Import {len(report.valid)} valid rows", type="primary", disabled=not report.valid):
        added, skipped = db.add_contacts(conn, [r.contact for r in report.valid], report.filename)
        st.session_state.pop("report_key", None)
        st.success(f"Imported {added} contacts.")
        if skipped:
            st.warning(f"{len(skipped)} skipped because they became contacts or were put on the "
                       f"do-not-contact list meanwhile: {', '.join(skipped)}")


def page_contacts():
    st.title("Contacts")
    contacts = pd.read_sql_query(
        "SELECT company, role, email, name, status, source_file, created_at FROM contacts ORDER BY id DESC", conn)
    if contacts.empty:
        st.info("No contacts yet. Upload a job list first.")
    else:
        st.dataframe(contacts, hide_index=True, width="stretch")
    st.subheader("Do-not-contact list")
    blocked = pd.read_sql_query("SELECT email, reason, note, created_at FROM suppression ORDER BY created_at DESC", conn)
    st.caption("Anyone who bounces or says they're not interested is added automatically. "
               "These addresses are never emailed.")
    if blocked.empty:
        st.write("Empty.")
    else:
        st.dataframe(blocked, hide_index=True, width="stretch")


SAMPLE_CONTACT = {"name": "Priya Sharma", "company": "Example Corp", "role": "Generative AI Engineer"}
SAMPLE_LINE = "I also saw that Example Corp recently launched an AI assistant for home buyers."


def page_templates():
    st.title("Templates")
    settings = db.get_settings(conn)
    with st.expander("Your details: {my_name} and {signature}", expanded=not settings.get("my_name")):
        with st.form("details"):
            my_name = st.text_input("Your name", settings.get("my_name", ""))
            signature = st.text_area("Signature", settings.get("signature", ""), height=130,
                                     help="Replaces {signature}. The opt-out line is added just above it.")
            if st.form_submit_button("Save details"):
                db.set_settings(conn, my_name=my_name, signature=signature)
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
                st.success(f"Saved '{saved.name}', version {saved.version}.")

    with preview:
        st.subheader("Preview")
        contacts = conn.execute("SELECT * FROM contacts ORDER BY id DESC LIMIT 200").fetchall()
        options = {f"{c['company']} · {c['role']} · {c['email']}": c for c in contacts}
        if not options:
            st.caption("No contacts imported yet, so the preview uses a sample contact.")
            options = {"Sample: Priya Sharma, Example Corp": SAMPLE_CONTACT}
        contact = options[st.selectbox("Preview with", list(options))]
        personal = st.text_input("Personal line (from Step 3, W1 writes one per contact)", SAMPLE_LINE,
                                 help="Clear it to see the email without a personal line.")
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


contact_count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
nav = st.navigation([
    st.Page(page_upload, title="Upload", icon="📤", default=True),
    st.Page(page_contacts, title=f"Contacts ({contact_count})", icon="👥"),
    st.Page(page_templates, title="Templates", icon="📝"),
])
nav.run()
conn.close()
