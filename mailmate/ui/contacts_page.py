"""2 Contacts: everyone imported, their status and next step; the applied checklist; new openings at
the companies I watch; do-not-contact."""

import pandas as pd
import streamlit as st

from mailmate import companies, openings
from mailmate.templates import APPLIED_TEMPLATE
from mailmate.ui import common as ui


def page_contacts():
    ui.header("2 · Contacts", "Everyone you imported, where their email is, and what to do next for them.")
    ui.sync_reminder()
    contacts_tab, applied_tab, openings_tab, blocked_tab = ui.tabs(
        ["Contacts", "Applied checklist", "Openings", "Do-not-contact"], "contacts_tab")
    if contacts_tab.open:
        with contacts_tab:
            _contacts_table()
    if applied_tab.open:
        with applied_tab:
            _applied_checklist()
    if openings_tab.open:
        with openings_tab:
            _openings()
    if blocked_tab.open:
        with blocked_tab:
            blocked = pd.read_sql_query("SELECT email, reason, note, created_at FROM suppression"
                                        " ORDER BY created_at DESC", ui.conn())
            st.caption("Anyone who bounces or says they're not interested is added automatically. "
                       "These addresses are never emailed.")
            if blocked.empty:
                st.info("Empty: nobody has bounced or said no.")
            else:
                st.dataframe(blocked, hide_index=True, width="stretch")


def _contacts_table() -> None:
    conn = ui.conn()
    applied = companies.applied_keys(conn)
    rows = conn.execute(
        "SELECT c.*, (SELECT MIN(e.created_at) FROM emails e WHERE e.contact_id = c.id AND e.kind = 'initial'"
        " AND e.status != 'rejected') AS drafted_at, (SELECT e.status FROM emails e WHERE e.contact_id = c.id"
        " AND e.kind = 'initial' AND e.status != 'rejected' ORDER BY e.id DESC LIMIT 1) AS email_status"
        " FROM contacts c ORDER BY c.id DESC").fetchall()
    if not rows:
        st.info("No contacts yet.")
        ui.link("upload", "1 Upload a job list")
        return
    counts: dict[str, int] = {}
    for r in rows:
        counts[ui.status_key(r)] = counts.get(ui.status_key(r), 0) + 1
    shown = st.pills("Show only", [k for k in ui.STATUS if k in counts],
                     format_func=lambda k: f"{ui.STATUS[k][0]} ({counts[k]})", key="status_filter")
    if shown:
        rows = [r for r in rows if ui.status_key(r) == shown]
    st.caption(f"{len(rows)} contact(s). Click a status above to see only those; click it again for everyone. "
               "Times are India time (DD/MM/YYYY T HH:MM:SS); 'Drafted at' = when the first email was drafted.")
    st.dataframe(pd.DataFrame([{
        "Sl. No.": n, "Company": r["company"], "Role": r["role"], "Email": r["email"],
        "Job link": r["job_url"] or None, "Name": r["name"], "Priority": (r["priority"] or "").title(),
        "Applied": companies.company_key(r["company"]) in applied, "Status": ui.contact_status(r),
        "Next step": ui.STATUS.get(ui.status_key(r), ("", ""))[1],
        "Drafted at": ui.ist(r["drafted_at"]), "Added": ui.ist(r["created_at"]), "Source file": r["source_file"],
    } for n, r in enumerate(rows, 1)]), hide_index=True, width="stretch", column_config={
        "Job link": st.column_config.LinkColumn("Job link", display_text="Open job"),
        "Applied": st.column_config.CheckboxColumn("Applied", help="Applied via the direct job link"),
    })
    with st.expander("What the statuses mean"):
        st.markdown("\n".join(f"- **{label}**: {step}" for label, step in ui.STATUS.values()))


def _applied_checklist() -> None:
    conn = ui.conn()
    st.caption("Tick the companies where you've applied through the job's own link: their recruiters get the "
               f"**{APPLIED_TEMPLATE}** template (labelled **[Applied]**) and go first whenever MailMate suggests "
               "whom to email, ahead of High priority. Still one recruiter per company at a time.")
    items = companies.checklist(conn)
    if not items:
        st.info("No companies yet: upload a job list first.")
        return
    table = pd.DataFrame([{"Applied": i["applied"], "Company": i["company"],
                           "Job link": i["links"][0] if i["links"] else None,
                           "Drafted at": ui.ist(i["drafted_at"]), "Applied at": ui.ist(i["applied_at"])}
                          for i in items])
    # The editor remembers ticks by row position; a fresh key after each save drops stale ticks.
    version = st.session_state.setdefault("checklist_version", 0)
    edited = st.data_editor(table, hide_index=True, width="stretch", key=f"applied_checklist_{version}",
                            disabled=["Company", "Job link", "Drafted at", "Applied at"],
                            column_config={"Applied": st.column_config.CheckboxColumn("Applied"),
                                           "Job link": st.column_config.LinkColumn("Job link", display_text="Open job")})
    ticked = dict(zip(edited["Company"], edited["Applied"]))
    changes = [i["company"] for i in items if bool(ticked.get(i["company"], i["applied"])) != i["applied"]]
    st.caption(f"{sum(i['applied'] for i in items)} of {len(items)} companies ticked (saved)."
               + (f" **{len(changes)} unsaved change(s):** {', '.join(changes)}." if changes else ""))
    if st.button("Save checklist", type="primary" if changes else "secondary", disabled=not changes):
        if ui.did(lambda: companies.save_checklist(conn, ticked), "save the checklist"):
            st.session_state.checklist_version = version + 1
            ui.flash(f"Checklist saved ({len(changes)} change(s)). New drafts for ticked companies use the "
                     f"{APPLIED_TEMPLATE} template.")
            st.rerun()


# --- openings watcher ---------------------------------------------------------------------------------

def _openings() -> None:
    conn = ui.conn()
    if added := openings.watch_from_job_links(conn):
        st.toast(f"Now watching the job board of {', '.join(added)} (from their job links).", icon="🔭")
    boards = openings.boards(conn)
    checked = openings.last_checked(conn)
    st.caption("New openings whose title matches your keywords, on the job boards of companies you watch "
               "(Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, or a plain careers page). Only "
               "public job lists are read; nothing about you is sent anywhere.")
    c1, c2 = st.columns([1, 3], vertical_alignment="center")
    if c1.button("Check for new openings", type="primary", disabled=not boards):
        with st.spinner(f"Reading {len(boards)} job board(s)..."):
            try:
                new, problems = openings.check(conn)
            except ui.EXPECTED_ERRORS as exc:
                st.error(f"**Check failed.** {ui.plain(exc)}")
                return
        ui.flash(f"{len(new)} new opening(s)." if new else "No new openings since the last check.",
                 "success" if new else "info")
        for problem in problems:
            ui.flash(problem, "warning")
        st.rerun()
    c2.caption(f"Watching {len(boards)} board(s). Last check: "
               + (f"{ui.ist(checked.isoformat())} IST." if checked else "never.")
               + ("" if boards else " Add one below, or let MailMate find boards for your companies."))

    new = openings.new_openings(conn)
    st.subheader(f"New openings ({len(new)})")
    if not new:
        st.info("None waiting. Check now and then (Home reminds you every few days).")
    for o in new[:40]:
        with st.container(border=True):
            left, use, drop = st.columns([6, 1.6, 1], vertical_alignment="center")
            left.markdown(f"**{o['company']}** · [{o['title']}]({o['url']})"
                          + (f" · {o['location']}" if o["location"] else "") + f"  \nfound {ui.ist(o['first_seen'])}")
            if use.button("Use as job link", key=f"use_opening_{o['id']}",
                          help="Becomes the job link of this company's contacts that don't have one yet"):
                if ui.did(lambda: _use(o), "use it"):
                    st.rerun()
            if drop.button("Dismiss", key=f"dismiss_opening_{o['id']}"):
                if ui.did(lambda: openings.dismiss(conn, o["id"]), "dismiss it"):
                    st.rerun()
    _watched_boards(boards)
    _watch_a_page()
    _find_boards()
    _keywords()


def _use(opening) -> None:
    n = openings.use_as_job_link(ui.conn(), opening["id"])
    ui.flash(f"{opening['company']}: the job link of {n} contact(s) set to {opening['title']}." if n else
             f"{opening['company']}'s contacts all have a job link already; marked as used.", "toast")


def _watched_boards(boards) -> None:
    if not boards:
        return
    with st.expander(f"Watched boards ({len(boards)})"):
        st.dataframe(pd.DataFrame([{"Company": b["company"], "On": openings.PLATFORMS[b["kind"]], "Board": b["url"],
                                    "Last check": ui.ist(b["last_checked"]), "Jobs on it": b["last_count"],
                                    "Problem": b["last_error"], "From": b["source"]} for b in boards]),
                     hide_index=True, width="stretch",
                     column_config={"Board": st.column_config.LinkColumn("Board", display_text="Open")})
        by_key = {b["company_key"]: b["company"] for b in boards}
        key = st.selectbox("Stop watching", list(by_key), format_func=by_key.get, index=None,
                           placeholder="Pick a company")
        if key and st.button(f"Stop watching {by_key[key]}"):
            if ui.did(lambda: openings.unwatch(ui.conn(), key), "stop watching it"):
                st.rerun()


def _watch_a_page() -> None:
    conn = ui.conn()
    with st.expander("Watch a careers page"):
        known = [r[0] for r in conn.execute("SELECT DISTINCT company FROM contacts ORDER BY company COLLATE NOCASE")]
        with st.form("watch_page", clear_on_submit=True):
            company = st.selectbox("Company", known, index=None, placeholder="Pick a company from your contacts")
            url = st.text_input("Careers page or job board link", placeholder="https://jobs.lever.co/acme")
            if st.form_submit_button("Watch it"):
                if not company or not url.strip():
                    st.error("Pick a company and paste its link first.")
                    return
                try:
                    board = openings.board_for(url)
                    openings.watch(conn, company, board)
                except ui.EXPECTED_ERRORS as exc:
                    st.error(f"**Not added.** {ui.plain(exc)}")
                    return
                ui.flash(f"Watching {company} on {openings.PLATFORMS[board.kind]}."
                         + (" A plain careers page works only if its jobs are ordinary links; if a check finds "
                            "nothing, use the hiring-platform link instead." if board.kind == "page" else ""))
                st.rerun()


def _find_boards() -> None:
    conn = ui.conn()
    with st.expander("Find job boards for my companies"):
        st.caption("Tries likely board names on Greenhouse, Lever and Ashby for every company without a watched board "
                   "(a few hundred quick requests, under a minute). A name can belong to another company, so check "
                   "each find before watching it.")
        if st.button("Search now"):
            with st.spinner("Looking for job boards..."):
                try:
                    st.session_state.found_boards = [f.model_dump() for f in openings.discover(conn)]
                except ui.EXPECTED_ERRORS as exc:
                    st.error(f"**Search failed.** {ui.plain(exc)}")
                    return
        found = st.session_state.get("found_boards")
        if found is None:
            return
        if not found:
            st.info("No boards found under the usual names. Add a careers page by hand above.")
        for i, f in enumerate(found):
            left, right = st.columns([5, 1], vertical_alignment="center")
            left.markdown(f"**{f['company']}**: [{openings.PLATFORMS[f['board']['kind']]} board "
                          f"'{f['board']['token']}']({f['board']['url']}), {f['openings']} job(s). Is it really them?")
            if right.button("Watch", key=f"watch_found_{i}"):
                board = openings.Board(**f["board"])
                if ui.did(lambda: openings.watch(conn, f["company"], board, source="found"), "watch it"):
                    st.session_state.found_boards = [x for x in found if x is not f]
                    st.rerun()


def _keywords() -> None:
    conn = ui.conn()
    with st.expander("Keywords"):
        with st.form("opening_keywords"):
            text = st.text_input("Titles must contain one of (comma-separated, whole words)",
                                 ", ".join(openings.keywords(conn)))
            if st.form_submit_button("Save keywords"):
                if ui.did(lambda: openings.set_keywords(conn, text), "save the keywords"):
                    ui.flash("Keywords saved; they apply from the next check.", "toast")
                    st.rerun()
