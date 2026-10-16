"""Reply sync in the background: when MailMate opens and the last sync is over 6 hours old.

It runs in its own thread with its own database connection, so Home never waits for it (W3 can take
minutes). One at a time; after a failure it waits an hour before trying again, and the failure is
recorded (event 'sync_failed') so Home can say what went wrong. Turn it off on 6 Replies.
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from mailmate import db, followups, sending
from mailmate.config import N8nSettings
from mailmate.n8n import N8nClient
from mailmate.sync import SyncReport, apply_sync

AFTER = timedelta(hours=6)
RETRY_AFTER_FAILURE = timedelta(hours=1)
LOOK_BACK_DAYS = 30
ACTIVE_DAYS = 45          # nothing sent for real in this long: nothing to sync

_lock = threading.Lock()
_state: dict = {"running_since": None, "finished": None, "summary": "", "error": "", "thread": None}


def enabled(conn) -> bool:
    return db.get_settings(conn).get("auto_sync", "on") != "off"


def set_enabled(conn, on: bool) -> None:
    db.set_settings(conn, auto_sync="on" if on else "off")


def record_failure(conn, error: str) -> None:
    with conn:
        db.log_event(conn, "sync_failed", error=error[:500])


def last_failure(conn) -> tuple[datetime, str] | None:
    """The last failed sync, if no sync has worked since."""
    row = conn.execute("SELECT created_at, detail FROM events WHERE kind = 'sync_failed' AND id > COALESCE("
                       "(SELECT MAX(id) FROM events WHERE kind = 'sync_done'), 0) ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(row["created_at"]), json.loads(row["detail"]).get("error", "")


def running() -> bool:
    return _state["running_since"] is not None


def status() -> dict:
    with _lock:
        return {k: v for k, v in _state.items() if k != "thread"}


def due(conn, now: datetime) -> str:
    """Why a background sync should start now; '' when it shouldn't."""
    if not enabled(conn) or running():
        return ""
    active = conn.execute("SELECT 1 FROM emails WHERE test_mode = 0 AND queued_at >= ? LIMIT 1",
                          ((now - timedelta(days=ACTIVE_DAYS)).isoformat(),)).fetchone()
    if not active:
        return ""
    failed = last_failure(conn)
    if failed and now - failed[0] < RETRY_AFTER_FAILURE:
        return ""
    synced = followups.last_sync(conn)
    if synced is None:
        return "there has been no sync yet"
    if now - synced >= AFTER:
        return f"the last sync was {int((now - synced).total_seconds() // 3600)} h ago"
    return ""


def describe(report: SyncReport) -> str:
    parts = [f"{len(report.newly_sent)} newly confirmed as sent", f"{len(report.new_replies)} new reply(ies)"]
    if report.suppressed:
        parts.append(f"{len(report.suppressed)} added to do-not-contact")
    return ", ".join(parts)


def start(db_path: Path, settings: N8nSettings, test_to: str,
          client_factory: Callable[[N8nSettings], N8nClient] = N8nClient) -> bool:
    """Start a background sync unless one is running. Returns whether it started."""
    with _lock:
        if _state["running_since"] is not None:
            return False
        _state.update(running_since=datetime.now(timezone.utc), summary="", error="")
        thread = threading.Thread(target=_work, args=(db_path, settings, test_to, client_factory), daemon=True,
                                  name="mailmate-autosync")
        _state["thread"] = thread
    thread.start()
    return True


def wait(timeout: float | None = None) -> None:
    """Wait for the background sync to finish (tests)."""
    thread = _state["thread"]
    if thread is not None:
        thread.join(timeout)


def _work(db_path: Path, settings: N8nSettings, test_to: str, client_factory) -> None:
    summary = error = ""
    client = conn = None
    try:
        client = client_factory(settings)
        result = client.sync(LOOK_BACK_DAYS)
        conn = db.connect(db_path)
        summary = describe(apply_sync(conn, result, test_to, spacing_max_s=sending.send_rules(conn).spacing_max_s))
    except Exception as exc:  # noqa: BLE001 - a background thread must never fail silently
        error = str(exc) or type(exc).__name__
        try:
            conn = conn or db.connect(db_path)
            record_failure(conn, error)
        except Exception:  # noqa: BLE001 - the error is still shown on Home from _state
            pass
    finally:
        if client is not None:
            client.close()
        if conn is not None:
            conn.close()
        with _lock:
            _state.update(running_since=None, finished=datetime.now(timezone.utc), summary=summary, error=error)
