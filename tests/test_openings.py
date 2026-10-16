"""Openings watcher: recognising boards, reading each platform's feed, keywords, new-once, finding
boards, using an opening. Every feed is faked (httpx.MockTransport)."""

import json

import httpx
import pytest

from mailmate import openings
from mailmate.openings import Board, WatchError

FEEDS = {   # path -> body, in each platform's real shape (trimmed)
    "/v1/boards/nimbus/jobs": {"jobs": [
        {"title": "Senior AI Engineer", "absolute_url": "https://boards.greenhouse.io/nimbus/jobs/1",
         "location": {"name": "Bengaluru"}},
        {"title": "Office Manager", "absolute_url": "https://boards.greenhouse.io/nimbus/jobs/2", "location": {"name": "Pune"}}]},
    "/v0/postings/Sprinto": [
        {"text": "GenAI Engineer", "hostedUrl": "https://jobs.lever.co/Sprinto/a", "categories": {"location": "Remote (India)"}},
        {"text": "Account Manager", "hostedUrl": "https://jobs.lever.co/Sprinto/b", "categories": {}}],
    "/posting-api/job-board/quill": {"jobs": [
        {"title": "Machine Learning Engineer, LLM", "jobUrl": "https://jobs.ashbyhq.com/quill/x", "location": "Remote"},
        {"title": "Hidden AI role", "jobUrl": "https://jobs.ashbyhq.com/quill/y", "isListed": False}]},
    "/api/v1/widget/accounts/orbit": {"jobs": [{"title": "Data Scientist", "url": "https://apply.workable.com/orbit/j/1",
                                                "city": "Mumbai", "country": "India"}]},
    "/v1/companies/Delta/postings": {"content": [{"id": "77", "name": "NLP Researcher", "location": {"city": "Delhi"}}]},
    "/api/offers/": {"offers": [{"title": "Computer Vision Intern", "careers_url": "https://echo.recruitee.com/o/cv"}]},
    "/careers": "<html><a href='/jobs/1'>Applied Scientist, Agents</a> <a href='#top'>Top</a>"
                "<a href='https://x.example/jobs/2'>Sales Lead</a></html>",
}


def transport(seen: list | None = None):
    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        body = FEEDS.get(request.url.path)
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)
    return httpx.MockTransport(answer)


def client(seen=None):
    return httpx.Client(transport=transport(seen))


@pytest.mark.parametrize("url, kind, token", [
    ("https://boards.greenhouse.io/nimbus/jobs/4461450008", "greenhouse", "nimbus"),
    ("https://job-boards.greenhouse.io/nimbus", "greenhouse", "nimbus"),
    ("https://boards.greenhouse.io/embed/job_board?for=nimbus&b=x", "greenhouse", "nimbus"),
    ("https://jobs.lever.co/Sprinto/c325db0c-2182", "lever", "Sprinto"),         # Lever names are case-sensitive
    ("https://jobs.ashbyhq.com/quill/8fb1615c", "ashby", "quill"),
    ("https://apply.workable.com/orbit/j/ABC123/", "workable", "orbit"),
    ("https://jobs.smartrecruiters.com/Delta/743999", "smartrecruiters", "Delta"),
    ("https://echo.recruitee.com/o/ml-engineer", "recruitee", "echo"),
])
def test_boards_are_recognised_from_job_links(url, kind, token):
    board = openings.detect(url)
    assert (board.kind, board.token) == (kind, token)


def test_other_links_are_plain_pages_or_refused():
    assert openings.detect("https://internshala.com/job/detail/ai-engineer") is None
    assert openings.board_for("https://acme.example/careers").kind == "page"
    with pytest.raises(WatchError):
        openings.board_for("acme.example/careers")


@pytest.mark.parametrize("board, titles", [
    (Board(kind="greenhouse", token="nimbus", url=""), ["Senior AI Engineer", "Office Manager"]),
    (Board(kind="lever", token="Sprinto", url=""), ["GenAI Engineer", "Account Manager"]),
    (Board(kind="ashby", token="quill", url=""), ["Machine Learning Engineer, LLM"]),                # unlisted left out
    (Board(kind="workable", token="orbit", url=""), ["Data Scientist"]),
    (Board(kind="smartrecruiters", token="Delta", url=""), ["NLP Researcher"]),
    (Board(kind="recruitee", token="echo", url=""), ["Computer Vision Intern"]),
    (Board(kind="page", url="https://acme.example/careers"), ["Applied Scientist, Agents", "Sales Lead"]),
])
def test_each_platform_is_read(board, titles):
    found = openings.fetch(board, client())
    assert [o.title for o in found] == titles
    assert all(o.url.startswith("https://") for o in found)


def test_keywords_match_whole_words():
    words = ["AI", "LLM", "data scientist"]
    assert openings.matches("Senior AI Engineer", words) and openings.matches("Gen AI lead", words)
    assert openings.matches("Lead Data Scientist", words) and openings.matches("LLM Ops", words)
    assert not openings.matches("Maintenance Engineer", words) and not openings.matches("Sales Lead", words)


def test_a_missing_board_says_so():
    with pytest.raises(WatchError, match="no Lever board called 'nobody'"):
        openings.fetch(Board(kind="lever", token="nobody", url=""), client())


@pytest.fixture
def watched(conn):
    conn.executemany("INSERT INTO contacts (company, role, email, job_url, created_at) VALUES (?, 'AI', ?, ?, '2026')", [
        ("Sprinto", "a@sprinto.example", "https://jobs.lever.co/Sprinto/c325"),
        ("Sprinto", "b@sprinto.example", ""),
        ("Nimbus", "n@nimbus.example", ""),
        ("Acme", "x@acme.example", "https://internshala.com/job/1")])
    return conn


def test_job_links_start_the_watch_list_and_a_check_finds_new_openings_once(watched):
    assert openings.watch_from_job_links(watched) == ["Sprinto"]
    assert openings.watch_from_job_links(watched) == []
    openings.watch(watched, "Nimbus", Board(kind="greenhouse", token="nimbus", url="https://boards.greenhouse.io/nimbus"))
    openings.watch(watched, "Acme", Board(kind="page", url="https://acme.example/empty"))
    new, problems = openings.check(watched, client())
    assert sorted(new) == ["Nimbus: Senior AI Engineer", "Sprinto: GenAI Engineer"]     # AI titles only
    assert problems == ["Acme: page not found"]
    assert openings.check(watched, client())[0] == []                                   # nothing new the second time
    acme = next(b for b in openings.boards(watched) if b["company"] == "Acme")
    assert acme["last_error"] == "page not found"


def test_finding_boards_tries_likely_names_and_only_suggests(watched):
    seen = []
    found = openings.discover(watched, client(seen))
    assert [(f.company, f.board.kind, f.board.token, f.openings) for f in found] == [
        ("Nimbus", "greenhouse", "nimbus", 2), ("Sprinto", "lever", "Sprinto", 2)]
    assert any(u.endswith("/v0/postings/sprinto?mode=json") for u in seen)                # lower case tried first
    assert openings.boards(watched) == []                                                 # nothing watched unasked


def test_an_opening_fills_empty_job_links_of_its_company(watched):
    openings.watch_from_job_links(watched)
    openings.check(watched, client())
    opening = openings.new_openings(watched)[0]
    assert openings.use_as_job_link(watched, opening["id"]) == 1                          # b@ had none; a@ keeps its own
    links = dict(watched.execute("SELECT email, job_url FROM contacts WHERE company = 'Sprinto'").fetchall())
    assert links == {"a@sprinto.example": "https://jobs.lever.co/Sprinto/c325",
                     "b@sprinto.example": "https://jobs.lever.co/Sprinto/a"}
    assert openings.new_openings(watched) == []


@pytest.mark.parametrize("company, expected", [("Sarvam AI Pvt. Ltd.", ["sarvamai", "sarvam-ai", "sarvam"]),
                                               ("Observe.AI", ["observeai", "observe-ai", "observe"]),
                                               ("Labs", [])])
def test_board_names_to_try(company, expected):
    assert openings.slugs(company) == expected


def test_feeds_are_valid_json_fixtures():
    assert all(isinstance(v, (dict, list, str)) for v in FEEDS.values()) and json.dumps(FEEDS["/api/offers/"])
