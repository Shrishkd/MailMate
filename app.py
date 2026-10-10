"""MailMate app.  Run:  streamlit run app.py

Nothing is sent from here without an explicit approval. Real sending is off by default.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from mailmate import db, followups, review, sending, stats
from mailmate.config import DB_PATH, RESUME_PATH, ConfigError, n8n_settings, test_address
from mailmate.emailcheck import DnsDomainChecker
from mailmate.importer import FIELD_LABELS, SUPPORTED, ImportReport, analyse, job_sheets
from mailmate.n8n import N8nClient, N8nError
from mailmate.review import ReviewError
from mailmate.sending import SendError
from mailmate.sentence_check import PersonalLine, is_job_ad, personalize_checked
from mailmate.sync import apply_sync
from mailmate.templates import (FIELDS, FOLLOW_UP_TEMPLATE, Template, check_template, latest_templates, merge,
                                save_template, values_for)

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
        if row["kind"] == "follow_up":
            st.caption("A follow-up has no personal line; edit its text below, or the Follow-up template.")
        else:
            _personal_line_editor(email_id, row, settings)
        with st.form(f"text_{email_id}"):
            subject = st.text_input("Subject", row["subject"])
            body = st.text_area("Body", row["body_text"], height=420)
            if st.form_submit_button("Save text"):
                review.edit_text(conn, email_id, subject, body)
                st.rerun()


def _personal_line_editor(email_id: int, row, settings: dict[str, str]) -> None:
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


def _follow_ups_due() -> None:
    now = datetime.now(timezone.utc)
    due = followups.due(conn, now)
    days = followups.follow_up_days(conn)
    with st.expander(f"Follow-ups due ({len(due)}): first emails with no reply after {days} days"):
        if result := st.session_state.pop("follow_up_result", None):
            st.success(f"Drafted {result[0]} follow-up(s). They're in the queue below, ready for review.")
            for problem in result[1]:
                st.warning(problem)
        new_days = st.number_input("Follow up after (days)", 2, 30, days)
        if new_days != days:
            followups.set_follow_up_days(conn, int(new_days))
            st.rerun()
        template = followups.follow_up_template(conn)
        st.caption(f"Text: the **{template.name}** template (v{template.version}), editable on the Templates page. "
                   "Each contact gets at most one follow-up, as a reply in the first email's Gmail thread.")
        if not due:
            return
        synced = followups.last_sync(conn)
        if not followups.sync_is_fresh(conn, now):
            st.warning("Sync replies first (Replies page): "
                       + (f"the last sync was at {synced.astimezone(sending.IST):%d %b %H:%M} IST" if synced else
                          "there has been no sync yet") + ". Nobody who already answered should get a nudge.")
            return
        options = {r["id"]: r for r in due}
        chosen = st.multiselect(
            "First emails to follow up", list(options), default=list(options),
            format_func=lambda i: f"{options[i]['company']} · {options[i]['name'] or '-'} · {options[i]['address']} "
                                  f"(sent {options[i]['sent_at'][:10]})")
        if st.button(f"Draft {len(chosen)} follow-up(s)", disabled=not chosen):
            made, problems = 0, []
            for first_id in chosen:
                try:
                    followups.create_follow_up(conn, first_id, now)
                    made += 1
                except ReviewError as exc:
                    problems.append(str(exc))
            st.session_state.follow_up_result = (made, problems)
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
            by_name = {f"{t.name} (v{t.version})": t for t in templates if t.name != FOLLOW_UP_TEMPLATE}
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

    _follow_ups_due()

    if not queue:
        st.info("No drafts yet.")
        return
    checks = {r["id"]: review.check_draft(conn, r["id"]) for r in queue}
    drafts = [r for r in queue if r["status"] == "draft"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Drafts", len(drafts))
    c2.metric("Drafts with problems", sum(not checks[r["id"]].clean for r in drafts))
    c3.metric("Approved", len(queue) - len(drafts))

    if old := review.outdated(conn):
        st.warning(f"{len(old)} email(s) were built from an older template version. Rebuilding keeps each personal "
                   "line, uses the newest version, and puts the email back to draft for approval.")
        if st.button(f"Rebuild {len(old)} email(s) with the newest template"):
            for email_id, template in old:
                review.rebuild_with_template(conn, email_id, template)
            st.rerun()

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

def page_send():
    st.title("Send")
    st.caption("Step 6: test emails to your own secondary inbox only. Real sending to recruiters comes in Step 7.")
    try:
        test_to = test_address()
    except ConfigError as exc:
        st.error(str(exc))
        return
    settings = db.get_settings(conn)

    st.subheader("1. Resume")
    if RESUME_PATH.exists():
        size_kb = RESUME_PATH.stat().st_size / 1024
        st.write(f"Attached to every email: **{settings.get('resume_name', 'resume.pdf')}** ({size_kb:.0f} KB)")
    upload = st.file_uploader("Upload a new resume (PDF)" if RESUME_PATH.exists() else "Upload your resume (PDF)",
                              type=["pdf"])
    if upload is not None and st.button("Save resume"):
        try:
            for warning in sending.save_resume(conn, upload.getvalue(), RESUME_PATH, upload.name):
                st.warning(warning)
        except SendError as exc:
            st.error(str(exc))
        else:
            st.rerun()

    st.subheader("2. Test email")
    candidates = sending.testable_emails(conn)
    if not RESUME_PATH.exists():
        st.info("Upload your resume first.")
    elif not candidates:
        st.info("Create a draft on the Review page first; a test sends the exact text of a draft or approved email.")
    else:
        options = {r["id"]: r for r in candidates}
        chosen = st.multiselect(
            f"Emails to send as a test (1-{sending.MAX_TEST_EMAILS}, same template version)", list(options),
            default=list(options)[:1], max_selections=sending.MAX_TEST_EMAILS,
            format_func=lambda i: f"#{i} {options[i]['template_name']} v{options[i]['version']} · "
                                  f"{options[i]['company']} · {options[i]['status']}")
        lo, hi = sending.TEST_SPACING_S
        st.caption(f"Each goes to **{test_to}** only (not to the recruiter), with your resume attached, "
                   f"{lo}-{hi} s apart. Nothing about the original emails changes.")
        if st.button(f"Send {len(chosen)} test email(s) to {test_to}", type="primary", disabled=not chosen):
            try:
                client = N8nClient(n8n_settings())
            except ConfigError as exc:
                st.error(str(exc))
            else:
                try:
                    with st.spinner("Handing the test to W2... (one click is enough)"):
                        batch = sending.send_test(conn, client, chosen, test_to, RESUME_PATH.read_bytes(),
                                                  settings.get("resume_name", "resume.pdf"))
                    st.success(f"W2 accepted test batch #{batch.batch_id}. The first email should arrive within a "
                               "minute; n8n -> Executions shows progress.")
                except (SendError, N8nError) as exc:
                    st.error(f"Not sent: {exc}")
                finally:
                    client.close()

    if RESUME_PATH.exists():
        follow_up = followups.follow_up_template(conn)
        st.markdown(f"**Follow-up template ({follow_up.name} v{follow_up.version})**: tested as a reply in your "
                    "self-test thread, so you see the text and that it lands in the same conversation.")
        if st.button(f"Send a test follow-up to {test_to}"):
            try:
                client = N8nClient(n8n_settings())
                with st.spinner("Handing the test to W2... (one click is enough)"):
                    batch = sending.send_follow_up_template_test(conn, client, test_to, RESUME_PATH.read_bytes(),
                                                                 settings.get("resume_name", "resume.pdf"))
                client.close()
                st.success(f"W2 accepted test batch #{batch.batch_id}. It should appear inside the Sarvam AI "
                           "conversation within a minute.")
            except (ConfigError, SendError, N8nError) as exc:
                st.error(f"Not sent: {exc}")

    st.subheader("3. Check what arrived")
    batches = sending.test_batches(conn)
    if not batches:
        st.caption("No test batches yet.")
    else:
        st.dataframe(pd.DataFrame([{"Batch": b["id"], "Template": f"{b['template_name']} v{b['version']}",
                                    "Status": b["status"], "Sent at": b["submitted_at"] or "",
                                    "Test checked": b["test_checked_at"] or ""} for b in batches]),
                     hide_index=True, width="stretch")
    unchecked = {b["template_id"]: f"{b['template_name']} v{b['version']}" for b in batches
                 if b["status"] == "submitted" and not b["test_checked_at"]}
    if unchecked:
        template_id = st.selectbox("Template version you checked", list(unchecked), format_func=unchecked.get)
        st.markdown(f"Open **{test_to}** and confirm every point:")
        ticks = [st.checkbox(text, key=f"tick_{template_id}_{i}") for i, text in enumerate((
            "The email arrived (check Spam too) and the text looks right",
            "The resume PDF is attached and opens",
            "In the sending account (Sent folder) it carries the MailMate label",
            "There is no 'This email was sent automatically with n8n' footer",
            "With more than one email: they arrived minutes apart, not all at once",
        ))]
        if st.button(f"Mark {unchecked[template_id]} as test-checked", disabled=not all(ticks)):
            sending.mark_test_checked(conn, template_id)
            st.rerun()

    _real_sending(test_to, settings)


def _send_rules_editor() -> sending.SendRules:
    rules = sending.send_rules(conn)
    with st.expander(f"Rules: at most {rules.daily_cap} a day, {rules.spacing_min_s // 60}-{rules.spacing_max_s // 60} "
                     f"min apart, Mon-Fri {rules.window_start:%H:%M}-{rules.window_end:%H:%M} IST"):
        with st.form("send_rules"):
            c1, c2, c3 = st.columns(3)
            cap = c1.number_input("Daily cap", 1, 50, rules.daily_cap)
            lo = c2.number_input("Min gap (minutes)", 2, 60, rules.spacing_min_s // 60)
            hi = c3.number_input("Max gap (minutes)", 2, 60, rules.spacing_max_s // 60)
            c4, c5 = st.columns(2)
            start = c4.time_input("Window opens (IST)", rules.window_start)
            end = c5.time_input("Window closes (IST)", rules.window_end)
            if st.form_submit_button("Save rules"):
                try:
                    sending.save_send_rules(conn, sending.SendRules(
                        daily_cap=cap, spacing_min_s=lo * 60, spacing_max_s=hi * 60, window_start=start, window_end=end))
                    st.rerun()
                except SendError as exc:
                    st.error(str(exc))
    return rules


def _real_sending(test_to: str, settings: dict[str, str]) -> None:
    st.subheader("4. Real sending")
    rules = _send_rules_editor()
    now = datetime.now(timezone.utc)
    local = now.astimezone(sending.IST)
    sent_today = sending.queued_today(conn, now)
    c1, c2, c3 = st.columns(3)
    c1.metric("Sent today", f"{sent_today} / {rules.daily_cap}")
    c2.metric("India time", f"{local:%a %H:%M}")
    running = sending.running_batch(conn, now, rules)
    c3.metric("Running batch", f"#{running[0]} until ~{running[1].astimezone(sending.IST):%H:%M}" if running else "none")

    candidates = sending.sendable_emails(conn)
    if not RESUME_PATH.exists():
        st.info("Upload your resume first (step 1).")
    elif not candidates:
        st.info("No approved emails from a test-checked template version yet.")
    else:
        options = {r["id"]: r for r in candidates}
        chosen = st.multiselect(
            "Approved emails to send for real", list(options), max_selections=min(sending.MAX_BATCH, rules.daily_cap),
            format_func=lambda i: f"#{i} {options[i]['company']} · {options[i]['name'] or '-'} · {options[i]['email']}")
        if chosen:
            plan = sending.plan_real_batch(conn, chosen, now, test_to)
            for problem in plan.problems:
                st.error(problem)
            if plan.ok:
                who = "your own test address only (self-test: window and weekdays don't apply)" if plan.self_test \
                    else f"{len(chosen)} recruiter(s)"
                st.warning(f"**This sends real email** to {who}, {rules.spacing_min_s // 60}-"
                           f"{rules.spacing_max_s // 60} min apart, done by about "
                           f"{plan.finishes_by.astimezone(sending.IST):%H:%M} IST. It can't be unsent.")
                typed = st.text_input(f"Type {len(chosen)} to confirm", key=f"confirm_{'-'.join(map(str, chosen))}")
                if st.button(f"Send {len(chosen)} real email(s)", type="primary",
                             disabled=typed.strip() != str(len(chosen))):
                    try:
                        client = N8nClient(n8n_settings())
                        with st.spinner("Handing the batch to W2... (one click is enough)"):
                            batch = sending.send_real(conn, client, chosen, confirmed=int(typed), test_to=test_to,
                                                      resume=RESUME_PATH.read_bytes(),
                                                      resume_name=settings.get("resume_name", "resume.pdf"))
                        client.close()
                        st.success(f"W2 accepted batch #{batch.batch_id}. The emails are 'queued'; n8n sends them "
                                   "one by one. n8n -> Executions shows progress.")
                    except (ConfigError, SendError, N8nError) as exc:
                        st.error(str(exc))

    batches = sending.real_batches(conn)
    if not batches:
        return
    st.dataframe(pd.DataFrame([{"Batch": b["id"], "Template": f"{b['template_name']} v{b['version']}",
                                "Status": b["status"], "Emails": b["emails"], "Still queued": b["queued"],
                                "Sent at": b["submitted_at"] or ""} for b in batches]), hide_index=True, width="stretch")
    with st.expander("Stop a running batch, or release a failed one"):
        st.markdown("**To stop a batch that is sending:** n8n → **Executions** → open the running "
                    "`MailMate W2 Send` execution → **Stop**. Then tell MailMate below. Emails already sent stay sent; "
                    "the reply sync (Step 8) finds out which ones went out.")
        submitted = [b["id"] for b in batches if b["status"] == "submitted"]
        if submitted:
            batch_id = st.selectbox("Batch I stopped in n8n", submitted)
            if st.button("I stopped it"):
                sending.mark_stopped(conn, batch_id)
                st.rerun()
        failed = [b["id"] for b in batches if b["status"] == "failed" and b["queued"]]
        if failed:
            st.markdown("**A failed batch whose emails are still 'queued'** may or may not have sent something. "
                        "Open n8n → Executions: if that batch's execution sent nothing, release its emails.")
            batch_id = st.selectbox("Failed batch", failed)
            sure = st.checkbox("I checked n8n Executions: nothing from this batch was sent")
            if st.button("Release its emails back to approved", disabled=not sure):
                sending.release_batch(conn, batch_id, nothing_sent_confirmed=sure, reason="checked in n8n")
                st.rerun()


CATEGORY_LABELS = {"interview_request": "🟢 interview request", "question": "🟡 question",
                   "not_interested": "🔴 not interested", "bounce": "⚫ bounce", "auto_reply": "⚪ auto-reply",
                   "other": "🔵 other"}


def _notes_table(notes) -> pd.DataFrame:
    return pd.DataFrame([{"Company": n.company, "To": n.to, "From": n.from_addr, "When": n.date[:16].replace("T", " "),
                          "Category": CATEGORY_LABELS.get(n.category, n.category),
                          "Sure": f"{n.confidence:.0%}" if n.confidence is not None else "",
                          "Check": "⚠ look at it" if n.flagged else "", "Snippet": n.snippet} for n in notes])


def page_replies():
    st.title("Replies")
    st.caption("Sync reads your MailMate-labelled Gmail threads through W3: emails Gmail shows as sent become "
               "'sent', replies are sorted, and bounces and 'not interested' replies go on the do-not-contact list.")
    try:
        test_to = test_address()
    except ConfigError as exc:
        st.error(str(exc))
        return
    days = st.select_slider("Look back", options=[7, 14, 30, 60], value=30, format_func=lambda d: f"{d} days")
    if st.button("Sync replies now", type="primary"):
        try:
            client = N8nClient(n8n_settings())
            with st.spinner("W3 is reading Gmail and sorting replies (about 10-60 s)..."):
                result = client.sync(days)
            client.close()
            st.session_state.sync_report = apply_sync(conn, result, test_to,
                                                      spacing_max_s=sending.send_rules(conn).spacing_max_s)
        except (ConfigError, N8nError) as exc:
            st.error(f"Sync failed: {exc}")

    if report := st.session_state.get("sync_report"):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Threads read", report.threads)
        c2.metric("Newly sent", len(report.newly_sent))
        c3.metric("New replies", len(report.new_replies))
        c4.metric("Added to do-not-contact", len(report.suppressed))
        if report.new_replies:
            st.subheader("New replies")
            st.dataframe(_notes_table(report.new_replies), hide_index=True, width="stretch")
        if report.suppressed:
            st.info("Now on the do-not-contact list: " + ", ".join(report.suppressed))
        if report.not_found:
            st.warning("Queued long ago but not found in Gmail, so maybe never sent (check n8n → Executions for a "
                       "failed Gmail Send): " + ", ".join(report.not_found))
        if report.test_replies:
            with st.expander(f"Replies to test sends ({len(report.test_replies)}): sorted, but they change nothing"):
                st.dataframe(_notes_table(report.test_replies), hide_index=True, width="stretch")
        if report.unknown_threads:
            with st.expander(f"Labelled threads MailMate didn't send ({len(report.unknown_threads)})"):
                st.write("\n".join(f"- {t}" for t in report.unknown_threads))

    st.subheader("All replies")
    replies = pd.read_sql_query(
        "SELECT c.company, c.email AS recruiter, r.from_addr, r.date, r.category, r.confidence, r.snippet"
        " FROM replies r JOIN emails e ON e.id = r.email_id JOIN contacts c ON c.id = e.contact_id"
        " ORDER BY r.date DESC", conn)
    if replies.empty:
        st.caption("No replies yet.")
    else:
        replies["category"] = replies["category"].map(lambda c: CATEGORY_LABELS.get(c, c))
        st.dataframe(replies, hide_index=True, width="stretch")


# Chart colors (dataviz reference palette): one blue for single-series bars, text in text tokens.
VIZ = {"light": {"bar": "#2a78d6", "text": "#0b0b0b", "muted": "#52514e"},
       "dark": {"bar": "#3987e5", "text": "#ffffff", "muted": "#c3c2b7"}}


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _funnel_chart(f: stats.Funnel, colors: dict[str, str]) -> alt.LayerChart:
    stages = [("Uploaded", f.uploaded), ("Approved", f.approved), ("Sent", f.sent),
              ("Replied", f.replied), ("Positive", f.positive)]
    data = pd.DataFrame([{"Stage": s, "Contacts": n,
                          "Share": f"{n / f.uploaded:.0%} of uploaded" if f.uploaded else "",
                          "Label": f"{n}  ({n / f.uploaded:.0%})" if f.uploaded else str(n)} for s, n in stages])
    order = [s for s, _ in stages]
    base = alt.Chart(data).encode(
        y=alt.Y("Stage:N", sort=order, title=None, axis=alt.Axis(labelColor=colors["muted"], ticks=False, domain=False)),
        # room right of the longest bar, so its label is never cut off
        x=alt.X("Contacts:Q", title=None, axis=None, scale=alt.Scale(domain=[0, max(f.uploaded, 1) * 1.25], nice=False)),
        tooltip=[alt.Tooltip("Stage:N"), alt.Tooltip("Contacts:Q"), alt.Tooltip("Share:N", title="Share")])
    bars = base.mark_bar(color=colors["bar"], cornerRadiusEnd=4, size=18)
    labels = base.mark_text(align="left", dx=6, color=colors["text"]).encode(text="Label:N")
    return (bars + labels).properties(height=200)


def _sends_chart(days: list, colors: dict[str, str]) -> alt.Chart:
    data = pd.DataFrame([{"Day": d.strftime("%d %b"), "Emails": n, "Date": d.strftime("%a %d %b")} for d, n in days])
    return alt.Chart(data).mark_bar(color=colors["bar"], cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=14).encode(
        x=alt.X("Day:N", sort=None, title=None, axis=alt.Axis(labelColor=colors["muted"], labelAngle=0, ticks=False)),
        y=alt.Y("Emails:Q", title=None, axis=alt.Axis(labelColor=colors["muted"], tickMinStep=1, grid=True,
                                                       gridOpacity=0.25, domain=False, ticks=False)),
        tooltip=[alt.Tooltip("Date:N", title="Day"), alt.Tooltip("Emails:Q", title="Emails sent")],
    ).properties(height=180)


def page_dashboard():
    st.title("Dashboard")
    try:
        test_to = test_address()
    except ConfigError as exc:
        st.error(str(exc))
        return
    st.caption("Real sends only, per contact: your own test address and test emails never count. 'Sent' means Gmail "
               "confirmed it (Replies → Sync); 'positive' means an interview request. Rates are out of contacts sent to.")
    colors = VIZ["light" if getattr(st.context.theme, "type", "dark") == "light" else "dark"]
    f = stats.funnel(conn, test_to)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Contacts", f.uploaded)
    c2.metric("Sent", f.sent, help=f"{f.queued} more queued in W2, not yet confirmed by a sync" if f.queued else None)
    c3.metric("Reply rate", _pct(f.rate(f.replied)), help=f"{f.replied} of {f.sent} replied (auto-replies don't count)")
    c4.metric("Interview requests", f.positive, help=f"{_pct(f.rate(f.positive))} of sent")
    c5.metric("Bounce rate", _pct(f.rate(f.bounced)), help=f"{f.bounced} of {f.sent} bounced")

    st.subheader("Funnel")
    st.altair_chart(_funnel_chart(f, colors), width="stretch")
    st.caption("Percentages are of contacts uploaded; the rates above are of contacts sent to.")
    if f.queued:
        st.caption(f"{f.queued} email(s) are queued in W2 and count as sent after the next reply sync.")

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("By template version")
        rows = stats.per_template(conn, test_to)
        if rows:
            st.dataframe(pd.DataFrame([{"Template": r.template, "Sent": r.sent, "Replied": r.replied,
                                        "Reply rate": _pct(r.reply_rate), "Interview requests": r.positive,
                                        "Bounced": r.bounced, "Replied after follow-up": r.replied_after_follow_up}
                                       for r in rows]), hide_index=True, width="stretch")
            st.caption("A reply counts for the version of the first email. 'After follow-up' = the first real "
                       "reply came after the follow-up went out.")
        else:
            st.caption("Nothing sent yet.")
    with right:
        st.subheader("Replies by category")
        categories = stats.reply_categories(conn, test_to)
        if categories:
            st.dataframe(pd.DataFrame([{"Category": CATEGORY_LABELS.get(k, k), "Replies": v}
                                       for k, v in categories.items()]), hide_index=True, width="stretch")
        else:
            st.caption("No replies yet.")

    st.subheader("Emails sent per day (last 14 days, India time)")
    days = stats.sends_per_day(conn, test_to)
    st.altair_chart(_sends_chart(days, colors), width="stretch")
    with st.expander("As a table"):
        st.dataframe(pd.DataFrame([{"Day": d.isoformat(), "Emails sent": n} for d, n in days]),
                     hide_index=True, width="stretch")

    st.subheader("Recent replies")
    recent = stats.recent_replies(conn, test_to)
    if recent:
        st.dataframe(pd.DataFrame([{"Company": r["company"], "From": r["email"], "When": r["date"][:16].replace("T", " "),
                                    "Category": CATEGORY_LABELS.get(r["category"], r["category"]),
                                    "Snippet": r["snippet"]} for r in recent]), hide_index=True, width="stretch")
    else:
        st.caption("No replies yet.")


contact_count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
draft_count = conn.execute("SELECT COUNT(*) FROM emails WHERE status = 'draft'").fetchone()[0]
nav = st.navigation([
    st.Page(page_upload, title="Upload", icon="📤", default=True),
    st.Page(page_contacts, title=f"Contacts ({contact_count})", icon="👥"),
    st.Page(page_templates, title="Templates", icon="📝"),
    st.Page(page_review, title=f"Review ({draft_count})", icon="✅"),
    st.Page(page_send, title="Send", icon="📨"),
    st.Page(page_replies, title="Replies", icon="💬"),
    st.Page(page_dashboard, title="Dashboard", icon="📊"),
])
nav.run()
conn.close()
