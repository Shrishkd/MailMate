"""Checks on W1's personal sentence, and the retry/fallback flow. No network: W1 is a fake."""

import pytest

from mailmate.n8n import N8nError, Personalization, Source
from mailmate.sentence_check import MAX_CHARS, check_sentence, is_job_ad, normalize, personalize_checked

NEWS = Source(id=1, title="Nimbus Labs opens research lab in Pune",
              url="https://news.example/nimbus-pune",
              content="Nimbus Labs has opened a research lab in Pune focused on Indic speech models. The lab will "
                      "hire 40 researchers. Its open-source model Kestrel-7B tops the IndicBench leaderboard. "
                      "Nimbus Labs is one of the fastest growing AI startups in India, serving millions of users.")
JOB_AD = Source(id=2, title="GenAI Engineer at Nimbus Labs", url="https://wellfound.com/jobs/123-genai-engineer",
                content="Python, LangChain, RAG. Nimbus Labs raised a Series B from Sequoia.")
GOOD = "Nimbus Labs opening a Pune lab for Indic speech models fits the problems I most want to work on."


def check(sentence, sources=(NEWS,), **kw):
    kw = {"company": "Nimbus Labs", "role": "GenAI Engineer", "requirements": "Python, LangChain, RAG",
          "my_name": "Shrish", **kw}
    return check_sentence(sentence, list(sources), **kw)


def problems(sentence, **kw) -> str:
    return " | ".join(check(sentence, **kw).problems)


# --- accepted -------------------------------------------------------------------------------

def test_a_grounded_sentence_is_accepted():
    verdict = check(GOOD)
    assert verdict.ok, verdict.problems
    assert verdict.sentence == GOOD and verdict.usable_sources == [NEWS]


def test_typography_is_made_plain_ascii():
    assert normalize("“Nimbus’ lab‑work — in Pune…”") == "Nimbus' lab-work - in Pune..."
    verdict = check("Nimbus Labs’ new Pune lab for Indic speech models is the kind of work I’d like to join.")
    assert verdict.ok, verdict.problems
    assert verdict.sentence.isascii()


@pytest.mark.parametrize("sentence", [
    "Nimbus Labs hiring 40 researchers for its Pune lab is what made me apply.",   # number in source
    "Kestrel-7B topping the IndicBench leaderboard is why I want to join Nimbus Labs.",  # name + digits
    "Nimbus Labs serving millions of users in India with Indic speech models is the work I want to do.",
    "Nimbus Labs being one of the fastest growing AI startups in India is the reason I'm applying.",
])
def test_numbers_names_and_superlatives_from_a_cited_source_are_fine(sentence):
    assert check(sentence).ok, check(sentence).problems


def test_the_company_may_be_named_by_its_distinctive_word():
    assert check("Nimbus opening a Pune lab for Indic speech models fits the work I want to do.").ok


# --- rejected: shape --------------------------------------------------------------------------

def test_empty_sentence():
    assert check("").problems == ["W1 wrote no sentence"]


def test_too_long():
    long = "Nimbus Labs opened a research lab in Pune " + "for Indic speech models " * 10 + "."
    assert f"the limit is {MAX_CHARS}" in problems(long)


@pytest.mark.parametrize("sentence, problem", [
    ("Nimbus Labs opened a lab in Pune.\nIt focuses on Indic speech.", "more than one line"),
    ("Nimbus Labs opened a lab in Pune. It focuses on Indic speech.", "more than one sentence"),
    ("Nimbus Labs opened a lab in Pune", "doesn't end with a full stop"),
    ("Nimbus Labs opened a lab in [city] for Indic speech.", "placeholder"),
    ("{company} opened a lab in Pune for Indic speech.", "placeholder"),
    ("Nimbus Labs opened a lab in XXX for Indic speech.", "placeholder"),
    ("Nimbus Labs opened a Pune lab, see https://news.example/nimbus for details.", "link"),
    ("Nimbus Labs opened a Pune lab, see nimbuslabs.ai for details.", "link"),
    ("Nimbus Labs opened a Pune lab, write to jobs@nimbuslabs.ai about it.", "email address"),
    ("Hi, Nimbus Labs opened a research lab in Pune.", "greeting"),
    ("Nimbus Labs opened a Pune lab, so let me know if we can talk.", "greeting, sign-off"),
    ("Nimbus Labs opened a Pune lab, and Shrish would love to help there.", "mentions my name"),
])
def test_shape_problems(sentence, problem):
    assert problem in problems(sentence)


def test_abbreviations_are_not_sentence_ends():
    assert check("Nimbus Labs Inc. opening a Pune lab for Indic speech models fits the work I want to do.").ok


# --- rejected: content ------------------------------------------------------------------------

@pytest.mark.parametrize("sentence", [
    "With my experience in speech models, Nimbus Labs' new Pune lab is a great fit.",
    "I've built Indic speech models, so the Nimbus Labs Pune lab caught my eye.",
    "I am a speech engineer and the Nimbus Labs Pune lab caught my eye.",
    "Nimbus Labs' Pune lab matches my 3 years of speech work.",
])
def test_claims_about_me_are_rejected(sentence):
    assert "claim about me" in problems(sentence)


@pytest.mark.parametrize("sentence", [
    "I'm excited by Nimbus Labs opening a research lab in Pune for Indic speech models.",
    "I am really interested in Nimbus Labs' new research lab in Pune.",
    "Nimbus Labs' Pune lab for Indic speech models is a real game-changer.",
    "Nimbus Labs' cutting-edge Pune lab works on Indic speech models.",
])
def test_cliches_are_rejected(sentence):
    assert "flattery or a stock phrase" in problems(sentence)


def test_tools_from_the_requirements_are_rejected_even_if_a_source_has_them():
    assert "names tools or skills (python, langchain, rag)" in problems(
        "Nimbus Labs uses Python, LangChain and RAG in its new Pune lab.", sources=(NEWS, JOB_AD))


def test_requirement_phrases_are_rejected():
    assert "Indic speech" in problems(GOOD, requirements="Indic speech, PyTorch")


def test_the_line_must_say_why_it_makes_me_want_to_apply():
    assert "not why it makes me want to apply" in problems(
        "Nimbus Labs opened a research lab in Pune focused on Indic speech models.")


@pytest.mark.parametrize("sentence", [
    "Nimbus Labs opening a Pune lab for Indic speech models is what made me apply.",
    "Seeing Nimbus Labs open a Pune lab for Indic speech models is the reason I'm applying.",
    "Nimbus Labs' new Pune lab for Indic speech models excites me, since that is the work I want to do.",
    "Nimbus Labs' Pune lab for Indic speech models drew me to the GenAI Engineer role.",
])
def test_ways_of_saying_why(sentence):
    assert check(sentence).ok, check(sentence).problems


def test_a_line_full_of_statistics_is_rejected():
    many = NEWS.model_copy(update={"content": NEWS.content + " Accuracy rose from 48.6% to 80.0% and errors fell from 35.0% to 8.0%."})
    assert "lists 4 figures (48.6%, 80.0%, 35.0%, 8.0%); use at most 2" in problems(
        "Nimbus Labs raising accuracy from 48.6% to 80.0% and cutting errors from 35.0% to 8.0% made me apply.",
        sources=(many,))


def test_the_company_must_be_named():
    assert "doesn't name Nimbus Labs" in problems("Opening a research lab in Pune for Indic speech models is smart.")


# --- rejected: grounding ----------------------------------------------------------------------

def test_no_source_cited():
    assert "cites no source" in problems(GOOD, sources=())


def test_job_ads_alone_dont_count():
    assert "only sources are job ads (wellfound.com)" in problems(GOOD, sources=(JOB_AD,))


@pytest.mark.parametrize("url, expected", [
    ("https://wellfound.com/jobs/3000999-generative-ai-engineer", True),
    ("https://www.naukri.com/job-listings-genai", True),
    ("https://in.indeed.com/viewjob?jk=1", True),
    ("https://www.linkedin.com/jobs/view/123", True),
    ("https://boards.greenhouse.io/nimbus/jobs/1", True),
    ("https://nimbuslabs.ai/careers/genai-engineer", True),
    ("https://www.freshworks.com/press-releases/freddy-ai-agent/", False),
    ("https://medium.com/freshworks-engineering-blog/freddy-ai-insights", False),
])
def test_job_ad_detection(url, expected):
    assert is_job_ad(url) is expected


def test_a_fact_only_in_a_job_ad_is_not_grounded():
    assert "'Series', 'B', 'Sequoia' are not in the sources" in problems(
        "Nimbus Labs raising a Series B from Sequoia shows real momentum behind Indic speech.", sources=(NEWS, JOB_AD))


@pytest.mark.parametrize("sentence, unsupported", [
    ("Nimbus Labs hiring 400 researchers for its Pune lab shows how serious it is about Indic speech.", "'400'"),
    ("Nimbus Labs raising $50M for its Pune lab shows how serious it is about Indic speech.", "'50M'"),
    ("Nimbus Labs serving billions of users with Indic speech models is remarkable.", "'billions'"),
    ("Nimbus Labs being the largest Indic speech company makes its Pune lab news stand out.", "'largest'"),
    ("Nimbus Labs opening a Pune lab with Google for Indic speech models fits my interests.", "'Google'"),
    ("Nimbus Labs' Pune lab building on its Falcon-2 model for Indic speech fits my interests.", "'Falcon'"),
])
def test_facts_not_in_the_sources_are_rejected(sentence, unsupported):
    assert unsupported in problems(sentence)


# --- one retry with feedback, then no sentence ------------------------------------------------

CONTACT = {"company": "Nimbus Labs", "role": "GenAI Engineer", "requirements": "Python", "job_url": "",
           "email": "priya@nimbuslabs.ai"}
BAD = "I'm excited by Nimbus Labs opening a research lab in Pune."


class FakeW1:
    """Answers with the queued results in order; records every call's arguments."""
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def w1(sentence, sources=(NEWS,)):
    return Personalization(sentence=sentence, sources=list(sources), model="gpt-oss:120b", search_ok=bool(sources))


def test_accepted_first_time_means_one_call():
    fake = FakeW1(w1(GOOD))
    line = personalize_checked(fake, CONTACT, my_name="Shrish")
    assert line.sentence == GOOD and line.sources == [NEWS] and not line.fell_back
    assert len(fake.calls) == 1 and "feedback" not in fake.calls[0]


def test_a_rejected_sentence_is_retried_once_with_the_reasons():
    fake = FakeW1(w1(BAD), w1(GOOD))
    line = personalize_checked(fake, CONTACT)
    assert line.sentence == GOOD
    assert len(line.attempts) == 2 and line.attempts[0].problems and not line.attempts[1].problems
    feedback = fake.calls[1]["feedback"]
    assert BAD in feedback and "flattery or a stock phrase" in feedback


def test_two_rejections_fall_back_to_no_sentence():
    fake = FakeW1(w1(BAD), w1(BAD))
    line = personalize_checked(fake, CONTACT)
    assert line.fell_back and line.sentence == "" and line.sources == []
    assert len(fake.calls) == 2


def test_no_sentence_from_w1_is_not_retried():
    fake = FakeW1(w1("", sources=()))
    line = personalize_checked(fake, CONTACT)
    assert line.fell_back and len(fake.calls) == 1


def test_a_failed_first_call_is_raised():
    with pytest.raises(N8nError):
        personalize_checked(FakeW1(N8nError("n8n down")), CONTACT)


def test_a_failed_retry_falls_back():
    line = personalize_checked(FakeW1(w1(BAD), N8nError("n8n down")), CONTACT)
    assert line.fell_back and "the retry failed: n8n down" in line.attempts[-1].problems[0]
