"""Manual run of W1 + MailMate's sentence checks (not part of the test suite: calls real n8n and Ollama).

    python scripts/check_sentences.py                       # 5 default companies
    python scripts/check_sentences.py "Acme" "AI Engineer"  # one company + role

For each company: every attempt W1 made, why it was rejected, and the final personal line
(or the fallback to none). This is Step 4's evidence that the checks catch real problems.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mailmate import db  # noqa: E402
from mailmate.config import DB_PATH, ConfigError, n8n_settings  # noqa: E402
from mailmate.n8n import N8nClient, N8nError  # noqa: E402
from mailmate.sentence_check import is_job_ad, personalize_checked  # noqa: E402

DEFAULT_JOBS = [
    ("Square Yards", "Generative AI Engineer", "Python, LLMs, RAG"),
    ("Sarvam AI", "AI Engineer", "LLM fine-tuning, Indic languages, PyTorch"),
    ("Freshworks", "Machine Learning Engineer", "Python, NLP, AWS"),
    ("Zepto", "AI Engineer", "Python, recommendation systems"),
    ("Razorpay", "Agentic AI Engineer", "LangChain, agents, Python"),
]


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        settings = n8n_settings()
    except ConfigError as exc:
        print(f"Setup problem: {exc}")
        return 1
    conn = db.connect(DB_PATH)
    my_name = db.get_settings(conn).get("my_name", "")
    conn.close()
    jobs = [(argv[0], argv[1], argv[2] if len(argv) > 2 else "")] if len(argv) >= 2 else DEFAULT_JOBS

    client = N8nClient(settings)
    accepted = rejected_attempts = 0
    for company, role, requirements in jobs:
        contact = {"company": company, "role": role, "requirements": requirements, "job_url": ""}
        started = time.monotonic()
        try:
            line = personalize_checked(client.personalize, contact, my_name=my_name)
        except N8nError as exc:
            print(f"== {company}: W1 failed: {exc}\n")
            continue
        print(f"== {company} / {role}  ({time.monotonic() - started:.0f} s)")
        for n, attempt in enumerate(line.attempts, 1):
            print(f"  attempt {n}: {'ACCEPTED' if not attempt.problems else 'REJECTED'}")
            print(f"    {attempt.raw or '(no sentence)'}")
            for problem in attempt.problems:
                print(f"    - {problem}")
            for s in attempt.sources:
                print(f"    [S{s.id}]{' (job ad)' if is_job_ad(s.url) else ''} {s.url}")
            rejected_attempts += bool(attempt.problems)
        print(f"  FINAL: {line.sentence or '(no personal line)'}\n")
        accepted += bool(line.sentence)
    client.close()

    print(f"{accepted} of {len(jobs)} companies got a personal line; {rejected_attempts} attempt(s) were rejected.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
