"""Look at what W3 returns, without changing anything (calls real n8n, Gmail and Ollama; not in pytest).

    python scripts/check_w3.py        # last 30 days
    python scripts/check_w3.py 7      # last 7 days

Prints every MailMate-labelled thread with its replies and how W3 sorted them. MailMate's
database is not touched: use the Replies page's Sync button for that.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mailmate.config import ConfigError, n8n_settings  # noqa: E402
from mailmate.n8n import N8nClient, N8nError  # noqa: E402


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    days = int(argv[0]) if argv else 30
    try:
        client = N8nClient(n8n_settings())
    except ConfigError as exc:
        print(f"Setup problem: {exc}")
        return 1
    started = time.monotonic()
    try:
        result = client.sync(days)
    except N8nError as exc:
        print(f"[FAIL] {exc}")
        return 1
    finally:
        client.close()
    print(f"W3 returned {len(result.threads)} thread(s) in {time.monotonic() - started:.0f} s\n")
    for t in sorted(result.threads, key=lambda t: t.sent_at):
        print(f"{t.sent_at[:16]}  to {t.to}  '{t.subject}'  (thread {t.thread_id})")
        for sent in t.later_sent:
            print(f"    <- my later message (follow-up) at {sent.date[:16]}")
        for r in t.replies:
            sure = f"{r.confidence:.0%}" if r.confidence is not None else "-"
            print(f"    -> {r.category:<17} {sure:>4}  from {r.from_}: {r.snippet[:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
