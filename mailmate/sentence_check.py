"""Checks on W1's personal sentence (rule 4). Plain code: the model writes, code verifies.

The sentence is untrusted model output built from untrusted web text. It goes into an email
only if every check passes. These checks are deliberately simple and strict: a false rejection
costs one retry or an email without a personal line; a false acceptance puts an invented fact
in front of a recruiter.

Grounding is checked through what can be checked mechanically: every name, number, size word
("millions") and superlative ("largest") in the sentence must appear in the text of a source
it cites. Job-board pages don't count as sources: the line should be about the company, not
the posting.
"""

import re
from collections.abc import Callable
from urllib.parse import urlparse

from pydantic import BaseModel

from mailmate.n8n import N8nError, Personalization, Source

MAX_CHARS = 220

# --- typography ---------------------------------------------------------------------------
# Models like curly quotes and non-breaking hyphens; plain ASCII renders the same everywhere
# and looks less machine-made.
_TYPOGRAPHY = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u2007": " ",
    "\u2026": "...",
    "\u200b": None, "\u200c": None, "\u200d": None, "\ufeff": None,
})


def normalize(sentence: str) -> str:
    text = " ".join(sentence.translate(_TYPOGRAPHY).split())
    if len(text) > 1 and text[0] == text[-1] == '"':   # the whole sentence wrapped in quotes
        text = text[1:-1].strip()
    return text


# --- word lists ---------------------------------------------------------------------------

_JOB_BOARDS = ("wellfound.com", "angel.co", "naukri.com", "indeed.", "glassdoor.", "instahyre.com",
               "iimjobs.com", "hirist.", "foundit.", "monster.", "shine.com", "cutshort.io",
               "internshala.com", "lever.co", "greenhouse.io", "workable.com", "ashbyhq.com",
               "smartrecruiters.com", "myworkdayjobs.com", "ziprecruiter.", "simplyhired.", "apna.co",
               "timesjobs.com", "freshersworld.com", "hasjob.co", "jobs.")
_JOB_PATH = re.compile(r"/(?:jobs?|careers?|openings?|vacanc\w*|apply|positions?)(?:/|$|\?|-)", re.I)

TECH_TERMS = (
    "python", "java", "javascript", "typescript", "golang", "rust", "c++", "c#", "scala", "kotlin",
    "sql", "nosql", "postgres", "postgresql", "mysql", "mongodb", "redis", "aws", "azure", "gcp",
    "docker", "kubernetes", "k8s", "terraform", "pytorch", "tensorflow", "keras", "scikit-learn",
    "sklearn", "pandas", "numpy", "spark", "pyspark", "hadoop", "kafka", "airflow", "langchain",
    "langgraph", "llamaindex", "rag", "react", "node.js", "nodejs", "django", "flask", "fastapi",
    "mlops", "nlp", "opencv", "hugging face", "huggingface", "fine-tuning", "fine tuning",
    "prompt engineering", "vector database", "vector databases", "git", "linux", "ci/cd",
)

# Claims about me (rule 4): skills, experience, achievements. The template says those things.
_CLAIMS = re.compile(r"""
    \bmy\s+(?:experience|background|skills?|expertise|work|projects?|portfolio|resume|cv|career|
             wheelhouse|knowledge|passion|journey|strengths?)\b
  | \bI(?:'ve|\s+have|\s+had)\s+(?:built|worked|developed|led|shipped|designed|created|deployed|
             delivered|implemented|used|been|experience|years|spent|trained|fine-tuned)\b
  | \bI\s+(?:built|worked|developed|led|shipped|designed|created|deployed|delivered|implemented|
             specialize|specialise|excel)\b
  | \bI(?:'m|\s+am)\s+(?:a|an|skilled|experienced|proficient|certified|passionate|fluent|
             well-versed|adept|confident)\b
  | \b(?:years?\s+of|experience\s+(?:in|with)|expertise\s+in|expert\s+in|proficient|hands-on|
       skilled\s+in|track\s+record)\b
""", re.I | re.X)

# Flattery and stock openers. All three first live sentences began "I'm excited/interested".
_CLICHES = re.compile(r"""
    \bI(?:'m|\s+am)\s+(?:so\s+|really\s+|very\s+|truly\s+)?(?:excited|thrilled|impressed|interested|
             fascinated|inspired|amazed|blown\s+away|eager)\b
  | \b(?:excited|thrilled)\s+(?:by|about|to)\b
  | \bgame[\s-]?changer\b | \bcutting[\s-]edge\b | \bstate[\s-]of[\s-]the[\s-]art\b | \bgroundbreaking\b
  | \brevolutioni[sz]\w*\b | \bworld[\s-]class\b | \bsynerg\w*\b | \bamazing\b | \bincredible\b
  | \bawesome\b | \bpassionate\b | \blove\s+what\b | \bkudos\b
""", re.I | re.X)

_PLACEHOLDER_CHARS = re.compile(r"[{}\[\]<>]")
_PLACEHOLDER_WORDS = re.compile(r"\b(?:X{2,}|TBD|TODO|lorem\s+ipsum|company\s+name|role\s+name|recruiter\s+name|"
                                r"first\s+name|your\s+name|insert\s+\w+)\b", re.I)
_LINK = re.compile(r"https?://|\bwww\.|\b[\w-]+\.(?:com|ai|io|in|org|net|co|dev|app|tech)\b", re.I)
_EMAIL = re.compile(r"\S+@\S+\.\w+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_GREETING = re.compile(r"^(?:hi|hey|hello|dear|greetings)\b", re.I)
_SIGN_OFF = re.compile(r"\b(?:regards|sincerely|best\s+wishes|thanks\s+for\s+your\s+time|thank\s+you\s+for|"
                       r"cheers|won't\s+follow\s+up|let\s+me\s+know|schedule\s+an\s+interview)\b", re.I)

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*(?:\s?%|[A-Za-z]+\b)?")
_SIZE_WORDS = re.compile(r"\b(?:hundreds?|thousands?|millions?|billions?|trillions?|lakhs?|crores?|dozens?)\b", re.I)
_SUPERLATIVES = re.compile(r"\b(?:first|largest|biggest|leading|fastest|fastest-growing|top|best|number\s+one|"
                           r"no\.\s?1|#1|pioneer\w*|only\s+company)\b", re.I)
_ABBREVIATIONS = re.compile(r"\b(?:Inc|Ltd|Co|Corp|Pvt|Dr|Mr|Ms|St|vs|etc|e\.g|i\.e|U\.S|U\.K|Jr|Sr)\.", re.I)

# Capitalized words that are not facts about a company. Checked case-sensitively below.
_GENERIC_CAPS = {"I", "I'm", "I've", "I'd", "I'll", "AI", "GenAI", "ML", "LLM", "LLMs", "API", "APIs", "SaaS",
                 "B2B", "B2C", "CEO", "CTO", "OK"}
_COMPANY_SUFFIXES = {"ai", "inc", "ltd", "llc", "labs", "lab", "technologies", "technology", "tech", "pvt",
                     "private", "limited", "corp", "corporation", "co", "group", "software", "solutions",
                     "systems", "india", "global", "the", "and", "&"}
_LEGAL_SUFFIXES = {"inc", "ltd", "llc", "pvt", "corp", "co", "limited", "private", "plc", "gmbh"}


def is_job_ad(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if any(host == b or host.endswith("." + b) or (b.endswith(".") and (host.startswith(b) or f".{b}" in host))
           for b in _JOB_BOARDS):
        return True
    return bool(_JOB_PATH.search(parsed.path or ""))


def _contains(haystack: str, needle: str) -> bool:
    """Whole-word, case-insensitive. `haystack` must already be lower case."""
    return re.search(r"(?<![a-z0-9])" + re.escape(needle.lower()) + r"(?![a-z0-9])", haystack) is not None


def _company_core(company: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", company.lower())
    core = [w for w in words if w not in _COMPANY_SUFFIXES]
    return core or words


def _names_company(sentence: str, company: str) -> bool:
    core = _company_core(company)
    lower = sentence.lower().replace("'s", "")
    if _contains(lower, " ".join(core)) or _contains(lower.replace(" ", ""), "".join(core)):
        return True
    return len(core[0]) >= 4 and _contains(lower, core[0])


def _requirement_items(requirements: str) -> list[str]:
    items = re.split(r"[,;/|\n\u2022]|\band\b|\bor\b", requirements)
    return [i.strip(" .()-") for i in items if len(i.strip(" .()-")) >= 3]


def _proper_words(sentence: str) -> list[str]:
    """Names and acronyms: capitalized words after the first, and any word with an inner capital."""
    found = []
    for position, word in enumerate(re.findall(r"(?<![\w$])[A-Za-z][A-Za-z0-9'&+.-]*", sentence)):
        word = re.sub(r"'s?$", "", word.rstrip(".'-"))
        for part in filter(None, word.split("-")):
            inner_caps = any(c.isupper() for c in part[1:])
            if (part[0].isupper() and position > 0) or inner_caps:
                found.append(part)
    return found


class Verdict(BaseModel):
    sentence: str = ""            # cleaned; empty when rejected
    problems: list[str] = []
    usable_sources: list[Source] = []

    @property
    def ok(self) -> bool:
        return not self.problems


def check_sentence(raw: str, sources: list[Source], *, company: str, role: str = "",
                   requirements: str = "", my_name: str = "") -> Verdict:
    """All problems with the sentence, not just the first: they go back to W1 as retry feedback."""
    if not raw.strip():
        return Verdict(problems=["W1 wrote no sentence"])
    sentence = normalize(raw)
    lower = sentence.lower()
    problems = []

    # Shape: one short sentence on one line, nothing that belongs to another part of the email.
    if len(sentence) > MAX_CHARS:
        problems.append(f"it is {len(sentence)} characters long; the limit is {MAX_CHARS}")
    if "\n" in raw.strip():
        problems.append("it spans more than one line")
    if not sentence.endswith((".", "!", "?")):
        problems.append("it doesn't end with a full stop")
    if re.search(r"[.!?]\s+[A-Z\"']", _ABBREVIATIONS.sub("", sentence)):
        problems.append("it is more than one sentence")
    if _PLACEHOLDER_CHARS.search(sentence) or _PLACEHOLDER_WORDS.search(sentence):
        problems.append("it contains a placeholder or bracket")
    if _LINK.search(sentence) or _EMAIL.search(sentence) or _PHONE.search(sentence):
        problems.append("it contains a link, email address or phone number")
    if _GREETING.search(sentence) or _SIGN_OFF.search(sentence):
        problems.append("it contains a greeting, sign-off or other text that belongs elsewhere in the email")
    if my_name.strip() and _contains(lower, my_name.strip()):
        problems.append("it mentions my name; the template already introduces me")

    # Content rules.
    if claim := _CLAIMS.search(sentence):
        problems.append(f"it makes a claim about me (\"{claim.group(0).strip()}\"); only the template talks about me")
    if cliche := _CLICHES.search(sentence):
        problems.append(f"it uses flattery or a stock phrase (\"{cliche.group(0).strip()}\"); state a specific fact plainly")
    core = " ".join(_company_core(company))
    tools = [t for t in TECH_TERMS if t != core and _contains(lower, t)]
    tools += [r for r in _requirement_items(requirements)
              if r.lower() not in tools and r.lower() != core and _contains(lower, r)]
    if tools:
        problems.append(f"it names tools or skills ({', '.join(tools)}); the line should be about the company, "
                        "not the job requirements")
    if not _names_company(sentence, company):
        problems.append(f"it doesn't name {company}, so it may be about a different company")

    # Grounding: everything checkable must appear in a cited source that isn't a job ad.
    usable = [s for s in sources if not is_job_ad(s.url)]
    if not sources:
        problems.append("it cites no source")
    elif not usable:
        problems.append("its only sources are job ads (" + ", ".join(urlparse(s.url).hostname or s.url for s in sources)
                        + "); use news or product pages about the company")
    else:
        text = " ".join(f"{s.title} {s.content}" for s in usable).translate(_TYPOGRAPHY).lower()
        squashed = re.sub(r"\s+", "", text)
        known = {w.lower() for w in re.findall(r"[a-z0-9]+", f"{company} {role}".lower())}
        unsupported = []
        for number in _NUMBER.findall(sentence):
            token = re.sub(r"\s+", "", number).lower()
            if not re.search(r"(?<![\d.,])" + re.escape(token), squashed):
                unsupported.append(number)
        for word in _SIZE_WORDS.findall(sentence):
            if not _contains(text, word.lower().rstrip("s")) and not _contains(text, word.lower()):
                unsupported.append(word)
        for word in _SUPERLATIVES.findall(sentence):
            if not _contains(text, word):
                unsupported.append(word)
        for name in _proper_words(sentence):
            if (name in _GENERIC_CAPS or name.lower() in known or name.lower() in _LEGAL_SUFFIXES
                    or any(c.isdigit() for c in name)):
                continue
            if not _contains(text, name) and not (name.endswith("s") and _contains(text, name[:-1])):
                unsupported.append(name)
        if unsupported:
            unique = list(dict.fromkeys(unsupported))
            problems.append(f"{', '.join(repr(u) for u in unique)} {'is' if len(unique) == 1 else 'are'} not in "
                            "the sources it cites")

    if problems:
        return Verdict(problems=problems)
    return Verdict(sentence=sentence, usable_sources=usable)


# --- one retry, then no sentence ----------------------------------------------------------

class Attempt(BaseModel):
    raw: str
    sources: list[Source] = []
    search_ok: bool = False
    problems: list[str] = []
    error: str = ""               # the W1 call itself failed


class PersonalLine(BaseModel):
    sentence: str = ""            # checked and cleaned; "" means the email goes out without one
    sources: list[Source] = []    # the sources that back it
    model: str = ""
    attempts: list[Attempt] = []

    @property
    def fell_back(self) -> bool:
        return not self.sentence


def feedback_for(attempt: Attempt) -> str:
    return (f'Your previous sentence was rejected: "{attempt.raw}". Problems: ' + "; ".join(attempt.problems)
            + ". Write a different sentence that fixes every problem.")


def personalize_checked(personalize: Callable[..., Personalization], contact, *, my_name: str = "") -> PersonalLine:
    """W1, the checks, one retry with the reasons as feedback, then fall back to no sentence.

    `personalize` is N8nClient.personalize (a fake in tests). An N8nError on the first call is
    raised: nothing was produced, and the app shows why. On the retry it only means fallback.
    """
    def field(key: str) -> str:
        return (contact[key] or "") if key in contact.keys() else ""

    request = dict(company=field("company"), role=field("role"), requirements=field("requirements"),
                   job_url=field("job_url"))
    line = PersonalLine()
    for attempt_no in (1, 2):
        feedback = feedback_for(line.attempts[-1]) if line.attempts else ""
        try:
            result = personalize(**request, feedback=feedback) if feedback else personalize(**request)
        except N8nError as exc:
            if attempt_no == 1:
                raise
            line.attempts.append(Attempt(raw="", error=str(exc), problems=[f"the retry failed: {exc}"]))
            break
        verdict = check_sentence(result.sentence, result.sources, company=request["company"], role=request["role"],
                                 requirements=request["requirements"], my_name=my_name)
        line.model = result.model
        line.attempts.append(Attempt(raw=result.sentence, sources=result.sources, search_ok=result.search_ok,
                                     problems=verdict.problems))
        if verdict.ok:
            line.sentence, line.sources = verdict.sentence, verdict.usable_sources
            break
        if not result.sentence.strip():
            break   # W1 found nothing usable about the company; asking again won't change the search
    return line
