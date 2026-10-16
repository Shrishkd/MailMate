"""Check W4 (reply suggestions and Gmail drafts) against real n8n; not part of pytest.

    python scripts/check_w4.py            # W4 suggests an answer to a sample recruiter email
    python scripts/check_w4.py --draft    # ...and saves it as a DRAFT in your self-test Gmail thread

W4 never sends anything. --draft only ever uses a thread whose email went to your own test address
(MAILMATE_TEST_ADDRESS), and that thread needs one answer from the test inbox (reply to the self-test
email from there once). Afterwards, look in Gmail -> Drafts of the sending account, then delete the draft.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mailmate import db  # noqa: E402
from mailmate.config import DB_PATH, ConfigError, n8n_settings, test_address  # noqa: E402
from mailmate.n8n import N8nClient, N8nError  # noqa: E402

SAMPLE = {
    "their_message": "Hi, thanks for reaching out! Your profile looks interesting. Could you share your availability "
                     "for a 30-minute call with our engineering manager this week?",
    "their_name": "Priya", "company": "Example Corp", "role": "AI Engineer", "my_name": "Your Name",
    "my_email": "Hi Priya, I'm an AI engineer who builds agents and GenAI products...",
    "notes": "free Wednesday and Thursday after 3 pm IST",
}


def self_test_thread(test_to: str) -> str | None:
    conn = db.connect(DB_PATH)
    try:
        row = conn.execute("SELECT e.gmail_thread_id FROM emails e JOIN contacts c ON c.id = e.contact_id"
                           " WHERE lower(c.email) = ? AND e.gmail_thread_id IS NOT NULL ORDER BY e.id DESC LIMIT 1",
                           (test_to,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        client, test_to = N8nClient(n8n_settings()), test_address()
    except ConfigError as exc:
        print(f"Setup problem: {exc}")
        return 1
    try:
        answer = client.write_reply(SAMPLE)
        print(f"[OK]   W4 suggested ({answer.model}):\n\n{answer.text}\n")
        if "--draft" not in argv:
            print("Run with --draft to also save a test draft in your self-test Gmail thread.")
            return 0
        thread = self_test_thread(test_to)
        if not thread:
            print(f"[SKIP] No MailMate email to {test_to} with a known Gmail thread yet. Send a real batch to your "
                  "test address only (a self-test), sync, reply once from the test inbox, then run this again.")
            return 1
        text = answer.text.replace("[", "(").replace("]", ")")       # a placeholder would be refused
        draft = client.save_reply_draft(thread, text)
        print(f"[OK]   Draft {draft.draft_id} saved, addressed to {draft.to}. Look in Gmail -> Drafts (sending "
              "account), check it sits in the self-test conversation, then delete it.")
        return 0
    except N8nError as exc:
        print(f"[FAIL] {exc}")
        if "no message from them" in str(exc):
            print("       Reply once to the self-test email from your test inbox, sync, then run this again.")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
