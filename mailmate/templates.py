"""Email templates: checking them, and merging one contact into one finished email.

Merging is plain code and deterministic: the same template and values always give the same
email. A merged email can never contain a raw {placeholder}: a missing required value stops
the merge with a reason, an empty optional value is removed cleanly, and a final check
refuses any output that still has a merge field in it.
"""

import html
import re
import sqlite3

from pydantic import BaseModel

from mailmate import db

FIELDS = ("first_name", "company", "role", "personal_line", "my_name", "signature")
REQUIRED_VALUES = ("company", "role", "my_name")   # no email is drafted without these
SUBJECT_FIELDS = ("first_name", "company", "role", "my_name")

# Rule 6: every email carries this line, just above the signature.
OPT_OUT_LINE = "If this isn't relevant, just let me know and I won't follow up."

_FIELD = re.compile(r"\{([^{}\n]*)\}")
# A field plus the space before it, so an empty value doesn't leave "Hey ," behind.
_FIELD_WITH_SPACE = re.compile(r"(?P<space>[ \t]?)\{(?P<name>[a-z_]+)\}")
_MANUAL_PLACEHOLDER = re.compile(r"\[[^\]\n]{0,60}\]")   # "[Company's name]" left from hand-editing
_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam"}
_NOT_A_PERSON = ("team", "hr", "talent", "recruit", "careers", "hiring", "people", "jobs")


class Template(BaseModel):
    name: str
    subject: str
    body: str
    version: int = 0
    id: int | None = None


class MergedEmail(BaseModel):
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    errors: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors


# --- checking a template ------------------------------------------------------------------

def check_template(subject: str, body: str) -> list[str]:
    """Problems that stop a template from being saved."""
    errors = []
    if not subject.strip():
        errors.append("the subject is empty")
    if "\n" in subject.strip():
        errors.append("the subject must be a single line")
    if not body.strip():
        errors.append("the body is empty")
    for where, text, allowed in (("subject", subject, SUBJECT_FIELDS), ("body", body, FIELDS)):
        for name in _FIELD.findall(text):
            if name not in FIELDS:
                errors.append(f"unknown field {{{name}}} in the {where}; use one of "
                              + ", ".join(f"{{{f}}}" for f in FIELDS))
            elif name not in allowed:
                errors.append(f"{{{name}}} can't be used in the {where}")
        if _FIELD.sub("", text).count("{") or _FIELD.sub("", text).count("}"):
            errors.append(f"a stray '{{' or '}}' in the {where}; merge fields look like {{company}}")
        for placeholder in _MANUAL_PLACEHOLDER.findall(text):
            errors.append(f"'{placeholder}' in the {where} looks like a placeholder to fill by hand; "
                          f"use a merge field such as {{company}}")
        if "**" in text:
            errors.append(f"'**' in the {where}: emails are sent as plain text, where Gmail shows "
                          f"the asterisks instead of bold")
    return errors


# --- merging ------------------------------------------------------------------------------

def first_name(full_name: str) -> str:
    """'Dr. ravi kumar' -> 'Ravi'. Empty when there is no usable personal name ('HR Team')."""
    words = [w for w in re.split(r"\s+", full_name.strip()) if w]
    if any(marker in w.lower() for w in words for marker in _NOT_A_PERSON):
        return ""
    words = [w for w in words if w.lower().rstrip(".") not in _HONORIFICS]
    if not words or len(words[0].rstrip(".")) < 2 or not words[0][0].isalpha():
        return ""
    word = words[0]
    return word.capitalize() if word.islower() or word.isupper() else word


def _fill(text: str, values: dict[str, str]) -> str:
    def replace(m: re.Match) -> str:
        value = values.get(m["name"], "").strip()
        if value:
            return m["space"] + value
        following = m.string[m.end():m.end() + 1]
        return "" if following in ("", " ", "\n", ",", ".", "!", "?", ";", ":") else m["space"]
    return _FIELD_WITH_SPACE.sub(replace, text)


def _drop_empty_field_lines(body: str, values: dict[str, str]) -> str:
    """A line holding only an empty field (like {personal_line}) disappears with its paragraph."""
    kept = []
    for line in body.split("\n"):
        names = _FIELD.findall(line)
        if names and not _FIELD.sub("", line).strip() and not any(values.get(n, "").strip() for n in names):
            continue
        kept.append(line)
    return "\n".join(kept)


def _with_opt_out(body: str) -> str:
    if OPT_OUT_LINE in body:
        return body
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if "{signature}" in line:
            return "\n".join(lines[:i] + [OPT_OUT_LINE, ""] + lines[i:])
    return body.rstrip() + "\n\n" + OPT_OUT_LINE


def _tidy(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def to_html(text: str) -> str:
    """Simple HTML: paragraphs, line breaks, **bold**, clickable links. No images, no styling."""
    def paragraph(block: str) -> str:
        escaped = html.escape(block, quote=False)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.S)
        escaped = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', escaped)
        return "<p>" + escaped.replace("\n", "<br>\n") + "</p>"
    return "\n".join(paragraph(b) for b in text.split("\n\n") if b.strip())


def merge(template: Template, values: dict[str, str]) -> MergedEmail:
    missing = [f for f in REQUIRED_VALUES if not values.get(f, "").strip()]
    if missing:
        return MergedEmail(errors=[f"missing {', '.join(missing)}; this email can't be written"])
    problems = check_template(template.subject, template.body)
    if problems:
        return MergedEmail(errors=problems)

    subject = " ".join(_fill(template.subject, values).split())
    body = _tidy(_fill(_drop_empty_field_lines(_with_opt_out(template.body), values), values))

    # Belt and braces: whatever happened above, a merge field never reaches a recruiter.
    leftovers = sorted({m for text in (subject, body) for m in _FIELD.findall(text)})
    if leftovers or "{" in subject + body or "}" in subject + body:
        return MergedEmail(errors=["the merged email still contains braces or a merge field "
                                   f"({', '.join(leftovers) or 'a stray brace'}); check the values"])
    return MergedEmail(subject=subject, body_text=body, body_html=to_html(body))


def values_for(contact, settings: dict[str, str], personal_line: str = "") -> dict[str, str]:
    return {
        "first_name": first_name(contact["name"] or ""),
        "company": contact["company"],
        "role": contact["role"],
        "personal_line": personal_line,
        "my_name": settings.get("my_name", ""),
        "signature": settings.get("signature", ""),
    }


# --- storage ------------------------------------------------------------------------------
# Templates are never edited in place: saving a change adds a new version, so every sent
# email points at the exact text it came from and reply rates can be compared per version.

def latest_templates(conn: sqlite3.Connection) -> list[Template]:
    rows = conn.execute(
        "SELECT * FROM templates t WHERE version = (SELECT MAX(version) FROM templates WHERE name = t.name)"
        " ORDER BY name").fetchall()
    return [Template(**{k: r[k] for k in ("id", "name", "version", "subject", "body")}) for r in rows]


def save_template(conn: sqlite3.Connection, name: str, subject: str, body: str) -> Template:
    """Save as a new version. Raises ValueError with the problems if the template is invalid."""
    name, subject, body = name.strip(), subject.strip(), body.replace("\r\n", "\n").strip()
    if not name:
        raise ValueError("the template needs a name")
    problems = check_template(subject, body)
    if problems:
        raise ValueError("; ".join(problems))
    current = next((t for t in latest_templates(conn) if t.name == name), None)
    if current and (current.subject, current.body) == (subject, body):
        return current
    version = current.version + 1 if current else 1
    with conn:
        cur = conn.execute("INSERT INTO templates (name, version, subject, body, created_at) VALUES (?, ?, ?, ?, ?)",
                           (name, version, subject, body, db.now()))
        db.log_event(conn, "template_saved", template_id=cur.lastrowid, name=name, version=version)
    return Template(id=cur.lastrowid, name=name, version=version, subject=subject, body=body)
