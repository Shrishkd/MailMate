"""6 Replies: the reply desk (answers I owe), syncing Gmail through W3, and every reply so far."""

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from mailmate import autosync, db, followups, replydesk, sending
from mailmate.config import ConfigError, n8n_settings, test_address
from mailmate.n8n import N8nClient
from mailmate.sync import apply_sync
from mailmate.ui import common as ui

REPLY_TABS = ["Reply desk", "Sync", "All replies"]


def _notes_table(notes) -> pd.DataFrame:
    return pd.DataFrame([{"Company": n.company, "To": n.to, "From": n.from_addr, "When": n.date[:16].replace("T", " "),
                          "Category": ui.CATEGORY_LABELS.get(n.category, n.category),
                          "Sure": f"{n.confidence:.0%}" if n.confidence is not None else "",
                          "Check": "⚠ look at it" if n.flagged else "", "Snippet": n.snippet} for n in notes])


def page_replies():
    ui.header("6 · Replies", "Recruiters' answers. The reply desk lists the ones you still owe an answer; sync reads "
              "your MailMate-labelled Gmail threads through W3 (it also runs by itself when MailMate opens and the "
              "last sync is over 6 hours old).")
    try:
        test_to = test_address()
    except ConfigError as exc:
        st.error(ui.plain(exc))
        return
    waiting = replydesk.open_replies(ui.conn(), test_to)
    desk_tab, sync_tab, all_tab = ui.tabs(REPLY_TABS, "replies_tab", default="Reply desk" if waiting else "Sync")
    if desk_tab.open:
        with desk_tab:
            _reply_desk(waiting, test_to)
    if sync_tab.open:
        with sync_tab:
            _sync(test_to)
    if all_tab.open:
        with all_tab:
            _all_replies()


# --- the reply desk ----------------------------------------------------------------------------------

def _suggest(reply_id: int, request: dict) -> None:
    """Button callback (runs before the page is drawn, so it can fill the answer box): W4 writes a suggestion.
    The notes are read now, not when the button was drawn, so the latest typing counts."""
    st.session_state[f"w4_error_{reply_id}"] = ""
    request = {**request, "notes": st.session_state.get(f"notes_{reply_id}", "").strip()}
    client = None
    try:
        client = N8nClient(n8n_settings())
        with st.spinner("W4 is writing a suggestion (usually 10-40 s)..."):
            answer = client.write_reply(request)
    except ui.EXPECTED_ERRORS as exc:
        st.session_state[f"w4_error_{reply_id}"] = ui.plain(exc)
        return
    finally:
        if client:
            client.close()
    st.session_state[f"answer_{reply_id}"] = answer.text


def _reply_desk(waiting, test_to: str) -> None:
    conn = ui.conn()
    now = datetime.now(timezone.utc)
    if not waiting:
        st.success("**Nothing to answer.** Every interview request and question has been answered (or needs no "
                   "answer). New ones appear here after a sync.")
        _recently_handled()
        return
    late = [r for r in waiting if replydesk.overdue(r, now)]
    st.caption(f"{len(waiting)} conversation(s) waiting for your answer" + (f", **{len(late)} overdue**" if late else "")
               + ". Answer interview requests within 12 hours and questions within a day. MailMate can suggest an "
               "answer and save it as a Gmail draft in the same conversation; you check it and send it from Gmail.")
    labels = {r["id"]: ("🔴 " if replydesk.overdue(r, now) else "")
              + f"{ui.CATEGORY_LABELS.get(r['category'], r['category'])} · {r['company']} · "
              + f"{r['name'] or r['from_addr']} · {replydesk.age(r, now)}" for r in waiting}
    if st.session_state.get("desk_reply") not in labels:
        st.session_state.desk_reply = next(iter(labels))
    reply_id = st.selectbox("Conversation", list(labels), format_func=labels.get, key="desk_reply",
                            help="Interview requests first, then the oldest. 🔴 = overdue.")
    row = next(r for r in waiting if r["id"] == reply_id)

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown(f"**From:** {row['from_addr']} · **{row['company']}** ({row['role']})  \n"
                    f"**Received:** {ui.ist(row['date'])} IST ({replydesk.age(row, now)})")
        st.code(row["body_text"] or row["snippet"], language=None, wrap_lines=True)
        with st.expander("Your email to them"):
            st.text(f"Subject: {row['my_subject']}")
            st.code(row["my_body"], language=None, wrap_lines=True)
        if row["job_url"]:
            st.markdown(f"[Open the job posting]({row['job_url']})")
    with right:
        st.subheader("Your answer")
        if row["gmail_draft_id"]:
            st.success("Your answer is saved as a **draft in Gmail**, in this conversation. Open Gmail → Drafts, "
                       "read it once more, and send it. Then click **I sent my answer** below.")
        if error := st.session_state.get(f"w4_error_{reply_id}"):
            st.error(f"**No suggestion.** {error} You can still write the answer yourself below.")
        st.text_area("Facts for the answer (optional)", key=f"notes_{reply_id}", height=80,
                             placeholder="e.g. free Tue or Wed after 3 pm IST; notice period 30 days",
                             help="W4 uses only these facts for times, dates and numbers. Anything missing becomes "
                                  "a [placeholder] you fill in.")
        st.button("Suggest an answer (W4)", key=f"suggest_{reply_id}", on_click=_suggest,
                  args=(reply_id, replydesk.write_request(row, db.get_settings(conn))))
        st.session_state.setdefault(f"answer_{reply_id}", row["answer_text"] or "")
        text = st.text_area("Answer (edit freely)", key=f"answer_{reply_id}", height=260)
        problems = replydesk.check_answer(text) if text.strip() else []
        for problem in problems:
            st.warning(problem[:1].upper() + problem[1:] + ".")
        if st.button("Save as a Gmail draft", type="primary", key=f"draft_{reply_id}",
                     disabled=not text.strip() or bool(problems),
                     help="Saves a draft in this Gmail conversation. Nothing is sent: you send it from Gmail."):
            _save_draft(row, text)
        c1, c2 = st.columns(2)
        if c1.button("I sent my answer", key=f"answered_{reply_id}"):
            if ui.did(lambda: replydesk.mark(conn, reply_id, "answered"), "mark it answered"):
                ui.flash(f"Marked as answered: {row['company']}.", "toast")
                st.rerun()
        if c2.button("No answer needed", key=f"no_answer_{reply_id}"):
            if ui.did(lambda: replydesk.mark(conn, reply_id, "no_answer_needed"), "set it aside"):
                ui.flash(f"Set aside: {row['company']} (no answer needed).", "toast")
                st.rerun()
    _recently_handled()


def _save_draft(row, text: str) -> None:
    client = None
    try:
        client = N8nClient(n8n_settings())
        with st.spinner("W4 is saving the draft in Gmail..."):
            draft = client.save_reply_draft(row["gmail_thread_id"], text)
        replydesk.record_draft(ui.conn(), row["id"], text, draft.draft_id)
    except ui.EXPECTED_ERRORS as exc:
        st.error(f"**Draft not saved.** {ui.plain(exc)}")
        return
    finally:
        if client:
            client.close()
    ui.flash(f"Draft saved in Gmail, addressed to {draft.to or row['from_addr']}. Open Gmail → Drafts, check it and "
             "send it; then click 'I sent my answer'.")
    st.rerun()


def _recently_handled() -> None:
    conn = ui.conn()
    done = conn.execute("SELECT r.id, r.handled, r.handled_at, r.category, c.company, c.name FROM replies r"
                        " JOIN emails e ON e.id = r.email_id JOIN contacts c ON c.id = e.contact_id"
                        " WHERE r.handled != '' ORDER BY r.handled_at DESC LIMIT 10").fetchall()
    if not done:
        return
    with st.expander(f"Recently handled ({len(done)})"):
        for r in done:
            c1, c2 = st.columns([5, 1], vertical_alignment="center")
            c1.markdown(f"{'✅ answered' if r['handled'] == 'answered' else '➖ no answer needed'} · **{r['company']}** "
                        f"· {r['name'] or '-'} · {ui.CATEGORY_LABELS.get(r['category'], r['category'])} · "
                        f"{ui.ist(r['handled_at'])}")
            if c2.button("Reopen", key=f"reopen_{r['id']}"):
                if ui.did(lambda: replydesk.reopen(conn, r["id"]), "reopen it"):
                    st.rerun()


# --- sync -----------------------------------------------------------------------------------------

def _sync(test_to: str) -> None:
    conn = ui.conn()
    synced = followups.last_sync(conn)
    st.markdown("**Last sync:** " + (f"{ui.ist(synced.isoformat())} IST" if synced else "never"))
    auto = st.toggle("Sync by itself when MailMate opens and the last sync is over 6 hours old",
                     value=autosync.enabled(conn))
    if auto != autosync.enabled(conn):
        ui.did(lambda: autosync.set_enabled(conn, auto), "change the setting")
    state = autosync.status()
    if state["running_since"]:
        st.info(f"A sync is running in the background (started {state['running_since'].astimezone(sending.IST):%H:%M}"
                " IST). Its result shows on Home when it's done.")
    elif failed := autosync.last_failure(conn):
        st.warning(f"The last sync failed at {failed[0].astimezone(sending.IST):%H:%M} IST: {failed[1][:200]}")
    days = st.select_slider("Look back", options=[7, 14, 30, 60], value=30, format_func=lambda d: f"{d} days")
    if st.button("Sync replies now", type="primary", disabled=bool(state["running_since"])):
        client = None
        try:
            client = N8nClient(n8n_settings())
            with st.spinner("W3 is reading Gmail and sorting replies (usually under a minute, up to a few)..."):
                result = client.sync(days)
            st.session_state.sync_report = apply_sync(conn, result, test_to,
                                                      spacing_max_s=sending.send_rules(conn).spacing_max_s)
        except ui.EXPECTED_ERRORS as exc:
            ui.did(lambda: autosync.record_failure(conn, ui.plain(exc)), "record the failure")
            st.error(f"**Sync failed; nothing changed.** {ui.plain(exc)} Home → System shows whether n8n and W3 are "
                     "up.")
        finally:
            if client:
                client.close()

    if report := st.session_state.get("sync_report"):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Threads read", report.threads)
        c2.metric("Newly sent", len(report.newly_sent))
        c3.metric("New replies", len(report.new_replies))
        c4.metric("Added to do-not-contact", len(report.suppressed))
        st.success("Synced. " + (f"**Next:** answer the {len(report.new_replies)} new reply(ies) in the Reply desk "
                                 "tab. " if report.new_replies else "No new replies. ")
                   + "Follow-ups that are due can now be drafted on 4 Review.")
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


def _all_replies() -> None:
    replies = pd.read_sql_query(
        "SELECT c.company, c.email AS recruiter, r.from_addr, r.date, r.category, r.confidence, r.handled, r.snippet"
        " FROM replies r JOIN emails e ON e.id = r.email_id JOIN contacts c ON c.id = e.contact_id"
        " ORDER BY r.date DESC", ui.conn())
    if replies.empty:
        st.info("No replies yet.")
        return
    replies["category"] = replies["category"].map(lambda c: ui.CATEGORY_LABELS.get(c, c))
    replies["handled"] = replies["handled"].map({"answered": "✅ answered", "no_answer_needed": "➖ no answer needed",
                                                 "": ""})
    st.dataframe(replies, hide_index=True, width="stretch")
