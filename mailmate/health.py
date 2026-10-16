"""Is everything MailMate needs up? n8n answering, the three workflows published, Gmail working lately.

Nothing here runs a workflow: n8n's /healthz says whether n8n is up, and a GET to a webhook that only
takes POST tells "published" ("not registered for GET requests") from "not published" ("is not
registered") without starting it. Whether Gmail's sign-in still works only shows when a workflow uses
it, so that check reads the last time a sync or a batch went through.
"""

from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel

from mailmate.config import N8nSettings

WORKFLOWS = {"W1 Personalize": "MailMate-personalize", "W2 Send": "MailMate-send", "W3 Sync": "MailMate-sync"}
GMAIL_QUIET_DAYS = 6     # Google ends n8n's Gmail sign-in after 7 days while the Google app is in "testing"


class Check(BaseModel):
    name: str
    ok: bool | None          # None: can't tell
    detail: str
    fix: str = ""


def _local(settings: N8nSettings) -> bool:
    return settings.base_url.startswith(("http://localhost", "http://127.0.0.1"))


def check_n8n(settings: N8nSettings, client: httpx.Client) -> list[Check]:
    try:
        up = client.get(f"{settings.base_url}/healthz")
        ok = up.status_code == 200
    except Exception as exc:  # noqa: BLE001 - any failure to connect means the same thing to me
        ok, up = False, exc
    if not ok:
        fix = ("Start Docker Desktop, then in PowerShell: `docker start n8n` (wait ~20 s)." if _local(settings) else
               "Check your internet connection and that the n8n Cloud instance is running.")
        reason = f"HTTP {up.status_code}" if isinstance(up, httpx.Response) else type(up).__name__
        return [Check(name="n8n", ok=False, detail=f"no answer from {settings.base_url} ({reason})", fix=fix)]
    checks = [Check(name="n8n", ok=True, detail=f"running at {settings.base_url}")]
    for name, path in WORKFLOWS.items():
        try:
            text = client.get(f"{settings.base_url}/webhook/{path}").text
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(name=name, ok=None, detail=f"couldn't ask ({type(exc).__name__})"))
            continue
        if "not registered for GET" in text or "Did you mean to make a POST" in text:
            checks.append(Check(name=name, ok=True, detail="published"))
        elif "is not registered" in text:
            checks.append(Check(name=name, ok=False, detail="not published",
                                fix=f"In n8n open 'MailMate {name}' and click Publish (it must be active)."))
        else:
            checks.append(Check(name=name, ok=None, detail=f"unexpected answer: {text[:80]}"))
    return checks


def check_gmail(last_worked: datetime | None, now: datetime, last_error: str = "") -> Check:
    """From the last time a sync or a batch went through, and the last sync error if any."""
    if last_error and any(w in last_error.lower() for w in ("auth", "credential", "token", "401", "invalid_grant")):
        return Check(name="Gmail", ok=False, detail=f"the last sync failed: {last_error[:120]}",
                     fix="In n8n → Credentials, open the Gmail credential and reconnect (Sign in with Google).")
    if last_worked is None:
        return Check(name="Gmail", ok=None, detail="not used yet")
    quiet = now - last_worked
    when = f"{quiet.days} day(s) ago" if quiet.days else "today"
    if quiet > timedelta(days=GMAIL_QUIET_DAYS):
        return Check(name="Gmail", ok=None, detail=f"last worked {when}",
                     fix="Google may have ended n8n's Gmail sign-in (7 days in testing mode). Sync now; if it fails, "
                         "reconnect the Gmail credential in n8n.")
    return Check(name="Gmail", ok=True, detail=f"last worked {when}")


def check_all(settings: N8nSettings, last_worked: datetime | None, now: datetime, last_error: str = "",
              transport: httpx.BaseTransport | None = None) -> list[Check]:
    with httpx.Client(timeout=httpx.Timeout(6.0, connect=4.0), transport=transport) as client:
        return check_n8n(settings, client) + [check_gmail(last_worked, now, last_error)]
