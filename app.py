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


contact_count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
nav = st.navigation([
    st.Page(page_upload, title="Upload", icon="📤", default=True),
    st.Page(page_contacts, title=f"Contacts ({contact_count})", icon="👥"),
])
nav.run()
conn.close()
