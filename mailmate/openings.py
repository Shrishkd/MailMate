"""Openings watcher: new AI roles on the job boards of companies I'm interested in.

Most companies list jobs on a hiring platform with a public job feed: Greenhouse, Lever, Ashby,
Workable, SmartRecruiters, Recruitee. A board is watched when a contact's job link points to one,
when I add a careers page by hand, or when "find boards" finds one for a company and I confirm it
(a board name like 'nimbus' could belong to another company, so nothing found is watched unasked).
A plain careers page is read for links whose text looks like a job title; pages built with JavaScript
show nothing that way, and the watcher says so.

Only public job lists are fetched (no login, nothing personal is sent). New titles that match my
keywords become openings; each one is kept once (by its link) so a second check finds nothing new.
"""

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel

from mailmate import db
from mailmate.companies import company_key

DEFAULT_KEYWORDS = ("AI, ML, machine learning, GenAI, generative, LLM, agent, agentic, data scientist, NLP, "
                    "deep learning, applied scientist, MLOps, computer vision")
TIMEOUT = httpx.Timeout(15.0, connect=8.0)
HEADERS = {"User-Agent": "MailMate openings watcher (personal job search)"}

FEEDS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{token}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{token}/postings",
    "recruitee": "https://{token}.recruitee.com/api/offers/",
}
PLATFORMS = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workable": "Workable",
             "smartrecruiters": "SmartRecruiters", "recruitee": "Recruitee", "page": "careers page"}
DISCOVER_ON = ("greenhouse", "lever", "ashby")
BOARD_PAGES = {"greenhouse": "https://boards.greenhouse.io/{token}", "lever": "https://jobs.lever.co/{token}",
               "ashby": "https://jobs.ashbyhq.com/{token}"}
_SUFFIXES = re.compile(r"\b(private|pvt|limited|ltd|inc|llc|corp|corporation|technologies|technology|labs?|"
                       r"solutions|software|india|global|services)\b\.?", re.I)


class Opening(BaseModel):
    title: str
    url: str
    location: str = ""


class Board(BaseModel):
    kind: str
    token: str = ""
    url: str


class WatchError(ValueError):
    pass


# --- recognising boards -----------------------------------------------------------------------------

def detect(url: str) -> Board | None:
    """The hiring platform and board name behind a job or careers link; None for a plain page."""
    parsed = urlparse(url.strip())
    host, parts = parsed.netloc.lower(), [p for p in parsed.path.split("/") if p]
    if not host:
        return None
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io"):
        token = parts[0] if parts and parts[0] != "embed" else dict(
            p.split("=", 1) for p in parsed.query.split("&") if "=" in p).get("for", "")
        return Board(kind="greenhouse", token=token, url=url) if token else None
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        return Board(kind="lever", token=parts[0], url=url)
    if host == "jobs.ashbyhq.com" and parts:
        return Board(kind="ashby", token=parts[0], url=url)
    if host == "apply.workable.com" and parts and parts[0] != "api":
        return Board(kind="workable", token=parts[0], url=url)
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and parts:
        return Board(kind="smartrecruiters", token=parts[0], url=url)
    if host.endswith(".recruitee.com") and host.count(".") == 2:
        return Board(kind="recruitee", token=host.split(".")[0], url=url)
    return None


def board_for(url: str) -> Board:
    """A board for any careers link: a platform board when recognised, otherwise the page itself."""
    if not urlparse(url.strip()).scheme.startswith("http"):
        raise WatchError("the link must start with https://")
    return detect(url) or Board(kind="page", url=url.strip())


# --- reading boards -----------------------------------------------------------------------------------

class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(" ".join(self._text).split())))
            self._href = None


def parse(board: Board, text: str) -> list[Opening]:
    """Openings from a board's feed (JSON) or a careers page (HTML)."""
    if board.kind == "page":
        reader = _Links()
        reader.feed(text)
        return [Opening(title=t, url=urljoin(board.url, href)) for href, t in reader.links
                if href and 3 <= len(t) <= 120 and not href.startswith(("#", "mailto:", "javascript:"))]
    data = json.loads(text)
    if board.kind == "greenhouse":
        return [Opening(title=j["title"], url=j["absolute_url"], location=(j.get("location") or {}).get("name", ""))
                for j in data.get("jobs", [])]
    if board.kind == "lever":
        return [Opening(title=j["text"], url=j["hostedUrl"], location=(j.get("categories") or {}).get("location") or "")
                for j in data]
    if board.kind == "ashby":
        return [Opening(title=j["title"], url=j.get("jobUrl") or j.get("applyUrl", ""), location=j.get("location", ""))
                for j in data.get("jobs", []) if j.get("isListed", True)]
    if board.kind == "workable":
        return [Opening(title=j["title"], url=j.get("url") or j.get("shortlink") or j.get("application_url", ""),
                        location=", ".join(x for x in (j.get("city"), j.get("country")) if x))
                for j in data.get("jobs", [])]
    if board.kind == "smartrecruiters":
        return [Opening(title=j["name"], url=f"https://jobs.smartrecruiters.com/{board.token}/{j['id']}",
                        location=(j.get("location") or {}).get("city", "")) for j in data.get("content", [])]
    if board.kind == "recruitee":
        return [Opening(title=j["title"], url=j.get("careers_url", ""), location=j.get("location", ""))
                for j in data.get("offers", [])]
    raise WatchError(f"unknown board kind: {board.kind}")


def fetch(board: Board, client: httpx.Client) -> list[Opening]:
    url = board.url if board.kind == "page" else FEEDS[board.kind].format(token=board.token)
    try:
        response = client.get(url, headers=HEADERS, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise WatchError(f"couldn't reach it ({type(exc).__name__})") from None
    if response.status_code == 404:
        raise WatchError(f"no {PLATFORMS[board.kind]} board called '{board.token}'" if board.token else "page not found")
    if response.status_code >= 400:
        raise WatchError(f"{PLATFORMS[board.kind]} answered HTTP {response.status_code}")
    try:
        return [o for o in parse(board, response.text) if o.url]
    except (ValueError, KeyError, TypeError) as exc:
        raise WatchError(f"couldn't read the job list ({type(exc).__name__})") from None


# --- keywords -------------------------------------------------------------------------------------------

def keywords(conn: sqlite3.Connection) -> list[str]:
    raw = db.get_settings(conn).get("opening_keywords", DEFAULT_KEYWORDS)
    return [k.strip() for k in raw.split(",") if k.strip()]


def set_keywords(conn: sqlite3.Connection, text: str) -> None:
    words = [w.strip() for w in text.split(",") if w.strip()]
    if not words:
        raise WatchError("give at least one keyword")
    db.set_settings(conn, opening_keywords=", ".join(words))


def matches(title: str, words: list[str]) -> bool:
    """Whole words, any case: 'AI' matches 'AI Engineer' and 'Gen AI', not 'Maintenance'."""
    return any(re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", title, re.I) for w in words)


# --- the watch list ---------------------------------------------------------------------------------------

def watch(conn: sqlite3.Connection, company: str, board: Board, source: str = "by hand") -> bool:
    """Watch this board for this company (one board per company: a new one replaces the old)."""
    key = company_key(company)
    if not key:
        raise WatchError("the company name is empty")
    with conn:
        old = conn.execute("SELECT url FROM watch_boards WHERE company_key = ?", (key,)).fetchone()
        conn.execute("INSERT INTO watch_boards (company_key, company, kind, token, url, source, created_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(company_key) DO UPDATE SET kind = excluded.kind,"
                     " token = excluded.token, url = excluded.url, source = excluded.source, last_error = ''",
                     (key, company.strip(), board.kind, board.token, board.url, source, db.now()))
        db.log_event(conn, "board_watched", company=company.strip(), platform=board.kind, source=source)
    return old is None or old["url"] != board.url


def unwatch(conn: sqlite3.Connection, key: str) -> None:
    with conn:
        conn.execute("DELETE FROM watch_boards WHERE company_key = ?", (key,))


def boards(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM watch_boards ORDER BY company COLLATE NOCASE").fetchall()


def watch_from_job_links(conn: sqlite3.Connection) -> list[str]:
    """Companies whose contacts' job links point to a hiring platform, not watched yet. No network."""
    watched = {r["company_key"] for r in boards(conn)}
    added = []
    for r in conn.execute("SELECT company, job_url FROM contacts WHERE job_url != '' ORDER BY id"):
        board = detect(r["job_url"])
        if board and company_key(r["company"]) not in watched:
            watch(conn, r["company"], board, source="job link")
            watched.add(company_key(r["company"]))
            added.append(r["company"])
    return added


def check(conn: sqlite3.Connection, client: httpx.Client | None = None, now: datetime | None = None,
          workers: int = 6) -> tuple[list[str], list[str]]:
    """Read every watched board. Returns (new matching openings as 'Company: title', problems)."""
    now = now or datetime.now(timezone.utc)
    rows, words = boards(conn), keywords(conn)
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)

    def read(row):
        try:
            return row, fetch(Board(kind=row["kind"], token=row["token"], url=row["url"]), client), ""
        except WatchError as exc:
            return row, [], str(exc)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(read, rows))
    finally:
        if own:
            client.close()
    new, problems = [], []
    with conn:
        for row, found, error in results:
            if not error and row["kind"] == "page" and not found:
                error = "no job links found: the page is probably built with JavaScript; add its hiring-platform link"
            if error:
                problems.append(f"{row['company']}: {error}")
            for o in found:
                if not matches(o.title, words):
                    continue
                added = conn.execute("INSERT OR IGNORE INTO openings (company_key, company, title, url, location,"
                                     " first_seen) VALUES (?, ?, ?, ?, ?, ?)",
                                     (row["company_key"], row["company"], o.title.strip(), o.url, o.location,
                                      now.isoformat(timespec="seconds"))).rowcount
                if added:
                    new.append(f"{row['company']}: {o.title.strip()}")
            conn.execute("UPDATE watch_boards SET last_checked = ?, last_error = ?, last_count = ? WHERE company_key = ?",
                         (now.isoformat(timespec="seconds"), error, len(found), row["company_key"]))
        db.log_event(conn, "openings_checked", boards=len(rows), new=len(new), problems=len(problems))
    return new, problems


def last_checked(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT created_at FROM events WHERE kind = 'openings_checked' ORDER BY id DESC LIMIT 1").fetchone()
    return datetime.fromisoformat(row[0]) if row else None


# --- finding boards for my companies ----------------------------------------------------------------------

def slugs(company: str) -> list[str]:
    """Board names a company might use: 'Sarvam AI Pvt. Ltd.' -> sarvamai, sarvam-ai, sarvam."""
    words = re.findall(r"[a-z0-9]+", _SUFFIXES.sub(" ", company.lower()))
    if not words:
        return []
    out = ["".join(words), "-".join(words), words[0]]
    return list(dict.fromkeys(s for s in out if len(s) >= 3))


class Found(BaseModel):
    company: str
    board: Board
    openings: int


def discover(conn: sqlite3.Connection, client: httpx.Client | None = None, workers: int = 8) -> list[Found]:
    """For companies with contacts but no watched board: boards that exist under a likely name."""
    watched = {r["company_key"] for r in boards(conn)}
    companies = {}
    for r in conn.execute("SELECT company FROM contacts ORDER BY id"):
        if company_key(r[0]) not in watched:
            companies.setdefault(company_key(r[0]), r[0])
    tries = [(company, Board(kind=kind, token=name, url=BOARD_PAGES[kind].format(token=name)))
             for company in companies.values() for slug in slugs(company) for kind in DISCOVER_ON
             for name in dict.fromkeys((slug, slug.capitalize()) if kind == "lever" else (slug,))]   # Lever: 'Sprinto'
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)

    def attempt(item):
        company, board = item
        try:
            return company, board, len(fetch(board, client))
        except WatchError:
            return company, board, None
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(attempt, tries))
    finally:
        if own:
            client.close()
    best: dict[str, Found] = {}
    for company, board, count in results:
        if count and (company not in best or count > best[company].openings):
            best[company] = Found(company=company, board=board, openings=count)
    return sorted(best.values(), key=lambda f: f.company.lower())


# --- what I do with an opening -------------------------------------------------------------------------------

def new_openings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM openings WHERE status = 'new' ORDER BY first_seen DESC, company").fetchall()


def dismiss(conn: sqlite3.Connection, opening_id: int) -> None:
    with conn:
        conn.execute("UPDATE openings SET status = 'dismissed' WHERE id = ?", (opening_id,))


def use_as_job_link(conn: sqlite3.Connection, opening_id: int) -> int:
    """The opening's link becomes the job link of the company's contacts that have none. Returns how many."""
    row = conn.execute("SELECT * FROM openings WHERE id = ?", (opening_id,)).fetchone()
    if row is None:
        raise WatchError("that opening doesn't exist")
    ids = [r["id"] for r in conn.execute("SELECT id, company FROM contacts WHERE job_url = ''")
           if company_key(r["company"]) == row["company_key"]]
    with conn:
        for contact_id in ids:
            conn.execute("UPDATE contacts SET job_url = ? WHERE id = ?", (row["url"], contact_id))
        conn.execute("UPDATE openings SET status = 'used' WHERE id = ?", (opening_id,))
        db.log_event(conn, "opening_used", company=row["company"], url=row["url"], contacts=len(ids))
    return len(ids)
