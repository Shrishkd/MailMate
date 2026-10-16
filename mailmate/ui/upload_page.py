"""1 Upload: read a job list, show what will be imported and why rows are rejected."""

import hashlib
from pathlib import Path

import pandas as pd
import streamlit as st

from mailmate import companies, db
from mailmate.emailcheck import DnsDomainChecker
from mailmate.importer import FIELD_LABELS, SUPPORTED, ImportReport, analyse, job_sheets
from mailmate.ui import common as ui


def _rows_table(rows, with_problems: bool) -> pd.DataFrame:
    records = []
    for r in rows:
        rec = {"Where": r.where,
               **{FIELD_LABELS[f]: v for f, v in r.raw.items() if not f.startswith("_") and f != "requirements"}}
        if with_problems:
            rec["Problems"] = " · ".join(r.errors)
        if r.warnings:
            rec["Warnings"] = " · ".join(r.warnings)
        records.append(rec)
    return pd.DataFrame(records)


def _is_duplicate(row) -> bool:
    return len(row.errors) == 1 and row.errors[0].startswith("duplicate of ")


def _has_no_email(row) -> bool:
    return row.errors == ["email is missing"]


def page_upload():
    conn = ui.conn()
    ui.header("1 · Upload a job list",
              "A .csv, .xlsx or Word table with at least **company** and **recruiter email** (optional: name, their "
              "title, target role, requirements, job link, Fit/priority, applied, email status). Title rows above the "
              "header are fine. Nothing is saved until you click Import; uploading the same people again never "
              "duplicates them.")
    settings = db.get_settings(conn)
    default_role = st.text_input(
        "Role you're applying for", settings.get("default_role", ""), placeholder="e.g. AI Engineer",
        help="Fills {role} in the email for every row without a 'Target Role' / 'Applying For' value. "
             "A recruiter's own title ('Senior Technical Recruiter') is never used as {role}.")
    uploaded = st.file_uploader("Job list", type=[s.lstrip(".") for s in SUPPORTED])
    if uploaded is None:
        return

    data = uploaded.getvalue()
    sheets = None
    available = job_sheets(uploaded.name, data)
    if available:
        sheets = st.multiselect("Sheets to import", available, default=available,
                                help="Only sheets with a Company and an Email column are listed. The same address "
                                     "in two sheets is imported once.")
    if "dns" not in st.session_state:          # one DNS cache per session: changing a choice doesn't re-check
        st.session_state.dns = DnsDomainChecker()
    key = (hashlib.sha256(data).hexdigest(), tuple(sheets or ()), default_role.strip())
    if st.session_state.get("report_key") != key:
        with st.spinner("Reading the file and checking every email domain..."):
            st.session_state.report = analyse(uploaded.name, data, conn, st.session_state.dns,
                                              sheets=sheets, default_role=default_role)
        st.session_state.report_key = key
    report: ImportReport = st.session_state.report

    if report.file_errors:
        st.error("**This file can't be imported:**\n\n" + "\n".join(f"- {e}" for e in report.file_errors))
        return

    for sheet in report.sheets:
        where = f"**{sheet.name}** (header on row {sheet.header_row})" if sheet.name else "**Columns found**"
        st.write(where + ": " + ", ".join(f"{FIELD_LABELS[f]} ← *{h}*" for f, h in sheet.columns.items()))
        if sheet.ignored_columns:
            st.caption("Ignored columns: " + ", ".join(sheet.ignored_columns))
        for note in sheet.notes:
            st.info(note)
    duplicates = [r for r in report.rejected if _is_duplicate(r)]
    no_email = [r for r in report.rejected if _has_no_email(r)]
    problems = [r for r in report.rejected if not _is_duplicate(r) and not _has_no_email(r)]
    warned = [r for r in report.valid if r.warnings]
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Rows read", len(report.rows))
    c2.metric("Will be imported", len(report.valid))
    c3.metric("Rejected", len(problems))
    c4.metric("No email", len(no_email))
    c5.metric("Duplicates", len(duplicates))
    c6.metric("Blank or note rows", report.blank_rows)
    if report.job_links:
        linked = sum(1 for r in report.valid if r.contact.job_url)
        st.caption(f"{len(report.job_links)} job link(s) read from the file; {linked} of the {len(report.valid)} "
                   "contacts to import have a job link. "
                   f"{len(report.applied_companies)} compan{'y' if len(report.applied_companies) == 1 else 'ies'} "
                   "marked as applied by direct link.")

    if problems:
        st.subheader(f"Rejected ({len(problems)})")
        st.caption("These rows won't be imported. Fix them in your file and upload it again.")
        rejected = _rows_table(problems, with_problems=True)
        st.dataframe(rejected, hide_index=True, width="stretch")
        st.download_button("Download the rejected rows (CSV)", rejected.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"{Path(report.filename).stem}_rejected.csv", mime="text/csv")
    if no_email:
        with st.expander(f"No email address ({len(no_email)}): founders and HR names without a found email"):
            st.caption("Not imported: MailMate needs an address to write to. Find their emails and add them to the "
                       "sheet if you want them included next time.")
            st.dataframe(_rows_table(no_email, with_problems=False), hide_index=True, width="stretch")
    if duplicates:
        with st.expander(f"Duplicates: {len(duplicates)} rows repeat an address from an earlier row"):
            st.dataframe(_rows_table(duplicates, with_problems=True), hide_index=True, width="stretch")
    if warned:
        st.subheader(f"Imported with warnings ({len(warned)})")
        st.dataframe(_rows_table(warned, with_problems=False), hide_index=True, width="stretch")
    if report.valid:
        with st.expander(f"All {len(report.valid)} rows that will be imported"):
            st.dataframe(pd.DataFrame([r.contact.model_dump() for r in report.valid]),
                         hide_index=True, width="stretch")
    if report.contacted_elsewhere:
        st.warning(f"{len(report.contacted_elsewhere)} address(es) you already emailed outside MailMate will go on "
                   "the do-not-contact list, so they are never emailed again: "
                   + ", ".join(report.contacted_elsewhere))

    if st.button(f"Import {len(report.valid)} valid rows", type="primary", disabled=not report.valid):
        try:
            for email in report.contacted_elsewhere:
                db.suppress(conn, email, "manual", note=f"contacted outside MailMate ({report.filename})")
            added, skipped = db.add_contacts(conn, [r.contact for r in report.valid], report.filename)
            for company in report.applied_companies:
                companies.set_applied(conn, company, True, source=report.filename)
            if default_role.strip():
                db.set_settings(conn, default_role=default_role)
        except ui.EXPECTED_ERRORS as exc:
            st.error(f"**Import stopped.** {ui.plain(exc)} Contacts saved before the problem stay; importing the "
                     "file again adds only the missing ones.")
            return
        st.session_state.pop("report_key", None)
        st.success(f"Imported {added} contacts. **Next:** check the applied companies on 2 Contacts, then "
                   "draft emails on 4 Review.")
        if skipped:
            st.warning(f"{len(skipped)} skipped because they became contacts or were put on the "
                       f"do-not-contact list meanwhile: {', '.join(skipped)}")
        ui.link("contacts", "2 Contacts: applied checklist", tab="Applied checklist")
        ui.link("review", "4 Review: create drafts", tab="Create drafts")
    _update_existing(report)


def _update_existing(report: ImportReport) -> None:
    """People already in MailMate are skipped on import; their new job links, priorities and Applied
    marks can still be taken from the file. Only empty fields are filled; nothing is overwritten."""
    conn = ui.conn()
    updates = report.known_updates
    already = companies.applied_keys(conn)
    new_applied = [c for c in report.applied_companies if companies.company_key(c) not in already]
    if not updates and not new_applied:
        return
    st.subheader("Update existing contacts from this file")
    parts = []
    if updates:
        parts.append(f"fill empty job links / priorities for up to {len(updates)} existing contact(s)")
    if new_applied:
        parts.append(f"tick {len(new_applied)} compan{'y' if len(new_applied) == 1 else 'ies'} as applied by direct "
                     f"link: {', '.join(new_applied)}")
    st.caption("This file has: " + "; ".join(parts) + ". Existing values are never overwritten.")
    if st.button("Update existing contacts"):
        try:
            changed = db.fill_contact_blanks(conn, updates, report.filename)
            for company in new_applied:
                companies.set_applied(conn, company, True, source=report.filename)
        except ui.EXPECTED_ERRORS as exc:
            st.error(f"**Couldn't update the existing contacts.** {ui.plain(exc)}")
            return
        st.success(f"Filled {changed} contact(s); {len(new_applied)} compan{'y' if len(new_applied) == 1 else 'ies'} "
                   "added to the applied checklist.")
