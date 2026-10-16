"""The health check (n8n up, workflows published, Gmail lately) and the background reply sync."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from mailmate import autosync, db, health
from mailmate.config import N8nSettings
from mailmate.n8n import N8nError, SyncResult

LOCAL = N8nSettings(base_url="http://localhost:5678", token="tok")
NOW = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)
PUBLISHED = '{"code":404,"message":"This webhook is not registered for GET requests. Did you mean to make a POST request?"}'
MISSING = '{"code":404,"message":"The requested webhook \\"GET MailMate-send\\" is not registered."}'


def n8n(missing: tuple[str, ...] = (), down: bool = False) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"                       # the check never starts a workflow
        if down:
            raise httpx.ConnectError("refused")
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(404, text=MISSING if name in missing else PUBLISHED)
    return httpx.MockTransport(answer)


def test_all_good():
    checks = health.check_all(LOCAL, NOW - timedelta(hours=3), NOW, transport=n8n())
    assert [(c.name, c.ok) for c in checks] == [("n8n", True), ("W1 Personalize", True), ("W2 Send", True),
                                                ("W3 Sync", True), ("Gmail", True)]


def test_n8n_down_says_how_to_start_docker():
    checks = health.check_all(LOCAL, None, NOW, transport=n8n(down=True))
    assert checks[0].ok is False and "docker start n8n" in checks[0].fix
    assert [c.name for c in checks] == ["n8n", "Gmail"]       # no point asking about workflows


def test_an_unpublished_workflow_is_named():
    checks = {c.name: c for c in health.check_all(LOCAL, None, NOW, transport=n8n(missing=("MailMate-send",)))}
    assert checks["W2 Send"].ok is False and "Publish" in checks["W2 Send"].fix
    assert checks["W1 Personalize"].ok and checks["W3 Sync"].ok


@pytest.mark.parametrize("last, error, ok", [
    (None, "", None),
    (NOW - timedelta(days=1), "", True),
    (NOW - timedelta(days=8), "", None),                                  # maybe the 7-day sign-in ran out
    (NOW - timedelta(days=1), "W3 failed: invalid_grant (token expired)", False),
])
def test_gmail(last, error, ok):
    check = health.check_gmail(last, NOW, error)
    assert check.ok is ok
    if ok is False:
        assert "reconnect" in check.fix


# --- background sync ------------------------------------------------------------------------------

@pytest.fixture
def sent(tmp_path):
    """A database (on disk: the background thread opens its own connection) with one real email sent."""
    path = tmp_path / "mailmate.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO contacts (company, role, email, status, created_at) VALUES ('Acme', 'AI', 'a@acme.example',"
                 " 'contacted', ?)", (db.now(),))
    conn.execute("INSERT INTO templates (name, version, subject, body, created_at) VALUES ('t', 1, 's', 'b', ?)",
                 (db.now(),))
    conn.execute("INSERT INTO emails (contact_id, template_id, status, test_mode, queued_at, created_at)"
                 " VALUES (1, 1, 'queued', 0, ?, ?)", ((NOW - timedelta(days=1)).isoformat(), db.now()))
    conn.commit()
    yield path, conn
    conn.close()


def synced(conn, at: datetime) -> None:
    conn.execute("INSERT INTO events (kind, created_at) VALUES ('sync_done', ?)", (at.isoformat(),))


def test_due_only_when_something_was_sent_and_the_last_sync_is_old(sent, conn):
    _, disk = sent
    assert autosync.due(conn, NOW) == ""                                  # nothing ever sent
    assert autosync.due(disk, NOW) == "there has been no sync yet"
    synced(disk, NOW - timedelta(hours=2))
    assert autosync.due(disk, NOW) == ""
    assert autosync.due(disk, NOW + timedelta(hours=5)) == "the last sync was 7 h ago"
    autosync.set_enabled(disk, False)
    assert autosync.due(disk, NOW + timedelta(hours=5)) == ""


def test_a_recent_failure_waits_an_hour(sent):
    _, disk = sent
    autosync.record_failure(disk, "n8n didn't answer")
    failed_at = autosync.last_failure(disk)[0]
    assert autosync.last_failure(disk)[1] == "n8n didn't answer"
    assert autosync.due(disk, failed_at + timedelta(minutes=20)) == ""
    assert autosync.due(disk, failed_at + timedelta(minutes=61)) == "there has been no sync yet"


class FakeClient:
    def __init__(self, error: Exception | None = None):
        self.error, self.closed = error, False

    def sync(self, since_days):
        if self.error:
            raise self.error
        return SyncResult(threads=[])

    def close(self):
        self.closed = True


def test_the_background_sync_applies_the_result_and_records_it(sent):
    path, disk = sent
    client = FakeClient()
    assert autosync.start(path, LOCAL, "me@example.net", client_factory=lambda s: client)
    autosync.wait(10)
    state = autosync.status()
    assert state["running_since"] is None and state["error"] == ""
    assert state["summary"].startswith("0 newly confirmed as sent, 0 new reply(ies)")
    assert disk.execute("SELECT COUNT(*) FROM events WHERE kind = 'sync_done'").fetchone()[0] == 1
    assert client.closed


def test_a_failed_background_sync_is_recorded_not_lost(sent):
    path, disk = sent
    assert autosync.start(path, LOCAL, "me@example.net",
                          client_factory=lambda s: FakeClient(N8nError("n8n returned HTTP 403")))
    autosync.wait(10)
    assert autosync.status()["error"] == "n8n returned HTTP 403"
    assert autosync.last_failure(disk)[1] == "n8n returned HTTP 403"
