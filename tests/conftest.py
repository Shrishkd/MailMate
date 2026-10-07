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
