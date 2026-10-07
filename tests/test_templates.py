import itertools

import pytest

from mailmate import db
from mailmate.templates import (FIELDS, OPT_OUT_LINE, Template, check_template, first_name, merge,
                                save_template, latest_templates, to_html, values_for)

BODY = """Hey {first_name},

I recently came across the news that {company} is hiring for the {role} role.

{personal_line}

I am {my_name} and I build AI agents.

GitHub: https://github.com/example

{signature}"""

TEMPLATE = Template(name="cold", subject="{role} at {company}?", body=BODY)

VALUES = {
    "first_name": "Priya",
    "company": "Nimbus Labs",
    "role": "GenAI Engineer",
    "personal_line": "Your new retrieval API caught my eye.",
    "my_name": "Shrish",
    "signature": "Thanks for your time,\nShrish",
}


def with_values(**changes) -> dict[str, str]:
    return {**VALUES, **changes}


# --- checking templates -------------------------------------------------------------------

def test_valid_template_has_no_problems():
    assert check_template(TEMPLATE.subject, BODY) == []


@pytest.mark.parametrize("subject, body, problem", [
    ("Hi", "Hello {compnay}", "unknown field {compnay} in the body"),
    ("Hi", "Hello { company }", "unknown field { company } in the body"),
    ("Hi", "Hello {company", "a stray '{' or '}' in the body"),
    ("Hi", "Hello company}", "a stray '{' or '}' in the body"),
    ("Hi", "Your team at [Company's name]", "'[Company's name]' in the body looks like a placeholder"),
    ("{personal_line}", "Hello", "{personal_line} can't be used in the subject"),
    ("", "Hello", "the subject is empty"),
    ("Hi", "  ", "the body is empty"),
])
def test_template_problems(subject, body, problem):
    assert any(p.startswith(problem) for p in check_template(subject, body)), check_template(subject, body)


# --- merging ------------------------------------------------------------------------------

def test_full_merge():
    email = merge(TEMPLATE, VALUES)
    assert email.ok
    assert email.subject == "GenAI Engineer at Nimbus Labs?"
    assert email.body_text == """Hey Priya,

I recently came across the news that Nimbus Labs is hiring for the GenAI Engineer role.

Your new retrieval API caught my eye.

I am Shrish and I build AI agents.

GitHub: https://github.com/example

If this isn't relevant, just let me know and I won't follow up.

Thanks for your time,
Shrish"""


def test_merge_is_deterministic():
    assert merge(TEMPLATE, VALUES) == merge(TEMPLATE, dict(VALUES))


def test_no_first_name_gives_plain_greeting():
    assert merge(TEMPLATE, with_values(first_name="")).body_text.startswith("Hey,\n\n")


def test_no_personal_line_removes_its_paragraph():
    body = merge(TEMPLATE, with_values(personal_line="")).body_text
    assert "role.\n\nI am Shrish" in body and "\n\n\n" not in body


@pytest.mark.parametrize("field", ["company", "role", "my_name"])
def test_missing_required_value_stops_the_merge(field):
    email = merge(TEMPLATE, with_values(**{field: "  "}))
    assert not email.ok and email.body_text == "" and field in email.errors[0]


def test_no_combination_of_empty_optional_fields_leaves_a_placeholder():
    optional = [f for f in FIELDS if f not in ("company", "role", "my_name")]
    for n in range(len(optional) + 1):
        for empty in itertools.combinations(optional, n):
            email = merge(TEMPLATE, with_values(**{f: "" for f in empty}))
            assert email.ok, empty
            assert "{" not in email.body_text + email.subject and "}" not in email.body_text
            assert "\n\n\n" not in email.body_text and " ," not in email.body_text


def test_a_value_containing_a_merge_field_is_refused():
    email = merge(TEMPLATE, with_values(personal_line="I admire {company}."))
    assert not email.ok and "still contains braces" in email.errors[0]


def test_an_invalid_template_is_never_merged():
    email = merge(Template(name="x", subject="Hi", body="Dear {compnay}"), VALUES)
    assert not email.ok and email.errors[0].startswith("unknown field {compnay}")


def test_opt_out_line_is_added_exactly_once_above_the_signature():
    body = merge(TEMPLATE, VALUES).body_text
    assert body.count(OPT_OUT_LINE) == 1
    assert body.index(OPT_OUT_LINE) < body.index("Thanks for your time")


def test_opt_out_line_already_in_the_template_is_not_repeated():
    template = Template(name="x", subject="Hi", body=f"Hello {{company}}.\n\n{OPT_OUT_LINE}\n\n{{signature}}")
    assert merge(template, VALUES).body_text.count(OPT_OUT_LINE) == 1


def test_opt_out_line_goes_last_without_a_signature():
    template = Template(name="x", subject="Hi", body="Hello {company}.")
    assert merge(template, VALUES).body_text == f"Hello Nimbus Labs.\n\n{OPT_OUT_LINE}"


def test_bold_markers_are_refused_because_plain_text_shows_them():
    assert check_template("Hi", "Hello **{company}**") == [
        "'**' in the body: emails are sent as plain text, where Gmail shows the asterisks instead of bold"]


def test_html_makes_links_clickable():
    email = merge(TEMPLATE, VALUES)
    assert '<a href="https://github.com/example">https://github.com/example</a>' in email.body_html


def test_html_escapes_text():
    assert to_html("a < b & **c**") == "<p>a &lt; b &amp; <b>c</b></p>"


@pytest.mark.parametrize("name, expected", [
    ("Priya Sharma", "Priya"),
    ("dr. ravi kumar", "Ravi"),
    ("PRIYA", "Priya"),
    ("McKenzie Rao", "McKenzie"),
    ("Mr. A. Gupta", ""),
    ("HR Team", ""),
    ("Talent Acquisition", ""),
    ("", ""),
])
def test_first_name(name, expected):
    assert first_name(name) == expected


def test_values_for_a_contact():
    contact = {"name": "Priya Sharma", "company": "Nimbus Labs", "role": "GenAI Engineer"}
    values = values_for(contact, {"my_name": "Shrish", "signature": "Thanks"}, "A line.")
    assert values == {"first_name": "Priya", "company": "Nimbus Labs", "role": "GenAI Engineer",
                      "personal_line": "A line.", "my_name": "Shrish", "signature": "Thanks"}


# --- storage ------------------------------------------------------------------------------

def test_saving_a_change_adds_a_version_and_keeps_the_old_one(conn):
    v1 = save_template(conn, "cold", "Hi {company}", BODY)
    assert save_template(conn, "cold", "Hi {company}", BODY + "\n") == v1   # no real change
    v2 = save_template(conn, "cold", "Hello {company}", BODY)
    assert (v1.version, v2.version) == (1, 2)
    assert [t.version for t in latest_templates(conn)] == [2]
    assert conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 2


def test_an_invalid_template_is_not_saved(conn):
    with pytest.raises(ValueError, match="unknown field"):
        save_template(conn, "cold", "Hi", "Dear {compnay}")
    assert latest_templates(conn) == []


def test_settings_round_trip(conn):
    db.set_settings(conn, my_name="Shrish", signature="Thanks,\r\nShrish\n")
    db.set_settings(conn, my_name="Shrish Das")
    assert db.get_settings(conn) == {"my_name": "Shrish Das", "signature": "Thanks,\nShrish"}
