import socket

import pytest

from mailmate import db
from mailmate.emailcheck import DomainResult

# Domains used in the tests. Nothing here touches the network.
FAKE_DNS = {
    "nimbuslabs.ai": DomainResult("ok"),
    "quillstack.io": DomainResult("ok"),
    "deadcorp.com": DomainResult("no_mail", "the domain does not exist"),
    "parkedsite.com": DomainResult("no_mail", "the domain says it accepts no email (null MX)"),
    "slowdns.in": DomainResult("unknown", "couldn't check the domain (Timeout)"),
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Rule 9: no test may reach a real webhook, Gmail, DNS or model. Any attempt fails loudly."""
    def refuse(*args, **kwargs):
        raise RuntimeError("tests must not use the network; use a fake (httpx.MockTransport, fake_dns)")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def fake_dns():
    def check(domain: str) -> DomainResult:
        return FAKE_DNS.get(domain, DomainResult("ok"))
    return check


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()
