"""5 Send: resume, test emails, real batches, and the batch list."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from mailmate import config, db, followups, selection, sending
from mailmate.config import ConfigError, n8n_settings, test_address
from mailmate.n8n import N8nClient
from mailmate.sending import SendError
from mailmate.ui import common as ui


SEND_TABS = ["Resume", "Test email", "Real sending", "Batches"]


def page_send():
    conn = ui.conn()
    ui.header("5 · Send", "In order: your resume → a test email to yourself for each template version → real emails "
              "to recruiters → afterwards, 6 Replies → Sync to confirm they went out.")
    ui.sync_reminder()
    try:
        test_to = test_address()
    except ConfigError as exc:
        st.error(ui.plain(exc))
        return
    settings = db.get_settings(conn)
    needs_test = sending.waiting_for_a_test(conn) or _unchecked_tests()
    default = "Resume" if not config.RESUME_PATH.exists() else "Test email" if needs_test else "Real sending"
    resume_tab, test_tab, real_tab, batches_tab = ui.tabs(SEND_TABS, "send_tab", default=default)
    if resume_tab.open:
        with resume_tab:
            _resume(settings)
    if test_tab.open:
        with test_tab:
            _test_email(test_to, settings)
    if real_tab.open:
        with real_tab:
            _real_sending(test_to, settings)
    if batches_tab.open:
        with batches_tab:
            _batches()


def _unchecked_tests() -> dict[int, str]:
    """{template id: 'name vN'} for versions with a test sent but not yet marked as checked."""
    return {b["template_id"]: f"{b['template_name']} v{b['version']}" for b in sending.test_batches(ui.conn())
            if b["status"] == "submitted" and not b["test_checked_at"]}


def _resume(settings: dict[str, str]) -> None:
    if config.RESUME_PATH.exists():
        size_kb = config.RESUME_PATH.stat().st_size / 1024
        st.success(f"Attached to every email: **{settings.get('resume_name', 'resume.pdf')}** ({size_kb:.0f} KB). "
                   "Upload a new PDF below only to replace it.")
    else:
        st.info("Upload your resume (PDF): it is attached to every first email. Nothing can be sent without it.")
    upload = st.file_uploader("Upload a new resume (PDF)" if config.RESUME_PATH.exists() else "Upload your resume (PDF)",
                              type=["pdf"])
    if upload is not None and st.button("Save resume", type="primary"):
        try:
            warnings = sending.save_resume(ui.conn(), upload.getvalue(), config.RESUME_PATH, upload.name)
        except ui.EXPECTED_ERRORS as exc:
            st.error(f"**Resume not saved.** {ui.plain(exc)}")
        else:
            ui.flash(f"Resume saved: {upload.name}. Next: the Test email tab.")
            for warning in warnings:
                ui.flash(warning, "warning")
            ui.goto("send_tab", "Test email")
            st.rerun()


def _test_email(test_to: str, settings: dict[str, str]) -> None:
    conn = ui.conn()
    st.caption(f"A test sends the exact text of a draft or approved email to **{test_to}** only (never to the "
               "recruiter), with your resume attached. Each template version needs one checked test before its "
               "emails can go to recruiters.")
    if not config.RESUME_PATH.exists():
        st.warning("Upload your resume first.")
        st.button("Go to Resume", on_click=ui.goto, args=("send_tab", "Resume"))
        return
    waiting = sending.waiting_for_a_test(conn)
    for version, count in waiting:
        st.warning(f"**{version} needs a test:** {count} approved email(s) use it and can't go to recruiters until "
                   "it's tested. It is pre-selected below.")

    st.subheader("Step A: send a test")
    candidates = sending.testable_emails(conn)
    if not candidates:
        st.info("Create a draft on 4 Review first; a test sends the exact text of a draft or approved email.")
        ui.link("review", "4 Review: create drafts", tab="Create drafts")
    else:
        options = {r["id"]: r for r in candidates}
        wanted = {v for v, _ in waiting}
        first = next((i for i, r in options.items() if f"{r['template_name']} v{r['version']}" in wanted),
                     next(iter(options)))
        chosen = st.multiselect(
            f"Emails to send as a test (1-{sending.MAX_TEST_EMAILS}, same template version)", list(options),
            default=[first], max_selections=sending.MAX_TEST_EMAILS,
            format_func=lambda i: f"#{i} {options[i]['template_name']} v{options[i]['version']} · "
                                  f"{options[i]['company']} · {options[i]['status']}")
        lo, hi = sending.TEST_SPACING_S
        st.caption(f"{lo}-{hi} s apart. Nothing about the original emails changes.")
        if st.button(f"Send {len(chosen)} test email(s) to {test_to}", type="primary", disabled=not chosen):
            _send_test(lambda client: sending.send_test(conn, client, chosen, test_to, config.RESUME_PATH.read_bytes(),
                                                        settings.get("resume_name", "resume.pdf")),
                       "The first email should arrive within a minute.")

    follow_up = followups.follow_up_template(conn)
    with st.expander(f"Test the follow-up template ({follow_up.name} v{follow_up.version})"):
        st.caption("Sent as a reply in your self-test thread, so you see the text and that it lands in the same "
                   "conversation.")
        if st.button(f"Send a test follow-up to {test_to}"):
            _send_test(lambda client: sending.send_follow_up_template_test(
                conn, client, test_to, config.RESUME_PATH.read_bytes(), settings.get("resume_name", "resume.pdf")),
                "It should appear inside your self-test conversation within a minute.")

    st.subheader("Step B: check what arrived")
    unchecked = _unchecked_tests()
    if not unchecked:
        st.caption("Nothing waiting for a check. After a test is sent, its checklist appears here.")
    else:
        template_id = st.selectbox("Template version you checked", list(unchecked), format_func=unchecked.get)
        st.markdown(f"Open **{test_to}** and confirm every point:")
        ticks = [st.checkbox(text, key=f"tick_{template_id}_{i}") for i, text in enumerate((
            "The email arrived (check Spam too) and the text looks right",
            "The resume PDF is attached and opens",
            "In the sending account (Sent folder) it carries the MailMate label",
            "There is no 'This email was sent automatically with n8n' footer",
            "With more than one email: they arrived minutes apart, not all at once",
        ))]
        if st.button(f"Mark {unchecked[template_id]} as test-checked", type="primary", disabled=not all(ticks)):
            if ui.did(lambda: sending.mark_test_checked(conn, template_id), "mark it as test-checked"):
                ui.flash(f"{unchecked[template_id]} is test-checked. Its approved emails can now go to recruiters "
                         "from the Real sending tab.")
                ui.goto("send_tab", "Real sending")
                st.rerun()
        if not all(ticks):
            st.caption("The button unlocks once every box is ticked.")
    batches = sending.test_batches(conn)
    if batches:
        with st.expander(f"Test batches so far ({len(batches)})"):
            st.dataframe(pd.DataFrame([{"Batch": b["id"], "Template": f"{b['template_name']} v{b['version']}",
                                        "Status": b["status"], "Sent at": ui.ist(b["submitted_at"]),
                                        "Test checked": ui.ist(b["test_checked_at"])} for b in batches]),
                         hide_index=True, width="stretch")


def _send_test(send, arrives: str) -> None:
    """Hand a test batch to W2; any problem is shown, and the n8n connection is always closed."""
    client = None
    try:
        client = N8nClient(n8n_settings())
        with st.spinner("Handing the test to W2... (one click is enough)"):
            batch = send(client)
    except ui.EXPECTED_ERRORS as exc:
        st.error(f"**Test not sent.** {ui.plain(exc)}")
    else:
        st.success(f"W2 accepted test batch #{batch.batch_id}. {arrives} **Next:** open your test inbox, then tick "
                   "the checklist in Step B below (it appears after this page refreshes; n8n → Executions shows "
                   "progress).")
    finally:
        if client:
            client.close()


def _send_rules_editor() -> sending.SendRules:
    conn = ui.conn()
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


def _when_to_send(conn, now: datetime, rules: sending.SendRules) -> datetime | None:
    """Now, or a start time W2 waits for (approve in the evening, send the next morning). None = now."""
    room, why = sending.capacity_now(conn, now)
    later = st.radio("When", ["Now", "Schedule for later"], horizontal=True, key="send_when",
                     index=0 if room else 1,
                     help="A scheduled batch is handed to W2 now and W2 waits until the start time. n8n (Docker) "
                          "must be running then; if it isn't, W2 sends nothing late and you can release the batch.")
    if later == "Now":
        if not room:
            st.info(f"**Can't send to recruiters right now:** {why}. Schedule the batch for later instead, or send a "
                    "batch to your own test address (a self-test can go any time).")
        return None
    default = sending.next_window_start(now, rules)
    if default - now < timedelta(minutes=10):
        default = sending.next_window_start(now + timedelta(days=1), rules)
    c1, c2 = st.columns(2)
    day = c1.date_input("Start on", default.date(), format="DD/MM/YYYY", key="send_day")
    hour = c2.time_input("At (IST)", default.time(), step=900, key="send_hour")
    start = datetime.combine(day, hour, sending.IST)
    st.caption(f"Opens {start:%A %d %b} at {start:%H:%M} IST. The window ({rules.window_start:%H:%M}-"
               f"{rules.window_end:%H:%M}), weekdays, that day's cap and the company rules are checked for then.")
    return start


def _real_sending(test_to: str, settings: dict[str, str]) -> None:
    conn = ui.conn()
    rules = _send_rules_editor()
    now = datetime.now(timezone.utc)
    local = now.astimezone(sending.IST)
    sent_today = sending.queued_today(conn, now)
    c1, c2, c3 = st.columns(3)
    c1.metric("Sent today", f"{sent_today} / {rules.daily_cap}")
    c2.metric("India time", f"{local:%a %H:%M}")
    waiting, running = sending.scheduled_batch(conn, now), sending.running_batch(conn, now, rules)
    c3.metric("Batch", f"#{waiting[0]} at {waiting[1].astimezone(sending.IST):%a %H:%M}" if waiting else
              f"#{running[0]} until ~{running[1].astimezone(sending.IST):%H:%M}" if running else "none")
    if waiting:
        st.info(f"Batch #{waiting[0]} is **scheduled for {waiting[1].astimezone(sending.IST):%a %d %b %H:%M} IST**: "
                "W2 holds it until then. Keep Docker (n8n) running and this laptop awake at that time. Other batches "
                "can go as long as they don't overlap it (to cancel it: 5 Send → Batches).")
    if running:
        st.info(f"Batch #{running[0]} is sending until about {running[1].astimezone(sending.IST):%H:%M} IST. Keep "
                "Docker (n8n) running and this laptop awake until then; then sync on 6 Replies.")

    if not config.RESUME_PATH.exists():
        st.warning("Upload your resume first.")
        st.button("Go to Resume", on_click=ui.goto, args=("send_tab", "Resume"), key="to_resume")
        return
    for version, count in sending.waiting_for_a_test(conn):
        st.warning(f"**{count} approved email(s) use {version}, which has no checked test yet**, so they aren't listed "
                   "here (rule 2). Send one of them to your test inbox and check it in the Test email tab.")
        st.button(f"Test {version}", on_click=ui.goto, args=("send_tab", "Test email"), key=f"to_test_{version}")
    candidates = sending.sendable_emails(conn)
    if not candidates:
        st.info("No approved emails ready to send. Approve drafts on 4 Review (and test their template version).")
        ui.link("review", "4 Review: review queue", tab="Review queue")
        return

    start_at = _when_to_send(conn, now, rules)
    at = start_at or now
    room, why = sending.capacity_now(conn, at) if start_at is None or start_at > now else (0, "the start has passed")
    groups: dict[str, list] = {}
    for r in candidates:
        groups.setdefault(f"{r['template_name']} v{r['version']}", []).append(r)
    first_pick = selection.suggest_send(conn, candidates, at, test_to, limit=1)
    default_group = next((g for g, rows in groups.items() if first_pick and any(r["id"] == first_pick[0] for r in rows)),
                         next(iter(groups)))
    group = st.radio("Which emails (a batch holds one template version)", list(groups),
                     index=list(groups).index(default_group), horizontal=True,
                     format_func=lambda g: f"{g} ({len(groups[g])})")
    labels = {r["id"]: f"#{r['id']} " + ui.who(r) for r in groups[group]}
    suggested = selection.suggest_send(conn, groups[group], at, test_to, limit=room)
    chosen = st.multiselect(
        f"Approved {group} emails to send for real", list(labels), default=suggested,
        max_selections=min(sending.MAX_BATCH, rules.daily_cap), format_func=labels.get,
        key=f"real_{group}_{'now' if start_at is None else 'later'}")
    if room:
        st.caption(f"Pre-ticked: {len(suggested)} suggested ({why}): follow-ups first, otherwise one recruiter per "
                   "free company, [Applied] first, then High priority. Change it freely; the rules are checked "
                   "again below.")
    ui.company_blocks_note(at, test_to)
    if not chosen:
        return
    plan = sending.plan_real_batch(conn, chosen, now, test_to, start_at=start_at)
    for problem in plan.problems:
        st.error(problem)
    if not plan.ok:
        st.caption("Untick the emails named above, or pick another time, to go on.")
        return
    who = "your own test address only (self-test: window and weekdays don't apply)" if plan.self_test \
        else f"{len(chosen)} recruiter(s)"
    when = "starting now" if start_at is None else f"starting {start_at:%a %d %b at %H:%M} IST"
    st.warning(f"**This sends real email** to {who}, {when}, {rules.spacing_min_s // 60}-{rules.spacing_max_s // 60} "
               f"min apart, done by about {plan.finishes_by.astimezone(sending.IST):%H:%M} IST. It can't be unsent.")
    typed = st.text_input(f"Type {len(chosen)} to confirm", key=f"confirm_{'-'.join(map(str, chosen))}")
    verb = "Send" if start_at is None else "Schedule"
    if st.button(f"{verb} {len(chosen)} real email(s)", type="primary", disabled=typed.strip() != str(len(chosen))):
        client = None
        try:
            client = N8nClient(n8n_settings())
            with st.spinner("Handing the batch to W2... (one click is enough)"):
                batch = sending.send_real(conn, client, chosen, confirmed=int(typed), test_to=test_to,
                                          resume=config.RESUME_PATH.read_bytes(),
                                          resume_name=settings.get("resume_name", "resume.pdf"), start_at=start_at)
        except ui.EXPECTED_ERRORS as exc:
            st.error(f"**Batch not sent.** {ui.plain(exc)}")
        else:
            if start_at is None:
                st.success(f"W2 accepted batch #{batch.batch_id}; n8n sends the emails one by one (n8n → Executions "
                           f"shows progress). **Next:** keep Docker and this laptop awake until about "
                           f"{plan.finishes_by.astimezone(sending.IST):%H:%M} IST, then 6 Replies → Sync replies now.")
            else:
                st.success(f"W2 accepted batch #{batch.batch_id} and holds it until **{start_at:%a %d %b %H:%M} IST**. "
                           "**Next:** at that time Docker (n8n) must be running and this laptop awake; it's done by "
                           f"about {plan.finishes_by.astimezone(sending.IST):%H:%M}. Replies sync by themselves when "
                           "you open MailMate afterwards.")
        finally:
            if client:
                client.close()


def _batches() -> None:
    conn = ui.conn()
    now = datetime.now(timezone.utc)
    batches = sending.real_batches(conn)
    if not batches:
        st.info("No real batches yet.")
        return
    st.dataframe(pd.DataFrame([{"Batch": b["id"], "Template": f"{b['template_name']} v{b['version']}",
                                "Status": b["status"], "Emails": b["emails"], "Still queued": b["queued"],
                                "Handed to W2": ui.ist(b["submitted_at"]),
                                "Scheduled for": ui.ist(b["scheduled_for"])} for b in batches]),
                 hide_index=True, width="stretch")
    st.caption("'Still queued' = handed to W2 but not yet confirmed by a reply sync (6 Replies).")
    st.subheader("Stop a batch")
    st.markdown("n8n → **Executions** → open the `MailMate W2 Send` execution (*running*, or *waiting* for a scheduled "
                "batch) → **Stop**. Then tell MailMate here. Emails already sent stay sent; the reply sync finds out "
                "which ones went out.")
    submitted = [b["id"] for b in batches if b["status"] == "submitted"]
    if submitted:
        batch_id = st.selectbox("Batch I stopped in n8n", submitted)
        if st.button("I stopped it"):
            if ui.did(lambda: sending.mark_stopped(conn, batch_id), "mark the batch as stopped"):
                ui.flash(f"Batch #{batch_id} marked as stopped. A scheduled batch stopped before its time can be "
                         "released right away (below); otherwise sync on 6 Replies first, to see what went out.")
                st.rerun()
    else:
        st.caption("No batch is sending or scheduled.")
    releasable = sending.releasable_batches(conn, now)
    if releasable:
        st.subheader("Release a batch that sent nothing")
        st.markdown("Its emails go back to *approved*, to send in a new batch. Listed here: failed batches, scheduled "
                    "batches stopped before their time, and batches a sync found nothing of (e.g. n8n was off at the "
                    "start). Look in n8n → Executions first to be sure nothing went out.")
        options = {b["id"]: f"#{b['id']} ({b['status']}, {b['queued']} queued)" for b in releasable}
        batch_id = st.selectbox("Batch", list(options), format_func=options.get)
        sure = st.checkbox("I checked n8n Executions: nothing from this batch was sent")
        if st.button("Release its emails back to approved", disabled=not sure):
            if ui.did(lambda: sending.release_batch(conn, batch_id, nothing_sent_confirmed=sure, reason="checked in n8n"),
                      "release the batch"):
                ui.flash(f"Batch #{batch_id}'s emails are approved again and can go in a new batch.")
                st.rerun()
