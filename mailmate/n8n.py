"""Calls to the n8n webhooks. Laptop -> cloud only; n8n can't call back.

Every call carries the secret X-MailMate-Token header. The home network is flaky, so network
failures and gateway errors (502/503/504/429) are retried with a growing pause. Answers that
won't change on retry (wrong token, workflow not published, workflow crashed) fail at once with
a message that says what to fix. Error messages never contain the token.
"""

import json
import time
from typing import Callable

import httpx
from pydantic import BaseModel, ValidationError

from mailmate.config import N8nSettings

TOKEN_HEADER = "X-MailMate-Token"
RETRY_DELAYS = (3, 10, 20)             # seconds before the 2nd, 3rd and 4th try
_RETRY_STATUS = {429, 502, 503, 504}
# W1 does a web search and an LLM call; 20-40 s is normal, so allow plenty before giving up.
TIMEOUT = httpx.Timeout(120.0, connect=15.0)


class N8nError(RuntimeError):
    pass


class Source(BaseModel):
    id: int
    title: str = ""
    url: str = ""
    content: str = ""


class Personalization(BaseModel):
    """W1's answer. `sentence` is untrusted model output until Step 4's checks pass it."""
    sentence: str = ""
    sources: list[Source] = []
    model: str = ""
    search_ok: bool = False


class SendAccepted(BaseModel):
    """W2's immediate answer: it took the batch. Sending itself goes on inside n8n."""
    accepted: int
    batch_id: int


class N8nClient:
    def __init__(self, settings: N8nSettings, *, transport: httpx.BaseTransport | None = None,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS, sleep: Callable[[float], None] = time.sleep):
        self._settings = settings
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._http = httpx.Client(timeout=TIMEOUT, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _redact(self, text: str) -> str:
        return text.replace(self._settings.token, "***") if self._settings.token else text

    def _post(self, path: str, *, json_body: dict | None = None, data: dict | None = None,
              files: dict | None = None, repeatable: bool = True) -> dict:
        """POST to a webhook. `repeatable=False` is for calls with side effects (sending email):
        those are retried only when the request certainly never left this laptop, because a
        lost answer could otherwise turn into a second send."""
        url = f"{self._settings.base_url}/webhook/{path}"
        last_problem = ""
        for attempt in range(len(self._retry_delays) + 1):
            if attempt:
                self._sleep(self._retry_delays[attempt - 1])
            try:
                response = self._http.post(url, json=json_body, data=data, files=files,
                                           headers={TOKEN_HEADER: self._settings.token})
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:   # never reached n8n
                last_problem = f"network error ({type(exc).__name__})"
                continue
            except httpx.TransportError as exc:
                if not repeatable:
                    raise N8nError(f"the connection broke after the request was sent ({type(exc).__name__}). "
                                   "It may or may not have reached n8n: check n8n -> Executions before trying "
                                   "again, so nothing is sent twice.") from None
                last_problem = f"network error ({type(exc).__name__})"
                continue
            status = response.status_code
            if status in _RETRY_STATUS:
                if not repeatable:
                    raise N8nError(f"n8n answered {status}. The request may have started the workflow: check "
                                   "n8n -> Executions before trying again, so nothing is sent twice.")
                last_problem = f"n8n answered {status}"
                continue
            if status in (401, 403):
                raise N8nError(f"n8n refused the webhook token ({status}). N8N_WEBHOOK_TOKEN in .env must match "
                               "the value of the 'MailMate webhook token' credential in n8n.")
            if status == 404:
                raise N8nError(f"webhook '{path}' not found (404). Is the workflow published in n8n "
                               "(Publish button), and is the Webhook path spelled exactly like that?")
            if status >= 400:
                raise N8nError(f"the n8n workflow failed ({status}): {self._redact(response.text[:300])}. "
                               "Open n8n -> Executions to see which node failed.")
            try:
                return response.json()
            except ValueError:
                raise N8nError(f"n8n answered {status} but not with JSON: {self._redact(response.text[:200])!r}")
        tries = len(self._retry_delays) + 1
        raise N8nError(f"couldn't reach n8n after {tries} tries: {last_problem}. Check your internet connection.")

    def send_batch(self, payload: dict, resume: bytes, resume_name: str = "resume.pdf") -> SendAccepted:
        """Hand a batch to W2. W2 answers at once and keeps sending in n8n for minutes or hours."""
        data = self._post("MailMate-send", data={"payload": json.dumps(payload)},
                          files={"resume": (resume_name, resume, "application/pdf")}, repeatable=False)
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        try:
            accepted = SendAccepted.model_validate(data)
        except ValidationError as exc:
            raise N8nError(f"W2 answered in an unexpected shape: {exc.errors()[0]['msg']}") from None
        if accepted.batch_id != payload["batch_id"] or accepted.accepted != len(payload["emails"]):
            raise N8nError(f"W2 accepted {accepted.accepted} email(s) of batch {accepted.batch_id}, but MailMate "
                           f"sent {len(payload['emails'])} of batch {payload['batch_id']}; check n8n -> Executions")
        return accepted

    def personalize(self, company: str, role: str, requirements: str = "", job_url: str = "",
                    feedback: str = "") -> Personalization:
        """`feedback`: why the previous sentence was rejected, so the retry can fix it."""
        payload = {"company": company, "role": role, "requirements": requirements}
        if job_url:
            payload["job_url"] = job_url
        if feedback:
            payload["feedback"] = feedback
        data = self._post("MailMate-personalize", json_body=payload)
        if isinstance(data, list) and len(data) == 1:   # some n8n respond modes wrap the item in a list
            data = data[0]
        try:
            result = Personalization.model_validate(data)
        except ValidationError as exc:
            raise N8nError(f"W1 answered in an unexpected shape: {exc.errors()[0]['msg']}") from None
        known = {s.id for s in result.sources}
        if len(known) != len(result.sources):
            raise N8nError("W1 returned two sources with the same id")
        return result
