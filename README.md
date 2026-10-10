# MailMate

Recruiter outreach that is safe by design. I upload a list of jobs and recruiters; MailMate drafts
one email per recruiter from my template, with one short personal line about the company that a
model writes and plain code fact-checks; I approve every email; n8n sends them slowly from my Gmail
with my resume attached. MailMate then reads the replies, sorts them, keeps a do-not-contact list,
and offers at most one follow-up, again for my approval.

> **Nothing is sent without my approval.** Test emails go only to my own secondary inbox. Real
> sending has a daily cap, a weekday sending window, random spacing, one batch at a time and a
> typed confirmation. The app runs on this machine only.

## Rules that are never broken

1. No email goes out without my explicit approval of that email.
2. Every template version needs a test email to my own inbox, checked by me, before real sending.
3. Daily cap (default 20), weekdays 09:30-18:00 IST, 3-7 minutes between emails; a batch must
   finish inside the window.
4. The model writes only the personal line. Code rejects it if it makes claims about me, names
   tools, contains facts not in the sources it cites, cites only job ads, is too long, contains
   placeholders or links, or doesn't say why the company makes me want to apply. One retry, then
   the email goes without a personal line.
5. Nobody is emailed twice (except the one follow-up). Bounces and "not interested" replies go on
   the do-not-contact list automatically.
6. Every email ends with an opt-out line.
7. Secrets live in `.env`, personal data in `data/`; both are git-ignored. Served on localhost only.
8. The n8n webhooks require a secret header.
9. Tests never touch the network, Gmail or a model.

## How it fits together

```
Streamlit app (this laptop, localhost)                 SQLite: data/mailmate.db
  upload -> templates -> review -> send -> replies -> dashboard
        |  HTTPS POST + header X-MailMate-Token   (laptop -> cloud only)
        v
n8n (Cloud; later Docker)                       stateless: keeps no data
  W1 MailMate-personalize   web search (Ollama) -> LLM writes one line + cited sources
  W2 MailMate-send          guard -> answer "accepted" -> per email: Gmail send (+PDF, label)
                            or reply in the first email's thread (follow-up) -> random wait
  W3 MailMate-sync          labelled Gmail threads -> replies, bounces -> LLM sorts replies
        |
        +-- Ollama Cloud API (web search + gpt-oss:120b)     +-- Gmail (OAuth)
```

- n8n can't reach the laptop, so **MailMate keeps all data** and the workflows keep none.
- n8n's Wait node gives reliable throttling over hours; the laptop can be off while a batch sends.
- **Gmail is the record of what was sent:** every sent email gets the `MailMate` label; W3 reads
  labelled threads to learn which emails went out and who answered.

## Setup from scratch (Windows, PowerShell)

### 1. The app

```powershell
py install 3.11                         # Python install manager; or install Python 3.11 from python.org
py -V:3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env                  # fill it in: step 3
pytest                                  # all tests should pass, offline
```

### 2. Gmail, n8n and the credentials

1. **Gmail:** create a label named exactly `MailMate`.
2. **n8n:** sign up at n8n.io; your workspace is `https://<name>.app.n8n.cloud`. Settings → Personal →
   Timezone: **Asia/Kolkata**.
3. **Webhook secret:** `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Keep it for `.env`
   and the n8n credential below.
4. In n8n, **Overview → Credentials → Create credential** (values only ever go here, never into workflows):

   | Credential type | Name it | Value |
   |---|---|---|
   | Gmail OAuth2 API | any (e.g. `Gmail account`) | *Sign in with Google* with the account you send from |
   | Header Auth | `MailMate webhook token` | Name `X-MailMate-Token`, Value: the secret from step 3 |
   | Header Auth | `Ollama API` | Name `Authorization`, Value `Bearer <your Ollama API key>` |

### 3. `.env`

```
N8N_BASE_URL=https://<name>.app.n8n.cloud
N8N_WEBHOOK_TOKEN=<the secret from step 2.3>
MAILMATE_TEST_ADDRESS=<your own secondary inbox>
```

### 4. Import the three workflows

For each file in `n8n/workflows/`: **Overview → Create workflow** (a new, empty one) → `⋯` → **Import
from File**. Then set the credentials node by node, **Save**, and **Publish**. Credentials never travel
inside workflow files.

| Workflow file | Set these |
|---|---|
| `MailMate_W1_Personalize.json` | Webhook → `MailMate webhook token`; Web search and LLM → `Ollama API` |
| `MailMate_W2_Send.json` | Webhook → `MailMate webhook token`; Gmail Send, Add MailMate label, Get original, Send reply → your Gmail credential; Add MailMate label → pick the `MailMate` label; **Explode + guard** → replace `REPLACE_WITH_YOUR_TEST_ADDRESS` with your secondary inbox |
| `MailMate_W3_Sync.json` | Webhook → `MailMate webhook token`; Labelled threads and Get thread → your Gmail credential; Classify → `Ollama API` |

Check that Gmail Send's options say **Append n8n Attribution: off**.

### 5. Check the live setup

These call the real n8n, Gmail and Ollama, so they are not part of `pytest`:

```powershell
python scripts/check_w1.py                     # W1 answers; refuses requests without the secret header
python scripts/check_sentences.py              # W1 + MailMate's checks on the personal line
python scripts/check_w2_guard.py you@primary   # W2 refuses 4 bad batches (they could only ever mail you)
python scripts/check_w3.py                     # what W3 reads and sorts; changes nothing
```

### 6. Run it

```powershell
streamlit run app.py        # http://localhost:8501, this machine only
```

## Using it, page by page

1. **Upload:** a `.csv`, `.xlsx` (any sheets with Company + Email columns; title rows above the header
   are fine) or a Word table. Type the role you're applying for; a *Target Role* column overrides it per
   row. A recruiter's own title ("Senior Technical Recruiter") is kept as their title, never used as the
   role. The report shows every rejected row and why; nothing is saved until **Import**. Rows whose
   status says you already emailed them go on the do-not-contact list.
2. **Templates:** your name and signature, and templates with merge fields `{first_name} {company}
   {role} {personal_line} {my_name} {signature}`. Saving a change creates a new version. The
   *Follow-up* template is the text of the one follow-up.
3. **Review:** create drafts (W1 researches each company), then approve, edit or reject each email.
   Problems need a ticked override with a reason; do-not-contact and "already contacted" can't be
   overridden. *Follow-ups due* lists first emails with no reply after N days.
4. **Send:** upload the resume PDF (under 1 MB). Send 1-3 drafts as a **test** to your own inbox and
   mark the template version test-checked. Then **real sending**: pick approved emails, type their
   number, send. A batch to your own test address only is a self-test and may go any time.
5. **Replies:** **Sync replies now** reads Gmail through W3: confirms which emails went out, stores and
   sorts replies, and puts bounces and confident "not interested" replies on the do-not-contact list.
   Sync before drafting follow-ups (they're only offered within 24 h of a sync).
6. **Dashboard:** funnel, reply and bounce rates, results per template version, sends per day,
   recent replies. Real sends only; your test address never counts.

**Stop a running batch:** n8n → Executions → open the running *MailMate W2 Send* → **Stop**, then
Send → *I stopped it*.

### Job list columns

Matched by header name after lower-casing; the header may be below title rows.

| Field | Accepted headers (examples) | Needed |
|---|---|---|
| Company | Company, Company Name, Organization | yes |
| Recruiter email | Email, Recruiter Email, Email ID, Contact Email | yes |
| Role applying for | Target Role, Applying For, Job Title, Role, Position | or type it at upload |
| Their title | Title, Role / Title, Recruiter Title, Designation | no |
| Name | Name, Recruiter Name, Contact; or First Name + Last Name | no |
| Email status | Hunter status, Email Status, Verification | no (`invalid` refused, `accept_all` warned) |
| Outreach status | Status, Date Sent | no (already sent -> do-not-contact) |
| Requirements, Job URL | Requirements, Skills, JD; Job URL, Link | no |

### Statuses

`contacts`: new → drafted → contacted → (followed_up) → replied / bounced.
`emails`: draft → approved → queued (handed to W2) → sent (Gmail confirmed by sync) → replied / bounced;
or rejected. Every change is written to the append-only `events` table.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `404` from a webhook | The workflow isn't published, or was edited and not published again; check the path spelling |
| `401`/`403` from a webhook | `N8N_WEBHOOK_TOKEN` in `.env` doesn't match the `MailMate webhook token` credential |
| W1: `search_ok` false, no line | Open the W1 execution, *Web search* node, read its error (Ollama key, quota) |
| Every personal line rejected | Read the reasons on the Review page; `scripts/check_sentences.py` shows them per attempt |
| Email shows "sent automatically with n8n" | W2 → Gmail Send → Options → Append n8n Attribution: off |
| Email text wraps at half width | W2 → Gmail Send must send `{{ $json.body_html }}` as **HTML** |
| No PDF attached | W2 → Gmail Send → Attachments property must be `resume` |
| "Not sent: ... stay queued until you've checked" | The connection broke after sending started. Look in n8n Executions; if nothing was sent, **Release** the batch on the Send page |
| Emails stay `queued` | Run **Sync replies**. If they're still queued an hour after the batch, n8n Executions shows which Gmail Send failed |
| `lookup ollama.com: no such host` | Network blip; the clients retry. If it persists, change DNS to 1.1.1.1 / 8.8.8.8 |
| `pip install` times out | Flaky network: `pip install --retries 10 --timeout 120 -e ".[dev]"` |
| git: "dubious ownership" after reinstalling Windows | `git config --global --add safe.directory D:/PROJECTS/MailMate` |

## Layout

| Path | What |
|---|---|
| `app.py` | Streamlit app: Upload, Contacts, Templates, Review, Send, Replies, Dashboard |
| `mailmate/importer.py` | Reading and checking job lists |
| `mailmate/templates.py` | Templates, deterministic merge, opt-out line |
| `mailmate/n8n.py` | Webhook client: W1, W2, W3; retries; never resends what may have gone out |
| `mailmate/sentence_check.py` | Checks on the personal line; one retry; fallback |
| `mailmate/review.py` | Drafts, checks on every approval, overrides |
| `mailmate/sending.py` | Test batches, real batches and their limits |
| `mailmate/sync.py` | Applying W3's answer: sent, replies, bounces, do-not-contact |
| `mailmate/followups.py` | The one follow-up |
| `mailmate/stats.py` | Dashboard numbers |
| `mailmate/db.py` | SQLite schema with the safety rules enforced in the database |
| `n8n/workflows/` | W1, W2, W3 exports (no credentials) |
| `scripts/` | Manual checks against the live workflows |
| `tests/` | `pytest` suite; offline |
| `data/` | Database, resume, uploads (git-ignored: back it up yourself) |

## Moving n8n to Docker (when the Cloud trial ends)

Export nothing new: the three workflow files are in `n8n/workflows/`. Run n8n locally
(`docker run -d --name n8n -p 5678:5678 -e GENERIC_TIMEZONE=Asia/Kolkata -v n8n_data:/home/node/.n8n
docker.n8n.io/n8nio/n8n`), recreate the three credentials (Gmail needs your own Google Cloud OAuth
client there), import the workflows as above, and set `N8N_BASE_URL=http://localhost:5678` in `.env`.
