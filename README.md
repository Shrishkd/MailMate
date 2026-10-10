# MailMate

Recruiter outreach automation that is safe by design: I upload a list of jobs, MailMate drafts
one email per recruiter from my template (a model writes only one short, fact-checked personal
sentence), I approve each email, and n8n sends them slowly from my Gmail with my resume attached.
It then reads replies, sorts them and queues at most one follow-up for my approval.

> **Nothing is sent without my approval.** Dry-run is the default, there is a daily cap, a sending
> window and random spacing, and a do-not-contact list that is applied automatically.

*Work in progress: full documentation arrives in Step 11.*

## Setup (Windows, PowerShell)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env      # then fill in N8N_BASE_URL and N8N_WEBHOOK_TOKEN
pytest
streamlit run app.py        # http://localhost:8501, this machine only
```

Check the live n8n W1 workflow (calls real n8n and Ollama; not part of `pytest`):

```powershell
python scripts/check_w1.py          # W1 answers and refuses requests without the secret header
python scripts/check_sentences.py   # W1 + MailMate's checks on the personal line, with retries
python scripts/check_w2_guard.py you@primary.example   # W2 refuses what it must (mails only you)
python scripts/check_w3.py         # what W3 reads and sorts, without changing MailMate's data
```

## Layout

| Path | What |
|---|---|
| `app.py` | Streamlit app (localhost only) |
| `mailmate/` | Python package |
| `n8n/workflows/` | Exported n8n workflows (no secrets) |
| `scripts/` | Manual checks against the live workflows |
| `data/` | Personal data and the SQLite database (git-ignored) |
| `tests/` | `pytest` suite; never calls real webhooks, Gmail or models |
