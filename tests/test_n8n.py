"""The n8n client against a fake network (httpx.MockTransport). No real webhook is called."""

import json

import httpx
import pytest

from mailmate.config import ConfigError, N8nSettings, n8n_settings, read_env_file
from mailmate.n8n import N8nClient, N8nError

SECRET = "s3cret-token-value"
SETTINGS = N8nSettings(base_url="https://demo.app.n8n.cloud", token=SECRET)
W1_ANSWER = {
    "sentence": "Nimbus Labs recently opened a research lab in Pune.",
    "sources": [{"id": 1, "title": "Nimbus opens Pune lab", "url": "https://news.example/nimbus", "content": "..."}],
    "model": "gpt-oss:120b",
    "search_ok": True,
}


def client_for(*responses, calls=None):
    """A client whose network answers with `responses` in order (an Exception is raised instead)."""
    queue = list(responses)
    calls = [] if calls is None else calls

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return N8nClient(SETTINGS, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_personalize_sends_the_secret_header_and_parses_the_answer():
    calls = []
    result = client_for(httpx.Response(200, json=W1_ANSWER), calls=calls).personalize(
        "Nimbus Labs", "GenAI Engineer", "Python")
    [request] = calls
    assert str(request.url) == "https://demo.app.n8n.cloud/webhook/MailMate-personalize"
    assert request.headers["X-MailMate-Token"] == SECRET
    assert json.loads(request.content) == {"company": "Nimbus Labs", "role": "GenAI Engineer", "requirements": "Python"}
    assert result.sentence == W1_ANSWER["sentence"] and result.sources[0].url == "https://news.example/nimbus"


def test_retry_feedback_is_sent_only_when_given():
    calls = []
    client = client_for(httpx.Response(200, json=W1_ANSWER), httpx.Response(200, json=W1_ANSWER), calls=calls)
    client.personalize("A", "B", job_url="https://a.example/job")
    client.personalize("A", "B", feedback="too long")
    assert json.loads(calls[0].content) == {"company": "A", "role": "B", "requirements": "",
                                            "job_url": "https://a.example/job"}
    assert json.loads(calls[1].content)["feedback"] == "too long"


def test_answer_wrapped_in_a_list_is_accepted():
    assert client_for(httpx.Response(200, json=[W1_ANSWER])).personalize("A", "B").search_ok


def test_network_blips_are_retried():
    calls = []
    client = client_for(httpx.ConnectError("no such host"), httpx.Response(503), httpx.Response(200, json=W1_ANSWER),
                        calls=calls)
    assert client.personalize("A", "B").sentence
    assert len(calls) == 3


def test_gives_up_after_four_tries_with_a_clear_message():
    client = client_for(*[httpx.ReadTimeout("slow")] * 4)
    with pytest.raises(N8nError, match=r"couldn't reach n8n after 4 tries: network error \(ReadTimeout\)"):
        client.personalize("A", "B")


@pytest.mark.parametrize("status, message", [
    (403, "n8n refused the webhook token"),
    (401, "n8n refused the webhook token"),
    (404, "not found \\(404\\). Is the workflow published"),
    (500, "the n8n workflow failed \\(500\\)"),
])
def test_permanent_errors_are_not_retried(status, message):
    calls = []
    client = client_for(httpx.Response(status, text="error"), calls=calls)
    with pytest.raises(N8nError, match=message):
        client.personalize("A", "B")
    assert len(calls) == 1


def test_error_messages_never_contain_the_token():
    client = client_for(httpx.Response(500, text=f"header was {SECRET}"))
    with pytest.raises(N8nError) as info:
        client.personalize("A", "B")
    assert SECRET not in str(info.value) and "***" in str(info.value)


@pytest.mark.parametrize("answer", [
    httpx.Response(200, text="<html>oops</html>"),
    httpx.Response(200, json={"sentence": "x", "sources": [{"title": "no id"}]}),
    httpx.Response(200, json={**W1_ANSWER, "sources": W1_ANSWER["sources"] * 2}),
])
def test_unexpected_answers_are_errors(answer):
    with pytest.raises(N8nError):
        client_for(answer).personalize("A", "B")


# --- .env -----------------------------------------------------------------------------------

def test_env_file_with_bom_and_crlf(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes("﻿# comment\r\nN8N_BASE_URL=https://demo.app.n8n.cloud/\r\nN8N_WEBHOOK_TOKEN=abc\r\n".encode())
    settings = n8n_settings(read_env_file(env))
    assert (settings.base_url, settings.token) == ("https://demo.app.n8n.cloud", "abc")


def test_token_is_hidden_from_repr():
    assert SECRET not in repr(SETTINGS)


@pytest.mark.parametrize("env, problem", [
    ({}, "N8N_BASE_URL is not set"),
    ({"N8N_BASE_URL": "https://<name>.app.n8n.cloud", "N8N_WEBHOOK_TOKEN": "x"}, "N8N_BASE_URL is not set"),
    ({"N8N_BASE_URL": "http://demo.app.n8n.cloud", "N8N_WEBHOOK_TOKEN": "x"}, "must start with https://"),
    ({"N8N_BASE_URL": "https://demo.app.n8n.cloud"}, "N8N_WEBHOOK_TOKEN is not set"),
])
def test_missing_settings_name_the_key(env, problem):
    with pytest.raises(ConfigError, match=problem):
        n8n_settings(env)
