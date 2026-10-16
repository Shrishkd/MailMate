"""4 Review: create drafts, follow-ups, and the review queue (approve, edit, reject)."""

from datetime import datetime, timezone

import streamlit as st

from mailmate import db, followups, review, selection, sending
from mailmate.config import ConfigError, n8n_settings, test_address
from mailmate.n8n import N8nClient, N8nError
from mailmate.review import ReviewError
from mailmate.sentence_check import PersonalLine, personalize_checked
from mailmate.templates import APPLIED_TEMPLATE, FOLLOW_UP_TEMPLATE, Template, latest_templates
from mailmate.ui import common as ui


DRAFT_SUGGESTION = 10     # W1 takes 5-30 s per contact, so suggest a batch that drafts in a few minutes


REVIEW_TABS = ["Create drafts", "Follow-ups", "Review queue"]


def page_review():
    conn = ui.conn()
    ui.header("4 · Review", "Draft emails, then read and approve each one. Approving never sends anything: sending "
              "is on 5 Send.")
    settings = db.get_settings(conn)
    if not settings.get("my_name"):
        st.warning("First set your name and signature (they go into every email).")
        ui.link("templates", "3 Templates: your details")
        return
    templates = latest_templates(conn)
    if not templates:
        st.warning("First save a template: it's the text every email is built from.")
        ui.link("templates", "3 Templates")
        return
    try:
        test_to = test_address()
    except ConfigError as exc:
        st.error(ui.plain(exc))
        return

    queue = conn.execute(
        "SELECT e.id, e.status, e.kind, c.company, c.email, c.name, c.priority, t.name AS template_name, t.version"
        " FROM emails e JOIN contacts c ON c.id = e.contact_id JOIN templates t ON t.id = e.template_id"
        " WHERE e.status IN ('draft', 'approved') ORDER BY e.status = 'approved', e.id").fetchall()
    drafts = sum(r["status"] == "draft" for r in queue)
    candidates = review.contacts_to_draft(conn)
    due = followups.due(conn, datetime.now(timezone.utc))
    st.markdown(f"**{len(candidates)}** contact(s) without an email · **{len(due)}** follow-up(s) due · "
                f"**{drafts}** draft(s) to review · **{len(queue) - drafts}** approved, waiting on 5 Send")
    create_tab, follow_tab, queue_tab = ui.tabs(REVIEW_TABS, "review_tab",
                                                default="Review queue" if drafts else "Create drafts")
    if create_tab.open:
        with create_tab:
            _create_drafts(candidates, templates, settings, test_to)
    if follow_tab.open:
        with follow_tab:
            _follow_ups_due()
    if queue_tab.open:
        with queue_tab:
            _review_queue(queue, settings, test_to)


def _create_drafts(candidates, templates, settings: dict[str, str], test_to: str) -> None:
    if result := st.session_state.pop("draft_result", None):
        made, problems = result
        (st.success if made else st.warning)(
            f"Drafted {made} email(s)." + (" **Next:** read them in the Review queue tab." if made else ""))
        for problem in problems:
            st.warning(problem)
    if not candidates:
        st.info("Every contact already has an email. Upload a new job list to get more.")
        ui.link("upload", "1 Upload")
        return
    by_name = {f"{t.name} (v{t.version})": t for t in templates if t.name not in (FOLLOW_UP_TEMPLATE, APPLIED_TEMPLATE)}
    if not by_name:
        st.warning("Save your usual outreach template first.")
        ui.link("templates", "3 Templates")
        return
    template = by_name[st.selectbox("Template for [New] contacts", list(by_name))]
    st.caption(f"**[Applied]** = a company you applied to by direct link: gets the **{APPLIED_TEMPLATE}** template "
               "automatically. **[New]** = everyone else: gets the template chosen here. **[High]/[Medium]/[Low]** = "
               "priority (Fit) from your sheet.")
    options = {c["id"]: c for c in candidates}
    now = datetime.now(timezone.utc)
    suggested = selection.suggest_drafts(ui.conn(), candidates, now, test_to, limit=DRAFT_SUGGESTION)
    labels = {i: ui.who(c) for i, c in options.items()}
    chosen = st.multiselect(
        "Contacts (at most 20 per run)", list(options), default=suggested, max_selections=20,
        format_func=labels.get)
    if suggested:
        st.caption(f"Pre-ticked: {len(suggested)} suggested, one recruiter per company: [Applied] first, then High, "
                   "Medium, Low, skipping companies that are waiting, closed, or already have a draft. Change it "
                   "freely. W1 researches each company (about 5-30 s each); keep this tab open while it runs.")
    else:
        st.info("Nothing pre-ticked: every company left is waiting on an earlier email (6 days) or already has an "
                "email in the queue, so its other recruiters wait their turn. New companies from your next upload "
                "are suggested right away. You can still tick someone by hand.")
    ui.company_blocks_note(now, test_to)
    if st.button(f"Draft {len(chosen)} email(s)", type="primary", disabled=not chosen):
        _draft_emails([options[i] for i in chosen], template, settings["my_name"])
        if st.session_state.get("draft_result", (0,))[0]:
            ui.goto("review_tab", "Review queue")
        st.rerun()


def _draft_emails(contacts, template: Template, my_name: str) -> None:
    """W1 + checks for each contact, then a stored draft. One contact's failure doesn't stop the
    rest, but two W1 failures in a row do: n8n is probably unreachable."""
    conn = ui.conn()
    try:
        client = N8nClient(n8n_settings())
    except ConfigError as exc:
        st.session_state.draft_result = (0, [ui.plain(exc)])
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
                review.create_draft(conn, contact["id"], review.template_for(conn, contact, template), line)
                made += 1
            except ReviewError as exc:
                problems.append(str(exc))
    finally:
        client.close()
        progress.empty()
    st.session_state.draft_result = (made, problems)


def _follow_ups_due() -> None:
    conn = ui.conn()
    now = datetime.now(timezone.utc)
    due = followups.due(conn, now)
    days = followups.follow_up_days(conn)
    if result := st.session_state.pop("follow_up_result", None):
        st.success(f"Drafted {result[0]} follow-up(s). **Next:** read them in the Review queue tab.")
        for problem in result[1]:
            st.warning(problem)
    template = followups.follow_up_template(conn)
    st.caption(f"**[Follow-up]** = one short nudge to someone who hasn't answered your first email after {days} days, "
               "sent as a reply in that email's Gmail thread. Each contact gets at most one. Text: the "
               f"**{template.name}** template (v{template.version}), editable on 3 Templates.")
    new_days = st.number_input("Follow up after (days)", 2, 30, days)
    if new_days != days:
        if ui.did(lambda: followups.set_follow_up_days(conn, int(new_days)), "change the number of days"):
            st.rerun()
    if not due:
        st.info(f"No follow-ups due: nobody has gone {days} days without answering yet.")
        return
    synced = followups.last_sync(conn)
    if not followups.sync_is_fresh(conn, now):
        st.warning(f"{len(due)} follow-up(s) are due, but sync replies first: "
                   + (f"the last sync was at {synced.astimezone(sending.IST):%d %b %H:%M} IST" if synced else
                      "there has been no sync yet") + ". Nobody who already answered should get a nudge.")
        ui.link("replies", "6 Replies: sync now")
        return
    options = {r["id"]: r for r in due}
    chosen = st.multiselect(
        "First emails to follow up", list(options), default=list(options),
        format_func=lambda i: f"{options[i]['company']} · {options[i]['name'] or '-'} · {options[i]['address']} "
                              f"(sent {options[i]['sent_at'][:10]})")
    if st.button(f"Draft {len(chosen)} follow-up(s)", type="primary", disabled=not chosen):
        made, problems = 0, []
        for first_id in chosen:
            try:
                followups.create_follow_up(conn, first_id, now)
                made += 1
            except ui.EXPECTED_ERRORS as exc:
                problems.append(ui.plain(exc))
        if made:
            ui.goto("review_tab", "Review queue")
            ui.flash(f"Drafted {made} follow-up(s). Read and approve them below.")
            for problem in problems:
                ui.flash(problem, "warning")
        else:
            st.session_state.follow_up_result = (0, problems)
        st.rerun()


def _review_queue(queue, settings: dict[str, str], test_to: str) -> None:
    conn = ui.conn()
    if result := st.session_state.pop("draft_result", None):     # drafting jumps here when it made something
        made, problems = result
        st.success(f"Drafted {made} email(s). Read each one below and approve or reject it.")
        for problem in problems:
            st.warning(problem)
    if not queue:
        st.info("Nothing to review. Create drafts (or follow-ups) in the other tabs.")
        return
    checks = {r["id"]: review.check_draft(conn, r["id"]) for r in queue}
    drafts = [r for r in queue if r["status"] == "draft"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Drafts", len(drafts))
    c2.metric("Drafts with problems", sum(not checks[r["id"]].clean for r in drafts))
    c3.metric("Approved (waiting on 5 Send)", len(queue) - len(drafts))
    st.page_link(ui.pages()["send"], label="5 Send: send the approved ones", icon=":material/arrow_forward:",
                 query_params={"tab": "Real sending"}, disabled=len(queue) == len(drafts))

    if old := review.outdated(conn):
        st.warning(f"{len(old)} email(s) were built from an older template version. Rebuilding keeps each personal "
                   "line, uses the newest version, and puts the email back to draft for approval.")
        if st.button(f"Rebuild {len(old)} email(s) with the newest template"):
            done, failed = 0, []
            for email_id, template in old:
                try:
                    review.rebuild_with_template(conn, email_id, template)
                    done += 1
                except ui.EXPECTED_ERRORS as exc:
                    failed.append(f"#{email_id}: {ui.plain(exc)}")
            ui.flash(f"Rebuilt {done} email(s); read and approve them again.")
            for problem in failed:
                ui.flash(problem, "warning")
            st.rerun()

    reviewed: set = st.session_state.setdefault("reviewed", set())
    allowed, why = review.bulk_approval_status(conn, len(reviewed), test_to)
    if st.button("Approve all drafts with no problems", disabled=not allowed or not drafts,
                 help=None if allowed else f"Locked: {why}"):
        try:
            approved, left = review.approve_all_clean(conn, len(reviewed), test_to)
        except ui.EXPECTED_ERRORS as exc:
            st.error(f"**Couldn't approve them.** {ui.plain(exc)}")
        else:
            ui.flash(f"Approved {approved}; {left} with problems are left for one-by-one review. "
                     "Next: 5 Send → Real sending.", "toast")
            st.rerun()
    st.caption(f"Approve all is locked: {why}." if not allowed else
               "Approve all is unlocked: it approves every draft whose checks all pass.")

    labels = {r["id"]: f"#{r['id']} " + ("✅ approved" if r["status"] == "approved" else
                                         "⚠️ problems" if not checks[r["id"]].clean else "📝 ready")
              + f" · {r['template_name']} v{r['version']} · {ui.who(r)}" for r in queue}
    if st.session_state.get("review_email") not in labels:       # sent, rejected, or first visit
        st.session_state.review_email = next(iter(labels))
    email_id = st.selectbox("Email", list(labels), format_func=labels.get, key="review_email",
                            help="Drafts first, then approved ones. ⚠️ = a check found something to look at. "
                                 "After you approve or reject, the next draft opens here.")
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
        _review_actions(email_id, row, check, queue)
    _review_edit(email_id, row, settings)


def _decide(email_id: int, decision: str, next_id: int | None) -> None:
    """Button callback. It only records the choice (this run's database connection is closed by the
    time callbacks run); carry_out_decision does it before anything is drawn, so the page is drawn
    once, with the next email already in place: no dimmed half-page, nothing moves down."""
    st.session_state.review_decision = (email_id, decision, next_id)


def carry_out_decision() -> None:
    conn = ui.conn()
    pending = st.session_state.pop("review_decision", None)
    if not pending:
        return
    email_id, decision, next_id = pending
    reason = st.session_state.get(f"reason_{email_id}", "")
    action, what, done = {
        "approve": (lambda: review.approve(conn, email_id), "approve it", f"Approved #{email_id}."),
        "override": (lambda: review.approve(conn, email_id, override_reason=reason), "approve it",
                     f"Approved #{email_id} with an override."),
        "unapprove": (lambda: review.unapprove(conn, email_id), "unapprove it", f"#{email_id} is a draft again."),
        "reject": (lambda: review.reject(conn, email_id), "reject it",
                   f"Rejected #{email_id}. The contact is 'new' again and can be drafted later."),
        "never": (lambda: review.reject(conn, email_id, never_contact=True), "reject it",
                  f"Rejected #{email_id}; the address is on the do-not-contact list."),
    }[decision]
    try:
        action()
    except ui.EXPECTED_ERRORS as exc:
        st.session_state.review_error = (email_id, f"**Couldn't {what}.** {ui.plain(exc)}")
        return
    if next_id is not None:
        st.session_state.review_email = next_id
        done += f" Now showing #{next_id}."
    elif decision in ("approve", "override"):
        done += " That was the last draft: next, 5 Send → Real sending."
    ui.flash(done, "toast")


def _next_draft(queue, email_id: int) -> int | None:
    """The email to show once this one is decided: the next draft below it, else the first draft left."""
    drafts = [r["id"] for r in queue if r["status"] == "draft" and r["id"] != email_id]
    later = [i for i in drafts if i > email_id]
    return (later or drafts or [None])[0]


def _review_actions(email_id: int, row, check: review.DraftCheck, queue) -> None:
    error = st.session_state.get("review_error")
    if error and error[0] == email_id:
        st.error(error[1])
        del st.session_state["review_error"]
    if row["status"] == "approved":
        st.success("Approved. It waits on 5 Send → Real sending; nothing is sent yet.")
        st.button("Unapprove", key=f"unapprove_{email_id}", on_click=_decide, args=(email_id, "unapprove", None))
        return
    following = _next_draft(queue, email_id)
    if check.blocking:
        st.error("This email can't be approved (see the red problem above). Reject it, or fix it under Edit.")
    elif check.problems:
        override = st.checkbox("Approve anyway: I've read every problem above", key=f"override_{email_id}")
        reason = st.text_input("Why is it OK?", key=f"reason_{email_id}", disabled=not override)
        st.button("Approve with override", type="primary", key=f"approve_o_{email_id}",
                  disabled=not (override and reason.strip()), on_click=_decide,
                  args=(email_id, "override", following))
    else:
        st.button("Approve", type="primary", key=f"approve_{email_id}", on_click=_decide,
                  args=(email_id, "approve", following))
    c1, c2 = st.columns(2)
    c1.button("Reject (draft again later)", key=f"reject_{email_id}", on_click=_decide,
              args=(email_id, "reject", following))
    c2.button("Reject and never contact", key=f"never_{email_id}", on_click=_decide,
              args=(email_id, "never", following))


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
                if ui.did(lambda: review.edit_text(ui.conn(), email_id, subject, body), "save the text"):
                    ui.flash("Text saved and checked again. If it was approved, approve it again.")
                    st.rerun()


def _personal_line_editor(email_id: int, row, settings: dict[str, str]) -> None:
    conn = ui.conn()
    with st.form(f"line_{email_id}"):
        sentence = st.text_input("Personal line", row["sentence"],
                                 help="Saving rebuilds the email from the template (hand edits are replaced) "
                                      "and runs the checks again. Leave empty for no personal line.")
        if st.form_submit_button("Save personal line"):
            if ui.did(lambda: review.set_personal_line(conn, email_id, sentence), "save the personal line"):
                ui.flash("Personal line saved; the email was rebuilt and checked again.")
                st.rerun()
    if st.button("Ask W1 for a new personal line", key=f"w1_{email_id}"):
        contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (row["contact_id"],)).fetchone()
        client = None
        try:
            client = N8nClient(n8n_settings())
            with st.spinner(f"W1 is researching {row['company']}..."):
                line = personalize_checked(client.personalize, contact, my_name=settings.get("my_name", ""))
        except (ConfigError, N8nError) as exc:
            st.error(f"**W1 couldn't write a line.** {ui.plain(exc)} The email is unchanged.")
        else:
            if line.sentence:
                if ui.did(lambda: review.set_personal_line(conn, email_id, line.sentence, line.sources),
                          "save W1's line"):
                    ui.flash("W1's new personal line passed the checks and is in the email.")
                    st.rerun()
            st.warning("No new line passed the checks; the email is unchanged. Reasons: "
                       + " | ".join(p for a in line.attempts for p in a.problems))
        finally:
            if client:
                client.close()
