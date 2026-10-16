"""Start of every script run: the database connection, the navigation, the last-resort error message."""

import sqlite3
from datetime import datetime

import streamlit as st

from mailmate import backup, config, db, sending
from mailmate.ui import (common as ui, contacts_page, dashboard_page, home_page, pipeline_page, replies_page,
                         review_page, send_page, templates_page, upload_page)

# key -> (page function, title, icon); the key is also the URL path (Home lives at /)
PAGES = {
    "home": (home_page.page_home, "Home: what to do next", "🏠"),
    "upload": (upload_page.page_upload, "1 Upload", "📤"),
    "contacts": (contacts_page.page_contacts, "2 Contacts", "👥"),
    "templates": (templates_page.page_templates, "3 Templates", "📝"),
    "review": (review_page.page_review, "4 Review", "✅"),
    "send": (send_page.page_send, "5 Send", "📨"),
    "replies": (replies_page.page_replies, "6 Replies", "💬"),
    "pipeline": (pipeline_page.page_pipeline, "7 Pipeline", "🧭"),
    "dashboard": (dashboard_page.page_dashboard, "8 Dashboard", "📊"),
}
SECTIONS = {"Start": ["home"], "Prepare": ["upload", "contacts", "templates"], "Email": ["review", "send"],
            "Results": ["replies", "pipeline", "dashboard"]}


def main(only: str | None = None) -> None:
    """One script run. `only` runs that page directly (the page tests use it; the app never does)."""
    st.set_page_config(page_title="MailMate", page_icon="✉️", layout="wide")
    conn = db.connect(config.DB_PATH)
    try:
        ui.start_run(conn)
        _daily_backup(conn)
        review_page.carry_out_decision()       # before anything is drawn, so the counts below are current
        counts = {
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "review": f"{conn.execute('SELECT COUNT(*) FROM emails WHERE status = ?', ('draft',)).fetchone()[0]} drafts",
        }
        pages = {key: st.Page(fn, title=f"{title} ({counts[key]})" if key in counts else title, icon=icon,
                              url_path=key, default=key == "home")
                 for key, (fn, title, icon) in PAGES.items()}
        ui.set_pages(pages)
        nav = st.navigation({section: [pages[k] for k in keys] for section, keys in SECTIONS.items()})
        try:
            PAGES[only][0]() if only else nav.run()
        except Exception as exc:  # noqa: BLE001 - the last safety net: a readable message, never a bare stack trace
            st.error(f"**Something went wrong on this page:** {ui.plain(exc)}\n\nNothing was sent because of it. "
                     "Reload the page and try again; if it keeps happening, the details below say where it broke.")
            with st.expander("Technical details"):
                st.exception(exc)
    finally:
        conn.close()


def _daily_backup(conn) -> None:
    """The first run of the day copies the database. A failed copy never stops the app: Home says so."""
    try:
        backup.daily_backup(conn, backup.folder_for(config.DB_PATH), datetime.now(sending.IST).date())
    except (OSError, sqlite3.Error) as exc:
        st.session_state.backup_problem = f"Today's backup copy failed: {exc}"
    else:
        st.session_state.pop("backup_problem", None)
