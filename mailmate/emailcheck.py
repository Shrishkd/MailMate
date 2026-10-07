"""Is this an address we can and should write to?

Syntax comes from email-validator (it follows the RFCs, a hand-written regex doesn't).
Whether the domain accepts mail is a DNS lookup done here with dnspython, so a flaky network
gives "couldn't check" (a warning) instead of rejecting good addresses.
"""

from dataclasses import dataclass
from email.utils import parseaddr
from typing import Callable, Literal

import dns.exception
import dns.resolver
from email_validator import EmailNotValidError, validate_email

# Addresses nobody reads. Writing to them wastes a send from the daily cap.
NO_REPLY_PARTS = ("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "do_not_reply", "mailer-daemon")

DomainStatus = Literal["ok", "no_mail", "unknown"]


@dataclass(frozen=True)
class DomainResult:
    status: DomainStatus
    reason: str = ""


DomainChecker = Callable[[str], DomainResult]


@dataclass(frozen=True)
class AddressResult:
    email: str = ""          # normalized, lower-cased; empty if invalid
    error: str = ""
    display_name: str = ""   # from "Priya Sharma <priya@acme.com>"


def parse_address(cell: str) -> AddressResult:
    """Clean one spreadsheet cell into a single, syntactically valid address."""
    text = cell.strip().strip(";,").strip()
    if text.lower().startswith("mailto:"):
        text = text[7:].split("?")[0].strip()
    if not text:
        return AddressResult(error="email is missing")
    if text.count("@") > 1:
        return AddressResult(error="more than one address in the cell; put one recruiter per row")
    name, addr = parseaddr(text)
    addr = addr or text
    try:
        normalized = validate_email(addr, check_deliverability=False).normalized
    except EmailNotValidError as exc:
        return AddressResult(error=f"not a valid email address: {exc}")
    local = normalized.split("@")[0].lower()
    if any(p in local for p in NO_REPLY_PARTS):
        return AddressResult(error="no-reply address; nobody reads it")
    return AddressResult(email=normalized.lower(), display_name=name.strip())


class DnsDomainChecker:
    """Does the domain accept mail? Cached per domain; gives up early if the network is down."""

    def __init__(self, timeout_s: float = 5.0, give_up_after: int = 3):
        self._resolver = dns.resolver.Resolver()
        self._resolver.lifetime = timeout_s
        self._cache: dict[str, DomainResult] = {}
        self._failures_in_a_row = 0
        self._give_up_after = give_up_after

    def __call__(self, domain: str) -> DomainResult:
        domain = domain.lower()
        if domain not in self._cache:
            if self._failures_in_a_row >= self._give_up_after:
                return DomainResult("unknown", "not checked: DNS lookups keep failing (network down?)")
            result = self._lookup(domain)
            self._failures_in_a_row = self._failures_in_a_row + 1 if result.status == "unknown" else 0
            self._cache[domain] = result
        return self._cache[domain]

    def _lookup(self, domain: str) -> DomainResult:
        try:
            answer = self._resolver.resolve(domain, "MX")
            hosts = [str(r.exchange).rstrip(".") for r in answer]
            if hosts == [""]:  # null MX (RFC 7505): "this domain accepts no mail"
                return DomainResult("no_mail", "the domain says it accepts no email (null MX)")
            return DomainResult("ok")
        except dns.resolver.NXDOMAIN:
            return DomainResult("no_mail", "the domain does not exist")
        except dns.resolver.NoAnswer:
            pass  # no MX record: mail falls back to the domain's own address, if it has one
        except (dns.exception.Timeout, dns.resolver.NoNameservers, dns.exception.DNSException) as exc:
            return DomainResult("unknown", f"couldn't check the domain ({type(exc).__name__})")
        try:
            self._resolver.resolve(domain, "A")
            return DomainResult("ok")
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return DomainResult("no_mail", "the domain has no mail server")
        except dns.exception.DNSException as exc:
            return DomainResult("unknown", f"couldn't check the domain ({type(exc).__name__})")
