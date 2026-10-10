"""Manual check that W2's guard refuses what it must (calls real n8n; not part of the test suite).

    python scripts/check_w2_guard.py you@your-primary-inbox.com

Each batch below must be refused by W2's 'Explode + guard' node before anything is sent. They
are addressed to YOUR OWN primary inbox (the argument), so even a broken guard can only ever
email you. If any batch is accepted, deactivate W2 and fix the guard before sending anything.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mailmate.config import ConfigError, n8n_settings, test_address  # noqa: E402
from mailmate.n8n import N8nClient, N8nError  # noqa: E402

FAKE_PDF = b"%PDF-1.4\n% MailMate guard check\n"


def email(to: str, n: int = 1) -> dict:
    text = "If you received this, W2's guard is broken. Deactivate W2."
    return {"email_id": f"guard-{n}", "to": to, "subject": "MailMate guard check (should never arrive)",
            "body_text": text, "body_html": f"<p>{text}</p>"}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(argv) != 1 or "@" not in argv[0]:
        print(__doc__)
        return 1
    own = argv[0].strip().lower()
    try:
        settings, test_to = n8n_settings(), test_address()
    except ConfigError as exc:
        print(f"Setup problem: {exc}")
        return 1
    if own == test_to:
        print("Use your PRIMARY address here, not the test address: test mode is allowed to send to that one.")
        return 1

    cases = [
        ("test mode, address other than the test inbox", {"test_mode": True, "emails": [email(own)]}),
        ("real-mode email without an HTML body",
         {"test_mode": False, "emails": [{k: v for k, v in email(own).items() if k != "body_html"}]}),
        ("batch larger than 20", {"test_mode": True, "emails": [email(test_to, i) for i in range(21)]}),
        ("test-mode follow-up to an address other than the test inbox",
         {"test_mode": True, "emails": [{**email(own), "reply_to_message_id": "abc"}]}),
    ]
    client = N8nClient(settings, retry_delays=())
    failures = 0
    for n, (label, body) in enumerate(cases, 1):
        payload = {"batch_id": -n, "sender_name": "guard check", "spacing_min_s": 60, "spacing_max_s": 60, **body}
        try:
            client.send_batch(payload, FAKE_PDF, "guard.pdf")
        except N8nError as exc:
            print(f"[OK]   refused: {label}\n       ({str(exc)[:140]})")
        else:
            failures += 1
            print(f"[FAIL] ACCEPTED: {label}. Deactivate W2 now and check the guard node!")
    client.close()
    print("\nAll refused, as they should be." if not failures else f"\n{failures} batch(es) were accepted!")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
