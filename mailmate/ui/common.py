"""Helpers every page uses: the run's database connection, messages, tabs, links, labels."""

import sqlite3
import threading
from datetime import datetime, timezone

import streamlit as st

from mailmate import companies, selection, sending
from mailmate.config import ConfigError
from mailmate.n8n import N8nError
from mailmate.review import ReviewError
from mailmate.sending import SendError
from mailmate.templates import APPLIED_TEMPLATE, FOLLOW_UP_TEMPLATE, ensure_applied_template, latest_templates

# Per script run: the database connection and the page objects. Streamlit runs each browser session's
# script in its own thread, so this is thread-local: two tabs never share a connection.
_run = threading.local()


def start_run(connection: sqlite3.Connection, page_objects: dict | None = None) -> None:
    _run.conn = connection
    _run.pages = page_objects or {}


def set_pages(page_objects: dict) -> None:
    _run.pages = page_objects


def conn() -> sqlite3.Connection:
    return _run.conn


def pages() -> dict:
    return _run.pages


# Problems an action can run into on purpose (a rule says no, n8n is down, .env is incomplete): shown in
# plain words where the button is, never swallowed.
EXPECTED_ERRORS = (ReviewError, SendError, N8nError, ConfigError, ValueError, sqlite3.Error)


def did(action, what: str) -> bool:
    """Run one button's action; on a problem say what didn't happen and why. Returns whether it worked."""
    try:
        action()
    except EXPECTED_ERRORS as exc:
        st.error(f"**Couldn't {what}.** {plain(exc)}")
        return False
    return True


def plain(exc: Exception) -> str:
    if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
        return "The database is busy (another MailMate tab is saving). Wait a moment and try again."
    if isinstance(exc, sqlite3.Error):
        return f"Database problem: {exc}."
    if isinstance(exc, ConfigError):
        return f"Setup problem: {exc}. Fix it in the .env file next to app.py, then reload the page."
    return str(exc)[:1].upper() + str(exc)[1:]


def flash(text: str, kind: str = "success") -> None:
    """A message that survives the st.rerun() after a button: shown once under the next page title.
    kind "toast" pops up in the corner instead and moves nothing on the page."""
    st.session_state.setdefault("flash", []).append((kind, text))


def header(title: str, intro: str) -> None:
    st.title(title)
    st.caption(intro)
    slot = st.container()       # always there, so the page below starts at the same place with or without a message
    for kind, text in st.session_state.pop("flash", []):
        if kind == "toast":
            st.toast(text, icon="✅", duration="long")
        else:
            getattr(slot, kind)(text)


def tabs(labels: list[str], key: str, default: str | None = None):
    """Tabs that remember which one is open; a link (?tab=...) or goto can open a given one.
    Only the open tab's code runs (check .open)."""
    wanted = st.query_params.get("tab")
    if "tab" in st.query_params:
        del st.query_params["tab"]
    goto = st.session_state.pop(f"goto_{key}", None) or wanted
    if goto in labels:
        st.session_state[key] = goto
    elif default in labels and key not in st.session_state:
        # The default applies on arrival only. Passed to st.tabs it would change with the page's state (no
        # drafts left...), and Streamlit would treat the tabs as new ones and jump to another tab.
        st.session_state[key] = default
    return st.tabs(labels, key=key, on_change="rerun")


def goto(key: str, label: str) -> None:
    """Button callback: open another tab of the same page on the next run."""
    st.session_state[f"goto_{key}"] = label


def link(page: str, label: str, tab: str | None = None) -> None:
    """A button to another page, optionally opening one of its tabs."""
    st.page_link(pages()[page], label=label, icon=":material/arrow_forward:",
                 query_params={"tab": tab} if tab else None)


# A contact's status, told by where their first email is ('drafted' alone says too little), and what to do next.
STATUS = {
    "new": ("🆕 new", "Draft an email: 4 Review → Create drafts"),
    "draft": ("📝 draft", "Read and approve it: 4 Review → Review queue"),
    "approved": ("✅ approved (not sent)", "Send it: 5 Send → Real sending"),
    "queued": ("📤 queued (sync to confirm)", "Confirm it went out: 6 Replies → Sync replies now"),
    "failed": ("⚠️ send failed", "Check n8n Executions, then 5 Send → Batches → release"),
    "contacted": ("📨 contacted", "Wait for a reply; a follow-up is offered on 4 Review when due"),
    "outside": ("📨 contacted (outside MailMate)", "Nothing to do here: emailed outside MailMate"),
    "followed_up": ("🔁 followed up", "Wait for a reply; no more emails go to them"),
    "replied": ("💬 replied", "Read it on 6 Replies and answer from Gmail"),
    "bounced": ("⛔ bounced", "Nothing: the address bounced and is on the do-not-contact list"),
}


def status_key(row) -> str:
    if row["status"] == "drafted":
        return row["email_status"] if row["email_status"] in STATUS else "draft"
    if row["status"] == "contacted" and row["email_status"] is None:
        return "outside"          # marked contacted by hand: their MailMate draft was rejected
    return row["status"]


def contact_status(row) -> str:
    key = status_key(row)
    return STATUS[key][0] if key in STATUS else key


def ist(timestamp: str | None) -> str:
    """Stored UTC timestamp -> India time as DD/MM/YYYY T HH:MM:SS."""
    if not timestamp:
        return ""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(sending.IST).strftime("%d/%m/%Y T %H:%M:%S")


def sync_reminder() -> None:
    """Sent emails count as sent only once a reply sync finds them in Gmail; say so when one is due."""
    count, batches = sending.unconfirmed(conn(), datetime.now(timezone.utc))
    if count:
        st.warning(f"{count} email(s) from batch {', '.join(f'#{b}' for b in batches)} went out but aren't "
                   "confirmed yet: open **Replies → Sync replies now**. Until then they show as 'queued (sync to "
                   "confirm)' and don't count on the Dashboard.")


def make_applied_template() -> None:
    """Create 'Applied outreach' as soon as a company is ticked as applied, from my usual template, so
    it can be read, edited and tested before the first draft needs it."""
    if not companies.applied_keys(conn()):
        return
    usual = [t for t in latest_templates(conn()) if t.name not in (FOLLOW_UP_TEMPLATE, APPLIED_TEMPLATE)]
    if usual:
        ensure_applied_template(conn(), usual[0])


def kind_tag(row) -> str:
    """[Follow-up], [Applied] (company I applied to by direct link: the Applied outreach template) or [New]."""
    if "kind" in row.keys() and row["kind"] == "follow_up":
        return "[Follow-up]"
    return "[Applied]" if companies.company_key(row["company"]) in companies.applied_keys(conn()) else "[New]"


def who(row) -> str:
    priority = {"high": "[High] ", "medium": "[Medium] ", "low": "[Low] "}.get(row["priority"] or "", "")
    return f"{kind_tag(row)} {priority}{row['company']} · {row['name'] or '-'} · {row['email']}"


def company_blocks_note(now: datetime, test_to: str) -> None:
    blocks = selection.company_blocks(conn(), now, test_to)
    if blocks:
        with st.expander(f"Companies waiting or closed ({len(blocks)}): nobody else there is emailed for now"):
            st.write("\n".join(f"- **{b.company}**: {b.describe()}" for b in
                               sorted(blocks.values(), key=lambda b: b.company.lower())))


CATEGORY_LABELS = {"interview_request": "🟢 interview request", "question": "🟡 question",
                   "not_interested": "🔴 not interested", "bounce": "⚫ bounce", "auto_reply": "⚪ auto-reply",
                   "other": "🔵 other"}
