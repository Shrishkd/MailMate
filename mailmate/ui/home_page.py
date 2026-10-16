"""Home: what to do next, most urgent first."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from mailmate import (autosync, backup, config, db, followups, health, openings, pipeline, replydesk, review,
                      selection, sending)
from mailmate.config import ConfigError, n8n_settings, test_address
from mailmate.templates import APPLIED_TEMPLATE, FOLLOW_UP_TEMPLATE, latest_templates
from mailmate.ui import common as ui


HEALTH_EVERY = timedelta(minutes=10)
OPENINGS_EVERY = timedelta(days=3)


def _next_steps(now: datetime, test_to: str, checks: list[health.Check]) -> list[tuple[str, str | None, str, str | None]]:
    """[(text, page, link label, tab)] in the order to do them: things that can go wrong first."""
    conn = ui.conn()
    steps = [(f"**{c.name}: {c.detail}.** {c.fix}", None, "", None) for c in checks if c.ok is False]
    for r in pipeline.upcoming(conn, now, within=timedelta(hours=36)):
        when = datetime.fromisoformat(r["next_at"]).astimezone(sending.IST)
        steps.append((f"**📅 {when:%a %d %b, %H:%M} IST · {r['company']}:** {r['next_step'] or pipeline.LABELS[r['stage']]}."
                      " Look over your notes and the job posting before it.", "pipeline", "7 Pipeline", None))
    rules = sending.send_rules(conn)
    settings = db.get_settings(conn)
    if running := sending.running_batch(conn, now, rules):
        steps.append((f"**Batch #{running[0]} is sending** until about {running[1].astimezone(sending.IST):%H:%M} IST. "
                      "Keep Docker (n8n) running and this laptop awake; sync after it finishes.",
                      "send", "5 Send: batches", "Batches"))
    if waiting := sending.scheduled_batch(conn, now):
        steps.append((f"**Batch #{waiting[0]} is scheduled** for {waiting[1].astimezone(sending.IST):%a %d %b %H:%M} IST. "
                      "Docker (n8n) must be running and this laptop awake then.", "send", "5 Send: batches", "Batches"))
    count, batches = sending.unconfirmed(conn, now)
    sent_any = conn.execute("SELECT 1 FROM emails WHERE test_mode = 0 AND status != 'draft' AND queued_at IS NOT NULL"
                            " LIMIT 1").fetchone()
    failed = autosync.last_failure(conn)
    if autosync.running():
        started = autosync.status()["running_since"]
        steps.append((f"**Syncing replies in the background** (started {started.astimezone(sending.IST):%H:%M} IST). "
                      "This list updates by itself when it's done.", None, "", None))
    elif failed and count:
        steps.append((f"**The background sync failed** at {failed[0].astimezone(sending.IST):%H:%M} IST: "
                      f"{failed[1][:160]}. See System below, then sync by hand.", "replies", "6 Replies: sync now", None))
    elif count:
        steps.append((f"**Sync replies:** {count} email(s) from batch {', '.join(f'#{b}' for b in batches)} went out "
                      "but aren't confirmed yet.", "replies", "6 Replies: sync now", None))
    elif sent_any and not followups.sync_is_fresh(conn, now):
        steps.append(("**Sync replies:** the last sync was over a day ago, so new replies and bounces aren't in yet.",
                      "replies", "6 Replies: sync now", None))
    if waiting := replydesk.open_replies(conn, test_to):
        late = [r for r in waiting if replydesk.overdue(r, now)]
        interviews = sum(r["category"] == "interview_request" for r in waiting)
        steps.append(((f"**Answer {len(waiting)} recruiter(s)**" + (f", {len(late)} overdue" if late else "")
                       + (f" ({interviews} interview request(s))" if interviews else "") + ": MailMate can suggest "
                       "the answer and put it in Gmail as a draft."), "replies", "6 Replies: reply desk", "Reply desk"))
    for r in pipeline.stale(conn, now):
        steps.append((f"**Update the pipeline:** how did *{r['next_step'] or pipeline.LABELS[r['stage']]}* with "
                      f"{r['company']} go?", "pipeline", "7 Pipeline", None))
    if not conn.execute("SELECT 1 FROM contacts LIMIT 1").fetchone():
        steps.append(("**Upload your job list** (company + recruiter email).", "upload", "1 Upload", None))
    if not settings.get("my_name"):
        steps.append(("**Add your name and signature.**", "templates", "3 Templates", None))
    if not [t for t in latest_templates(conn) if t.name not in (FOLLOW_UP_TEMPLATE, APPLIED_TEMPLATE)]:
        steps.append(("**Write your outreach template.**", "templates", "3 Templates", None))
    if not config.RESUME_PATH.exists():
        steps.append(("**Upload your resume** (attached to every email).", "send", "5 Send: resume", "Resume"))
    for version, n in sending.waiting_for_a_test(conn):
        steps.append((f"**Test {version}:** {n} approved email(s) use it and wait for a checked test before they "
                      "can go to recruiters.", "send", "5 Send: test email", "Test email"))
    unchecked = {f"{b['template_name']} v{b['version']}" for b in sending.test_batches(conn)
                 if b["status"] == "submitted" and not b["test_checked_at"]}
    if unchecked:
        steps.append((f"**Check the test email(s)** of {', '.join(sorted(unchecked))} in {test_to} and tick the "
                      "checklist.", "send", "5 Send: test email", "Test email"))
    drafts = conn.execute("SELECT COUNT(*) FROM emails WHERE status = 'draft'").fetchone()[0]
    if drafts:
        steps.append((f"**Review {drafts} draft(s):** read each and approve or reject.", "review",
                      "4 Review: review queue", "Review queue"))
    if due := followups.due(conn, now):
        steps.append((f"**Draft {len(due)} follow-up(s):** no reply after {followups.follow_up_days(conn)} days.",
                      "review", "4 Review: follow-ups", "Follow-ups"))
    ready = sending.sendable_emails(conn)
    if ready:
        room, why = sending.capacity_now(conn, now)
        steps.append(((f"**Send:** {len(ready)} approved email(s) are ready; up to {room} can go now ({why})."
                       if room else f"**Send later:** {len(ready)} approved email(s) are ready, but not now: {why}."),
                      "send", "5 Send: real sending", "Real sending"))
    try:
        candidates = review.contacts_to_draft(conn)
        free = selection.suggest_drafts(conn, candidates, now, test_to, limit=len(candidates)) if candidates else []
    except ui.EXPECTED_ERRORS:
        free = []
    if free:
        steps.append((f"**Draft new emails:** {len(free)} contact(s) at companies free to email (one recruiter per "
                      "company; [Applied] first).", "review", "4 Review: create drafts", "Create drafts"))
    if fresh := openings.new_openings(conn):
        names = list(dict.fromkeys(o["company"] for o in fresh))
        steps.append((f"**{len(fresh)} new opening(s)** at {', '.join(names[:3])}" + (" and others" if len(names) > 3
                      else "") + ": apply, or use one as the job link before emailing.", "contacts",
                      "2 Contacts: openings", "Openings"))
    elif openings.boards(conn):
        checked = openings.last_checked(conn)
        if checked is None or now - checked > OPENINGS_EVERY:
            steps.append(("**Check for new openings** at the companies you watch (last check: "
                          + (f"{(now - checked).days} days ago" if checked else "never") + ").", "contacts",
                          "2 Contacts: openings", "Openings"))
    return steps


def _backups() -> None:
    folder = backup.folder_for(config.DB_PATH)
    copies = backup.backups(folder)
    if problem := st.session_state.get("backup_problem"):
        st.warning(f"**{problem}** MailMate works as usual; make a copy by hand below, and check that the disk "
                   "isn't full.")
    with st.expander(f"Backups ({len(copies)}): " + (f"latest {ui.ist(_mtime(copies[0]))} IST" if copies else "none yet")):
        st.caption(f"The first time MailMate opens each day it copies the database to `{folder}`; the last "
                   f"{backup.KEEP_DAILY} daily copies are kept. Copies you make here are kept until you delete them.")
        if st.button("Back up now"):
            if ui.did(lambda: backup.backup_now(ui.conn(), folder, datetime.now(sending.IST)), "make a copy"):
                ui.flash("Copy made.", "toast")
                st.rerun()
        if copies:
            st.dataframe(pd.DataFrame([{"Copy": p.name, "Made": ui.ist(_mtime(p)), "Size": f"{p.stat().st_size / 1024:.0f} KB"}
                                       for p in copies]), hide_index=True, width="stretch")
            st.markdown("**To restore a copy:** stop MailMate (Ctrl+C in its terminal), rename `data/mailmate.db` to "
                        "`data/mailmate-broken.db`, copy the backup you want to `data/mailmate.db`, then "
                        "`streamlit run app.py` again.")


def _mtime(path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _health(now: datetime) -> list[health.Check]:
    """n8n, the workflows and Gmail, checked at most every 10 minutes (each check is a quick GET)."""
    cached = st.session_state.get("health")
    if cached and now - cached[0] < HEALTH_EVERY:
        return cached[1]
    conn = ui.conn()
    failure = autosync.last_failure(conn)
    checks = health.check_all(n8n_settings(), followups.last_sync(conn), now, failure[1] if failure else "")
    st.session_state.health = (now, checks)
    return checks


@st.fragment(run_every=5)
def _wait_for_background_sync() -> None:
    """While a background sync runs, look every 5 s; when it's done, redraw Home with the new state."""
    if not autosync.running():
        st.rerun()


def _announce_background_sync() -> None:
    state = autosync.status()
    if state["finished"] and st.session_state.get("autosync_seen") != state["finished"]:
        st.session_state.autosync_seen = state["finished"]
        if state["error"]:
            st.toast(f"The background sync failed: {state['error'][:150]}", icon="⚠️", duration="long")
        else:
            st.toast(f"Replies synced in the background: {state['summary']}.", icon="✅", duration="long")
            st.session_state.pop("health", None)          # Gmail just worked: check again


def _system(checks: list[health.Check]) -> None:
    bad = [c for c in checks if c.ok is False]
    icon = {True: "✅", False: "❌", None: "❔"}
    with st.expander("System: n8n, workflows, Gmail" + (f" ({len(bad)} problem(s))" if bad else " (all good)"
                                                          if all(c.ok for c in checks) else "")):
        for c in checks:
            st.markdown(f"{icon[c.ok]} **{c.name}**: {c.detail}" + (f"  \n{c.fix}" if c.fix else ""))
        st.caption("Checked without running any workflow. Replies sync by themselves when MailMate opens and the "
                   "last sync is over 6 hours old (switch that off on 6 Replies).")
        if st.button("Check again"):
            st.session_state.pop("health", None)
            st.rerun()


def page_home():
    conn = ui.conn()
    ui.header("MailMate: what to do next", "Your next steps, most urgent first. Each button opens the right page "
              "and tab. Nothing is ever sent without your approval and a typed confirmation.")
    now = datetime.now(timezone.utc)
    try:
        test_to = test_address()
        n8n_settings()
    except ConfigError as exc:
        st.error(f"**Setup problem:** {exc}. Fix it in the .env file next to app.py (see README → Setup), then "
                 "reload this page.")
        return
    if reason := autosync.due(conn, now):
        if autosync.start(config.DB_PATH, n8n_settings(), test_to):
            st.toast(f"Syncing replies in the background: {reason}.", icon="🔄")
    _announce_background_sync()
    checks = _health(now)
    steps = _next_steps(now, test_to, checks)
    if not steps:
        st.success("**All caught up.** Nothing needs you right now: replies arrive in Gmail; sync on 6 Replies "
                   "now and then, and upload a new job list when you have one.")
    for n, (text, page, label, tab) in enumerate(steps, 1):
        with st.container(border=True):
            left, right = st.columns([4, 1.4], vertical_alignment="center")
            left.markdown(f"{'👉 ' if n == 1 else ''}{n}. {text}")
            if page:
                with right:
                    ui.link(page, label, tab)
    if autosync.running():
        _wait_for_background_sync()

    st.subheader("Where things stand")
    rows = conn.execute(
        "SELECT c.status, (SELECT e.status FROM emails e WHERE e.contact_id = c.id AND e.kind = 'initial'"
        " AND e.status != 'rejected' ORDER BY e.id DESC LIMIT 1) AS email_status FROM contacts c").fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        counts[ui.status_key(r)] = counts.get(ui.status_key(r), 0) + 1
    shown = [k for k in ui.STATUS if counts.get(k)]
    if shown:
        for chunk in range(0, len(shown), 5):
            cols = st.columns(5)
            for col, k in zip(cols, shown[chunk:chunk + 5]):
                col.metric(ui.STATUS[k][0], counts[k])
        ui.link("contacts", "2 Contacts: everyone, with their next step")
    synced = followups.last_sync(conn)
    rules = sending.send_rules(conn)
    st.caption(f"Last reply sync: {ui.ist(synced.isoformat()) + ' IST' if synced else 'never'} · Sent today: "
               f"{sending.queued_today(conn, now)} / {rules.daily_cap} · Real emails go Mon-Fri "
               f"{rules.window_start:%H:%M}-{rules.window_end:%H:%M} IST.")

    _system(checks)
    _backups()

    with st.expander("How MailMate works, and what the labels mean"):
        st.markdown(
            "**The flow:** 1 Upload a job list → 2 Contacts (tick companies you applied to) → 3 Templates → "
            "4 Review (draft, read, approve) → 5 Send (test each template version on yourself, then send real "
            "batches) → 6 Replies (answer recruiters; sync runs by itself) → 7 Pipeline (interviews and offers) → "
            "8 Dashboard.\n\n"
            "**Email labels:** **[Applied]** = a company you applied to by direct link (gets the "
            f"*{APPLIED_TEMPLATE}* template) · **[New]** = first email with your usual template · "
            "**[Follow-up]** = the one nudge, in the same Gmail thread · **[High]/[Medium]/[Low]** = Fit from your "
            "sheet.\n\n"
            "**Contact statuses:**\n" + "\n".join(f"- **{label}**: {step}" for label, step in ui.STATUS.values())
            + "\n\n**Safety rules:** one recruiter per company at a time · each template version is tested on "
            "yourself first · at most 20 a day, 3-7 min apart, Mon-Fri in the window · nobody is emailed twice "
            "with a first email · bounces and 'not interested' go on the do-not-contact list.")
