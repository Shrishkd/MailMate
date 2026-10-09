"""MailMate app.  Run:  streamlit run app.py

Nothing is sent from here without an explicit approval. Real sending is off by default.
"""

import hashlib
from pathlib import Path

import pandas as pd
import streamlit as st

from mailmate import db, review
from mailmate.config import DB_PATH, ConfigError, n8n_settings
from mailmate.emailcheck import DnsDomainChecker
from mailmate.importer import FIELD_LABELS, SUPPORTED, ImportReport, analyse, job_sheets
from mailmate.n8n import N8nClient, N8nError
from mailmate.review import ReviewError
from mailmate.sentence_check import PersonalLine, is_job_ad, personalize_checked
from mailmate.templates import FIELDS, Template, check_template, latest_templates, merge, save_template, values_for

st.set_page_config(page_title="MailMate", page_icon="✉️", layout="wide")
conn = db.connect(DB_PATH)


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


def page_upload():
    st.title("Upload a job list")
    st.caption("A .csv, .xlsx or Word table with at least **company** and **recruiter email** (optional: name, "
               "their title, target role, requirements, job URL, email status, outreach status). Title rows above "
               "the header are fine. Nothing is saved until you click Import.")
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
    problems = [r for r in report.rejected if not _is_duplicate(r)]
    warned = [r for r in report.valid if r.warnings]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Rows read", len(report.rows))
    c2.metric("Will be imported", len(report.valid))
    c3.metric("Rejected", len(problems))
    c4.metric("Duplicates", len(duplicates))
    c5.metric("Blank or note rows", report.blank_rows)

    if problems:
        st.subheader(f"Rejected ({len(problems)})")
        st.caption("These rows won't be imported. Fix them in your file and upload it again.")
        rejected = _rows_table(problems, with_problems=True)
        st.dataframe(rejected, hide_index=True, width="stretch")
        st.download_button("Download the rejected rows (CSV)", rejected.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"{Path(report.filename).stem}_rejected.csv", mime="text/csv")
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
        for email in report.contacted_elsewhere:
            db.suppress(conn, email, "manual", note=f"contacted outside MailMate ({report.filename})")
        added, skipped = db.add_contacts(conn, [r.contact for r in report.valid], report.filename)
        if default_role.strip():
            db.set_settings(conn, default_role=default_role)
        st.session_state.pop("report_key", None)
        st.success(f"Imported {added} contacts.")
        if skipped:
            st.warning(f"{len(skipped)} skipped because they became contacts or were put on the "
                       f"do-not-contact list meanwhile: {', '.join(skipped)}")


def page_contacts():
    st.title("Contacts")
    contacts = pd.read_sql_query(
        "SELECT company, role, email, name, title, status, source_file, created_at FROM contacts ORDER BY id DESC", conn)
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


def _draft_emails(contacts, template: Template, my_name: str) -> None:
    """W1 + checks for each contact, then a stored draft. One contact's failure doesn't stop the
    rest, but two W1 failures in a row do: n8n is probably unreachable."""
    try:
        client = N8nClient(n8n_settings())
    except ConfigError as exc:
        st.error(str(exc))
        return
    progress = st.progress(0.0)
    made, problems, w1_failures = 0, [], 0
    try:
        for i, contact in enumerate(contacts):
            progress.progress(i / len(contacts), text=f"{i + 1} of {len(contacts)}: researching {contact['company']}...")
            try:
                line = personalize_checked(client.personalize, contact, my_name=my_name)
                w1_failures = 0
            except N8nError as exc:
                w1_failures += 1
                if w1_failures == 2:
                    problems.append(f"stopped: W1 failed twice in a row ({exc})")
                    break
                problems.append(f"{contact['email']}: W1 failed ({exc}); drafted without a personal line")
                line = PersonalLine()
            try:
                review.create_draft(conn, contact["id"], template, line)
                made += 1
            except ReviewError as exc:
                problems.append(str(exc))
    finally:
        client.close()
        progress.empty()
    st.session_state.draft_result = (made, problems)


def _review_actions(email_id: int, row, check: review.DraftCheck) -> None:
    try:
        if row["status"] == "approved":
            st.success("Approved. It waits for a send batch (Step 6/7); nothing is sent yet.")
            if st.button("Unapprove", key=f"unapprove_{email_id}"):
                review.unapprove(conn, email_id)
                st.rerun()
            return
        if check.blocking:
            st.error("This email can't be approved. Reject it.")
        elif check.problems:
            override = st.checkbox("Approve anyway: I've read every problem above", key=f"override_{email_id}")
            reason = st.text_input("Why is it OK?", key=f"reason_{email_id}", disabled=not override)
            if st.button("Approve with override", type="primary", key=f"approve_o_{email_id}",
                         disabled=not (override and reason.strip())):
                review.approve(conn, email_id, override_reason=reason)
                st.rerun()
        elif st.button("Approve", type="primary", key=f"approve_{email_id}"):
            review.approve(conn, email_id)
            st.rerun()
        c1, c2 = st.columns(2)
        if c1.button("Reject (draft again later)", key=f"reject_{email_id}"):
            review.reject(conn, email_id)
            st.rerun()
        if c2.button("Reject and never contact", key=f"never_{email_id}"):
            review.reject(conn, email_id, never_contact=True)
            st.rerun()
    except ReviewError as exc:
        st.error(str(exc))


def _review_edit(email_id: int, row, settings: dict[str, str]) -> None:
    with st.expander("Edit"):
        with st.form(f"line_{email_id}"):
            sentence = st.text_input("Personal line", row["sentence"],
                                     help="Saving rebuilds the email from the template (hand edits are replaced) "
                                          "and runs the checks again. Leave empty for no personal line.")
            if st.form_submit_button("Save personal line"):
                review.set_personal_line(conn, email_id, sentence)
                st.rerun()
        if st.button("Ask W1 for a new personal line", key=f"w1_{email_id}"):
            contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (row["contact_id"],)).fetchone()
            client = None
            try:
                client = N8nClient(n8n_settings())
                with st.spinner(f"W1 is researching {row['company']}..."):
                    line = personalize_checked(client.personalize, contact, my_name=settings.get("my_name", ""))
            except (ConfigError, N8nError) as exc:
                st.error(f"W1 failed: {exc}")
            else:
                if line.sentence:
                    review.set_personal_line(conn, email_id, line.sentence, line.sources)
                    st.rerun()
                st.warning("No new line passed the checks; the email is unchanged. Reasons: "
                           + " | ".join(p for a in line.attempts for p in a.problems))
            finally:
                if client:
                    client.close()
        with st.form(f"text_{email_id}"):
            subject = st.text_input("Subject", row["subject"])
            body = st.text_area("Body", row["body_text"], height=420)
            if st.form_submit_button("Save text"):
                review.edit_text(conn, email_id, subject, body)
                st.rerun()


def page_review():
    st.title("Review")
    st.caption("Every email needs your approval. Approving doesn't send anything: sending comes in Steps 6-7.")
    settings = db.get_settings(conn)
    if not settings.get("my_name"):
        st.warning("Set your name and signature on the Templates page first.")
        return
    templates = latest_templates(conn)
    if not templates:
        st.warning("Save a template on the Templates page first.")
        return

    queue = conn.execute(
        "SELECT e.id, e.status, c.company, c.email, c.name FROM emails e JOIN contacts c ON c.id = e.contact_id"
        " WHERE e.status IN ('draft', 'approved') ORDER BY e.status = 'approved', e.id").fetchall()

    candidates = review.contacts_to_draft(conn)
    with st.expander(f"Create drafts ({len(candidates)} contacts without an email)", expanded=not queue):
        if result := st.session_state.pop("draft_result", None):
            made, problems = result
            st.success(f"Drafted {made} email(s).")
            for problem in problems:
                st.warning(problem)
        if candidates:
            by_name = {f"{t.name} (v{t.version})": t for t in templates}
            template = by_name[st.selectbox("Template", list(by_name))]
            options = {c["id"]: c for c in candidates}
            chosen = st.multiselect(
                "Contacts (at most 20 per run)", list(options), default=list(options)[:5], max_selections=20,
                format_func=lambda i: f"{options[i]['company']} · {options[i]['name'] or '-'} · {options[i]['email']}")
            st.caption("W1 researches each company (about 5-30 s each); the personal line is checked, retried once, "
                       "or left out. Keep this tab open while it runs.")
            if st.button(f"Draft {len(chosen)} email(s)", type="primary", disabled=not chosen):
                _draft_emails([options[i] for i in chosen], template, settings["my_name"])
                st.rerun()

    if not queue:
        st.info("No drafts yet.")
        return
    checks = {r["id"]: review.check_draft(conn, r["id"]) for r in queue}
    drafts = [r for r in queue if r["status"] == "draft"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Drafts", len(drafts))
    c2.metric("Drafts with problems", sum(not checks[r["id"]].clean for r in drafts))
    c3.metric("Approved", len(queue) - len(drafts))

    reviewed: set = st.session_state.setdefault("reviewed", set())
    allowed, why = review.bulk_approval_status(conn, len(reviewed))
    if st.button("Approve all drafts with no problems", disabled=not allowed or not drafts,
                 help=None if allowed else f"Locked: {why}"):
        try:
            approved, left = review.approve_all_clean(conn, len(reviewed))
            st.success(f"Approved {approved}; {left} with problems left for one-by-one review.")
        except ReviewError as exc:
            st.error(str(exc))
        st.rerun()
    if not allowed:
        st.caption(f"Approve all is locked: {why}.")

    def label(i: int) -> str:
        r = next(q for q in queue if q["id"] == i)
        mark = "approved" if r["status"] == "approved" else ("problems" if not checks[i].clean else "ready")
        return f"#{i} [{mark}] {r['company']} · {r['name'] or '-'} · {r['email']}"

    email_id = st.selectbox("Email", [r["id"] for r in queue], format_func=label)
    reviewed.add(email_id)
    row = conn.execute(
        "SELECT e.*, c.company, c.email AS address, c.name, c.title, t.name AS template_name, t.version"
        " FROM emails e JOIN contacts c ON c.id = e.contact_id JOIN templates t ON t.id = e.template_id"
        " WHERE e.id = ?", (email_id,)).fetchone()
    check = checks[email_id]

    left, right = st.columns([3, 2], gap="large")
    with left:
        who = f"{row['name']} <{row['address']}>" if row["name"] else row["address"]
        st.markdown(f"**To:** {who}" + (f" · *{row['title']}*" if row["title"] else "")
                    + f"  \n**Template:** {row['template_name']} v{row['version']}")
        st.text(f"Subject: {row['subject']}")
        st.code(row["body_text"], language=None, wrap_lines=True)
    with right:
        st.subheader("Checks")
        for p in check.problems:
            (st.error if p.blocking else st.warning)(p.text)
        if check.clean:
            st.success("Every check passes.")
        for note in check.notes:
            st.caption(note)
        sources = review._sources(row)
        if row["sentence"] and sources:
            st.markdown("**Personal line sources**")
            for s in sources:
                st.markdown(f"[S{s.id}] [{s.title}]({s.url})")
        _review_actions(email_id, row, check)
    _review_edit(email_id, row, settings)

contact_count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
draft_count = conn.execute("SELECT COUNT(*) FROM emails WHERE status = 'draft'").fetchone()[0]
nav = st.navigation([
    st.Page(page_upload, title="Upload", icon="📤", default=True),
    st.Page(page_contacts, title=f"Contacts ({contact_count})", icon="👥"),
    st.Page(page_templates, title="Templates", icon="📝"),
    st.Page(page_review, title=f"Review ({draft_count})", icon="✅"),
])
nav.run()
conn.close()
