"""Manual check of the live W1 workflow (not part of the test suite: it calls real n8n and Ollama).

    python scripts/check_w1.py                       # 3 default companies
    python scripts/check_w1.py "Acme" "AI Engineer"  # one company + role

Prints each personal sentence with the sources it cites, then checks that the webhook refuses
requests without the secret header or with a wrong one. Exit code 1 if anything failed.
"""

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mailmate.config import ConfigError, n8n_settings  # noqa: E402
from mailmate.n8n import TOKEN_HEADER, N8nClient, N8nError  # noqa: E402

DEFAULT_JOBS = [
    ("Square Yards", "Generative AI Engineer", "Python, LLMs, RAG"),
    ("Sarvam AI", "AI Engineer", "LLM fine-tuning, Indic languages, PyTorch"),
    ("Freshworks", "Machine Learning Engineer", "Python, NLP, AWS"),
]


def main(argv: list[str]) -> int:
    try:
        settings = n8n_settings()
    except ConfigError as exc:
        print(f"Setup problem: {exc}")
        return 1
    jobs = [(argv[0], argv[1], argv[2] if len(argv) > 2 else "")] if len(argv) >= 2 else DEFAULT_JOBS
    print(f"W1 at {settings.base_url}/webhook/MailMate-personalize\n")

    failures = 0
    client = N8nClient(settings)
    for company, role, requirements in jobs:
        started = time.monotonic()
        try:
            result = client.personalize(company, role, requirements)
        except N8nError as exc:
            print(f"[FAIL] {company}: {exc}\n")
            failures += 1
            continue
        ok = result.search_ok and result.sentence and result.sources
        failures += not ok
        print(f"[{'OK' if ok else 'FAIL'}] {company} / {role}  ({time.monotonic() - started:.0f} s, "
              f"model {result.model}, search_ok={result.search_ok})")
        print(f"  sentence ({len(result.sentence)} chars): {result.sentence or '(empty)'}")
        for s in result.sources:
            print(f"  [S{s.id}] {s.title[:90]}\n        {s.url}  ({len(s.content)} chars of text)")
        print()
    client.close()

    url = f"{settings.base_url}/webhook/MailMate-personalize"
    body = {"company": "Square Yards", "role": "AI Engineer"}
    for label, headers in (("no secret header", {}), ("wrong secret", {TOKEN_HEADER: "wrong-" + "x" * 20})):
        try:
            status = httpx.post(url, json=body, headers=headers, timeout=30).status_code
        except httpx.HTTPError as exc:
            print(f"[FAIL] request with {label}: network error ({type(exc).__name__})")
            failures += 1
            continue
        refused = status in (401, 403)
        failures += not refused
        print(f"[{'OK' if refused else 'FAIL'}] request with {label} -> {status} "
              f"({'refused' if refused else 'NOT refused: check the Webhook node authentication!'})")

    print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) failed.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
