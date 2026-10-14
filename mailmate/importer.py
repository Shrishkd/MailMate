"""Uploaded job list (.csv / .xlsx / .docx table) -> checked contacts and a readable report.

Nothing is written here. The report says, row by row, what will be imported and what won't
and why; the app writes the valid rows only after you confirm.
Row numbers match what you see in Excel. The header doesn't have to be on row 1: title rows
above it are skipped, and in a workbook every sheet with Company + Email columns can be used.

Two kinds of "role" are kept apart: {role} in the template is the job I'm applying for; a
recruiter's own title ("Senior Technical Recruiter") is only shown for context. A role column
that mostly holds recruiter/HR titles is treated as their title, and the role I typed at
upload is used instead.
"""

import csv
import io
import re
import sqlite3
import warnings
from pathlib import Path
from typing import NamedTuple

import openpyxl
from docx import Document
from pydantic import BaseModel

from mailmate import db
from mailmate.companies import company_key
from mailmate.emailcheck import DomainChecker, parse_address

SUPPORTED = (".csv", ".xlsx", ".docx")

# Header text (normalized) -> field. Matching is exact after normalizing, so "Recruiter" is the
# recruiter's name but "Recruiter Email" is the address.
ALIASES = {
    "company": ("company", "company name", "organization", "organisation", "employer", "firm"),
    "role": ("target role", "role applying for", "applying for", "position applying for", "job role",
             "job title", "role", "position", "job", "opening"),
    "title": ("title", "role title", "recruiter title", "contact title", "designation", "their title"),
    "requirements": ("requirements", "requirement", "skills", "required skills", "job requirements",
                     "jd", "job description", "description", "qualifications"),
    "email": ("email", "e mail", "email address", "email id", "mail", "recruiter email",
              "recruiter e mail", "recruiter email id", "contact email", "hr email", "recruiter mail"),
    "name": ("name", "recruiter", "recruiter name", "contact", "contact name", "hr", "hr name",
             "contact person", "hiring manager", "full name"),
    "first_name": ("first name", "firstname", "given name"),
    "last_name": ("last name", "lastname", "surname", "family name"),
    "job_url": ("job url", "url", "link", "job link", "posting", "posting url", "job posting", "direct link",
                "direct apply link", "direct job link", "apply url", "apply here", "application url",
                "job apply link", "careers link",
                "apply link", "application link"),
    "email_status": ("hunter status", "email status", "verification", "verification status",
                     "email verification", "verified", "email basis", "how the email was found",
                     "how email was found"),
    "status": ("status", "outreach status", "contact status"),
    "date_sent": ("date sent", "sent on", "sent date", "emailed on", "date emailed"),
    "priority": ("priority", "importance", "fit", "job fit", "match"),
    "applied": ("applied", "applied via link", "applied via direct link", "applied direct link", "applied directly",
                "applied on site", "applied online", "direct apply", "direct application", "applied status", "applied on"),
    "group": ("group", "segment"),
}
REQUIRED = ("company", "email")
FIELD_LABELS = {"company": "Company", "role": "Role applying for", "title": "Their title",
                "requirements": "Requirements", "email": "Recruiter email", "name": "Recruiter name",
                "first_name": "First name", "last_name": "Last name", "job_url": "Job URL",
                "email_status": "Email status", "status": "Status", "date_sent": "Date sent",
                "priority": "Priority", "applied": "Applied (direct link)", "group": "Group"}

# Spreadsheet stand-ins for "nothing here".
EMPTY_MARKERS = {"", "-", "--", "—", "–", "n/a", "na", "nil", "none", "null", "nan", "tbd", "?"}
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
HEADER_SEARCH_ROWS = 15

# A "role" value that is really the contact's own job, not the job I'm applying for.
_HR_TITLE = re.compile(r"\b(?:recruit\w*|talent|hr|human\s+resources?|hiring|people|acquisition|staffing|"
                       r"sourc\w*|general\s+contact|careers?|contact|inbox)\b", re.I)
# Status values meaning "I already emailed this person outside MailMate" (rule 5).
_CONTACTED = {"sent", "contacted", "emailed", "mailed", "replied", "followed up", "follow up sent", "bounced",
              "not interested", "interviewing", "interview", "rejected", "closed", "done"}
# "Applied" means I applied through the job link, not that I emailed this recruiter.
_APPLIED_STATUS = re.compile(r"^applied\b", re.I)
_YES = {"yes", "y", "true", "1", "x", "done", "applied", "\u2713", "\u2714", "\u2705", "ok"}
# The email checker's wording varies ("accept_all", "Hunter pattern-generated, not verified"): match words.
_BAD_ADDRESS = re.compile(r"\b(?:invalid|disposable|undeliverable|bounced|blocked)\b", re.I)
_RISKY_ADDRESS = re.compile(r"unverified|not\s+verified|accept[\s_-]?all|catch[\s_-]?all|cannot\s+confirm|"
                            r"can't\s+confirm|\brisky\b|\bunknown\b", re.I)
_SHEET_APPLIED = re.compile(r"(?<!not )\bapplied\b", re.I)    # "Applied-20 Contacts", not "Not applied"
_EXAMPLE_DOMAINS = {"example.com", "example.org", "example.net"}
_EXAMPLE_TLDS = (".example", ".test", ".invalid", ".localhost")


class Contact(BaseModel):
    company: str
    role: str
    requirements: str = ""
    email: str
    name: str = ""
    title: str = ""           # the recruiter's own job title, for context only
    job_url: str = ""
    priority: str = ""        # 'high' | 'medium' | 'low' | '' : who goes first in suggestions
    applied: bool = False     # I applied to this company through the job's own link


class RowResult(BaseModel):
    row: int                      # as numbered in the spreadsheet
    sheet: str = ""               # "" for CSV and Word files
    raw: dict[str, str]           # the cells we read, by field
    contact: Contact | None = None
    errors: list[str] = []
    warnings: list[str] = []
    contacted_elsewhere: bool = False   # its Status says I already emailed them

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def where(self) -> str:
        return f"'{self.sheet}' row {self.row}" if self.sheet else f"row {self.row}"


class SheetReport(BaseModel):
    name: str = ""                    # "" for CSV and Word files
    header_row: int = 1
    columns: dict[str, str] = {}      # field -> header as written in the file
    ignored_columns: list[str] = []
    notes: list[str] = []             # decisions the reader should know about
    blank_rows: int = 0               # blank rows and rows with neither company nor email (notes)


class JobLink(BaseModel):
    """One opening from a sheet of job links (a company and where to apply, no recruiter email)."""
    company: str
    role: str = ""
    url: str
    applied: bool = False
    priority: str = ""        # from a Priority or Fit column: 'high' | 'medium' | 'low' | ''


class ImportReport(BaseModel):
    filename: str
    file_errors: list[str] = []
    sheets: list[SheetReport] = []
    rows: list[RowResult] = []
    job_links: list[JobLink] = []

    @property
    def columns(self) -> dict[str, str]:
        return {f: h for s in self.sheets for f, h in s.columns.items()}

    @property
    def ignored_columns(self) -> list[str]:
        return list(dict.fromkeys(c for s in self.sheets for c in s.ignored_columns))

    @property
    def blank_rows(self) -> int:
        return sum(s.blank_rows for s in self.sheets)

    @property
    def valid(self) -> list[RowResult]:
        return [r for r in self.rows if r.ok]

    @property
    def rejected(self) -> list[RowResult]:
        return [r for r in self.rows if not r.ok]

    @property
    def applied_companies(self) -> list[str]:
        """Companies this file marks as applied by direct link (any row, imported or not)."""
        names: dict[str, str] = {}
        for r in self.rows:
            company = _one_line(r.raw.get("company", ""))
            if company and _row_applied(r.raw):
                names.setdefault(company_key(company), company)
        return sorted(names.values(), key=str.lower)

    @property
    def known_updates(self) -> list[dict]:
        """Rows of people who are already contacts: the job link and priority this file has for them
        (MailMate only ever fills empty fields with these, see db.fill_contact_blanks)."""
        out = []
        for r in self.rows:
            if not r.raw.get("_address") or not any(e.startswith("already a contact") for e in r.errors):
                continue
            link = r.raw.get("job_url", "").strip()
            link = link if re.match(r"https?://\S+$", link) else ""
            priority = normalize_priority(r.raw.get("priority", ""))
            if link or priority:
                out.append({"email": r.raw["_address"], "job_url": link, "priority": priority})
        return out

    @property
    def contacted_elsewhere(self) -> list[str]:
        """Valid addresses already emailed outside MailMate: they go on the do-not-contact list."""
        return sorted({r.raw["_address"] for r in self.rows if r.contacted_elsewhere and r.raw.get("_address")})

    def as_text(self) -> str:
        """Plain-text version of the report (the app shows the same thing as tables)."""
        lines = [f"File: {self.filename}"]
        if self.file_errors:
            lines += [f"  CANNOT READ: {e}" for e in self.file_errors]
            return "\n".join(lines)
        for s in self.sheets:
            label = f"Sheet '{s.name}', header on row {s.header_row}" if s.name else f"Header on row {s.header_row}"
            lines.append(label + ": " + ", ".join(f"{FIELD_LABELS[f]} <- '{h}'" for f, h in s.columns.items()))
            if s.ignored_columns:
                lines.append("  Ignored columns: " + ", ".join(f"'{c}'" for c in s.ignored_columns))
            lines += [f"  Note: {n}" for n in s.notes]
        lines.append(f"{len(self.rows)} rows read: {len(self.valid)} will be imported, "
                     f"{len(self.rejected)} rejected, {self.blank_rows} blank or note rows skipped")
        for r in self.rows:
            who = r.raw.get("email") or "(no email)"
            mark = "OK  " if r.ok else "SKIP"
            lines.append(f"  {mark} {r.where:>12}  {who}  [{r.raw.get('company', '')}]")
            lines += [f"         error:   {e}" for e in r.errors]
            lines += [f"         warning: {w}" for w in r.warnings]
        return "\n".join(lines)


def _row_applied(raw: dict[str, str]) -> bool:
    """Applied by direct link: an Applied column (Yes / tick / date), a Status or Group that says Applied."""
    return (is_applied(raw.get("applied", "")) or bool(_APPLIED_STATUS.match(raw.get("status", "")))
            or is_applied(raw.get("group", "")))


def _sheet_says_applied(name: str) -> bool:
    return bool(name) and bool(_SHEET_APPLIED.search(name))


def is_applied(value: str) -> bool:
    """'Yes', a tick, 'Applied' or a date (when I applied) -> True; 'No' or empty -> False."""
    text = _clean(value).lower()
    if not text or text in {"no", "n", "false", "0", "not yet", "pending"}:
        return False
    return text in _YES or text.startswith("applied") or bool(re.match(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", text))


def normalize_priority(value: str) -> str:
    """'High', 'P1', '1 - urgent' -> 'high'; anything unrecognised -> '' (sorted after 'low')."""
    text = _norm_header(value)
    if not text:
        return ""
    if text.startswith(("high", "urgent", "top")) or text in ("p1", "1", "a"):
        return "high"
    if text.startswith(("med", "normal")) or text in ("p2", "2", "b"):
        return "medium"
    if text.startswith("low") or text in ("p3", "3", "c"):
        return "low"
    return ""


def _norm_header(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split())


_ALIAS_LOOKUP = {alias: field for field, aliases in ALIASES.items() for alias in aliases}


def _clean(value) -> str:
    text = "" if value is None else str(value)
    text = text.replace(" ", " ").replace("​", "").replace("﻿", "").strip()
    return "" if text.lower() in EMPTY_MARKERS else text


def _one_line(text: str) -> str:
    return " ".join(text.split())


# --- reading ------------------------------------------------------------------------------

def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):  # Excel on Windows saves CSV as cp1252
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _read_csv(data: bytes) -> list[list[str]]:
    text = _decode(data)
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    try:
        dialect = csv.Sniffer().sniff(first_line, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    return [list(r) for r in csv.reader(io.StringIO(text), delimiter=delimiter)]


class Table(NamedTuple):
    name: str                                   # "" for CSV and Word files
    rows: list[list[str]]
    links: dict[tuple[int, int], str]           # (row, column) -> the web address behind a hyperlinked cell


def _read_xlsx(data: bytes) -> list[Table]:
    """Cell text, plus the address behind hyperlinked cells: a cell that says 'Apply here' keeps its link."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")         # openpyxl warns about Excel features it skips (data validation)
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    tables = []
    for sheet in workbook.worksheets:
        rows, links = [], {}
        for r, cells in enumerate(sheet.iter_rows()):
            row = []
            for c, cell in enumerate(cells):
                row.append("" if cell.value is None else str(cell.value))
                target = getattr(cell.hyperlink, "target", None) if cell.hyperlink else None
                if target and target.lower().startswith(("http://", "https://")):
                    links[(r, c)] = target
            rows.append(row)
        tables.append(Table(sheet.title, rows, links))
    return tables


def _read_docx(data: bytes) -> list[list[str]]:
    """The first table that has a header naming a company and an email column."""
    tables = [[[cell.text for cell in row.cells] for row in t.rows] for t in Document(io.BytesIO(data)).tables]
    for rows in tables:
        if _find_header(rows) is not None:
            return rows
    return tables[0] if tables else []


def read_tables(filename: str, data: bytes) -> list[Table]:
    """One Table per sheet. CSV and Word files have one table with no sheet name."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        tables = [Table("", _read_csv(data), {})]
    elif suffix == ".xlsx":
        tables = _read_xlsx(data)
    elif suffix == ".docx":
        tables = [Table("", _read_docx(data), {})]
    else:
        raise ValueError(f"unsupported file type '{suffix}'; use {', '.join(SUPPORTED)}")
    return [Table(t.name, [[_clean(c) for c in r] for r in t.rows], t.links) for t in tables]


LINK_REQUIRED = ("company", "job_url")


def _find_header(table: list[list[str]], required: tuple[str, ...] = REQUIRED) -> int | None:
    """Index of the first row (within the first few) that has the required columns (company + email)."""
    for i, cells in enumerate(table[:HEADER_SEARCH_ROWS]):
        fields = {_ALIAS_LOOKUP.get(_norm_header(_clean(c))) for c in cells}
        if set(required) <= fields:
            return i
    return None


def job_sheets(filename: str, data: bytes) -> list[str]:
    """Workbook sheets that look like contact lists (have Company + Email headers). [] for CSV/Word."""
    try:
        return [t.name for t in read_tables(filename, data) if t.name and _find_header(t.rows) is not None]
    except Exception:
        return []


def _link_url(text: str, link: str | None) -> str:
    """A cell that says 'Apply here' is worth its hyperlink; a cell that is itself an address stays."""
    text = text.strip()
    return link if link and not re.match(r"https?://", text, re.I) else text


def _read_link_sheet(table: Table) -> tuple[SheetReport, list[JobLink]] | None:
    """A sheet of openings: Company + a job link and no email column (those sheets hold contacts)."""
    header_index = _find_header(table.rows, LINK_REQUIRED)
    if header_index is None or _find_header(table.rows) is not None:
        return None
    columns, ignored, _ = map_columns(table.rows[header_index], LINK_REQUIRED)
    sheet = SheetReport(name=table.name, header_row=header_index + 1, ignored_columns=ignored,
                        columns={f: table.rows[header_index][i] for f, i in columns.items()})
    links = []
    for offset, cells in enumerate(table.rows[header_index + 1:]):
        raw = {f: (cells[i] if i < len(cells) else "") for f, i in columns.items()}
        url = _link_url(raw.get("job_url", ""), table.links.get((header_index + 1 + offset, columns["job_url"])))
        company = _one_line(raw.get("company", ""))
        if not company or not re.match(r"https?://\S+$", url):
            sheet.blank_rows += 1
            continue
        links.append(JobLink(company=company, role=_one_line(raw.get("role", "")), url=url, applied=_row_applied(raw),
                             priority=normalize_priority(raw.get("priority", ""))))
    sheet.notes.append(f"{len(links)} job link(s) found ({sum(link.applied for link in links)} marked applied). "
                       "They're attached to the contacts at the same company; nobody is imported from this sheet")
    return sheet, links


def pick_link(links: list[JobLink], role: str) -> JobLink | None:
    """The opening this contact's role names; otherwise the company's first link."""
    if not links:
        return None
    wanted = _norm_header(role)
    for link in links:
        opening = _norm_header(link.role)
        if opening and wanted and (opening in wanted or wanted in opening):
            return link
    return links[0]


# --- checking -----------------------------------------------------------------------------

def map_columns(header: list[str], required: tuple[str, ...] = REQUIRED) -> tuple[dict[str, int], list[str], list[str]]:
    """Header row -> ({field: column index}, ignored headers, errors)."""
    columns: dict[str, int] = {}
    ignored, errors = [], []
    for i, raw in enumerate(header):
        text = _clean(raw)
        field = _ALIAS_LOOKUP.get(_norm_header(text))
        if field is None:
            if text:
                ignored.append(text)
        elif field in columns:
            errors.append(f"two columns mean '{FIELD_LABELS[field]}': '{_clean(header[columns[field]])}' "
                          f"and '{text}'; rename or delete one")
        else:
            columns[field] = i
    for field in required:
        if field not in columns:
            names = ", ".join(f"'{a}'" for a in ALIASES[field][:4])
            errors.append(f"no '{FIELD_LABELS[field]}' column found (accepted headers include {names})")
    return columns, ignored, errors


def _role_column_is_their_title(values: list[str]) -> bool:
    filled = [v for v in values if v]
    return bool(filled) and sum(bool(_HR_TITLE.search(v)) for v in filled) * 2 > len(filled)


def _is_example(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1]
    return domain in _EXAMPLE_DOMAINS or domain.endswith(_EXAMPLE_TLDS) or any(
        domain.endswith("." + d) for d in _EXAMPLE_DOMAINS)


def check_row(raw: dict[str, str], row: int, check_domain: DomainChecker, default_role: str = "",
              sheet: str = "") -> RowResult:
    result = RowResult(row=row, sheet=sheet, raw=raw)
    company = _one_line(raw.get("company", ""))
    role = _one_line(raw.get("role", "")) or _one_line(default_role)
    if not company:
        result.errors.append("company is missing")
    if not role:
        result.errors.append("role is missing: type the role you're applying for above the upload, "
                             "or add a 'Target Role' column")
    for field, value in (("company", company), ("role", role)):
        if len(value) > 150:
            result.errors.append(f"{field} is {len(value)} characters long; is this the right column?")

    address = parse_address(raw.get("email", ""))
    if address.error:
        result.errors.append(address.error)
    elif _is_example(address.email):
        result.errors.append("example address (a sample row?); not a real recruiter")
    else:
        result.raw["_address"] = address.email
        domain = check_domain(address.email.split("@")[1])
        if domain.status == "no_mail":
            result.errors.append(f"can't receive email: {domain.reason}")
        elif domain.status == "unknown":
            result.warnings.append(f"{domain.reason}; it will be checked again before sending")

    status = _norm_header(raw.get("status", ""))
    if status in _CONTACTED or raw.get("date_sent", ""):
        said = f"Status '{raw['status']}'" if status in _CONTACTED else f"Date sent {raw['date_sent'][:10]}"
        result.errors.append(f"already contacted outside MailMate ({said}); MailMate never emails an address twice")
        result.contacted_elsewhere = bool(result.raw.get("_address"))

    verification = raw.get("email_status", "")
    if _BAD_ADDRESS.search(verification):
        result.errors.append(f"the email checker says this address is {raw['email_status']}")
    elif _RISKY_ADDRESS.search(verification):
        result.warnings.append(f"may bounce: the mail server can't confirm this mailbox exists "
                               f"(email status '{raw['email_status']}')")

    job_url = raw.get("job_url", "").strip()
    if job_url and not re.match(r"https?://\S+$", job_url):
        result.warnings.append(f"job URL '{job_url[:60]}' doesn't look like a web link")

    if result.ok:
        name = (_one_line(raw.get("name", ""))
                or _one_line(f"{raw.get('first_name', '')} {raw.get('last_name', '')}")
                or address.display_name)
        result.contact = Contact(company=company, role=role, requirements=raw.get("requirements", "").strip(),
                                 email=address.email, job_url=job_url, name=name,
                                 title=_one_line(raw.get("title", "")), priority=normalize_priority(raw.get("priority", "")),
                                 applied=_row_applied(raw))
    return result


def analyse(filename: str, data: bytes, conn: sqlite3.Connection, check_domain: DomainChecker, *,
            sheets: list[str] | None = None, default_role: str = "") -> ImportReport:
    """`sheets`: which workbook sheets to read (None: every sheet that looks like a contact list).
    `default_role`: the job I'm applying for, used where the file has no target-role value."""
    report = ImportReport(filename=filename)
    if len(data) > MAX_FILE_BYTES:
        report.file_errors.append(f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        return report
    try:
        tables = read_tables(filename, data)
    except ValueError as exc:
        report.file_errors.append(str(exc))
        return report
    except Exception as exc:  # corrupt file, wrong extension, password-protected workbook...
        report.file_errors.append(f"couldn't open the file ({type(exc).__name__}); is it really a "
                                  f"{Path(filename).suffix} file?")
        return report

    tables = [t for t in tables if any(any(r) for r in t.rows)]
    if not tables:
        report.file_errors.append("the file is empty")
        return report
    link_sheets = [found for found in (_read_link_sheet(t) for t in tables) if found]   # always read: they only add links
    report.job_links = [link for _, found in link_sheets for link in found]
    if sheets is not None:
        tables = [t for t in tables if t.name in sheets]
        if not tables:
            report.file_errors.append("no sheet selected")
            return report
    elif len(tables) > 1:
        usable = [t for t in tables if _find_header(t.rows) is not None]
        if not usable:
            report.file_errors.append("no sheet has both a Company and an Email column in its first "
                                      f"{HEADER_SEARCH_ROWS} rows")
            return report
        tables = usable

    # Pass 1: find each sheet's header and columns.
    parsed = []
    for name, table, links in tables:
        header_index = _find_header(table)
        if header_index is None:
            header_index = next(i for i, r in enumerate(table) if any(r))
        sheet = SheetReport(name=name, header_row=header_index + 1)
        columns, sheet.ignored_columns, errors = map_columns(table[header_index])
        prefix = f"sheet '{name}': " if name else ""
        report.file_errors += [prefix + e for e in errors]
        body = table[header_index + 1:]
        if len(body) > MAX_ROWS:
            report.file_errors.append(f"{prefix}{len(body)} rows; split the file into parts of at most {MAX_ROWS}")
        if "role" in columns and _role_column_is_their_title([r[columns["role"]] for r in body
                                                              if columns["role"] < len(r)]):
            role_header = table[header_index][columns["role"]]
            if "title" not in columns:
                columns["title"] = columns["role"]
            del columns["role"]
            sheet.notes.append(f"'{role_header}' mostly holds recruiter/HR titles, so it's kept as their title; "
                               "the role you're applying for comes from the box above the upload")
        sheet.columns = {f: table[header_index][i] for f, i in columns.items()}
        sheet_applied = _sheet_says_applied(name)
        if sheet_applied:
            sheet.notes.append(f"the sheet is named '{name}', so every company in it is marked as applied by "
                               "direct link (untick any that aren't on the Contacts page)")
        report.sheets.append(sheet)
        parsed.append((sheet, columns, header_index, body, links, sheet_applied))
    report.sheets += [sheet for sheet, _ in link_sheets]
    if report.file_errors:
        return report

    # Pass 2: check rows. Addresses whose Status says "already contacted" are refused in every sheet.
    existing, blocked = db.known_contacts(conn), db.suppressed(conn)
    first_seen: dict[str, str] = {}
    results = []
    links_by_company: dict[str, list[JobLink]] = {}
    for link in report.job_links:
        links_by_company.setdefault(company_key(link.company), []).append(link)
    applied_by_link = {company_key(link.company) for link in report.job_links if link.applied}
    for sheet, columns, header_index, body, links, sheet_applied in parsed:
        for offset, cells in enumerate(body):
            row = header_index + offset + 2
            raw = {f: (cells[i] if i < len(cells) else "") for f, i in columns.items()}
            if not raw.get("company") and not raw.get("email"):
                sheet.blank_rows += 1      # blank, or a note below the table
                continue
            if "job_url" in columns:       # 'Apply here' -> the address behind it
                raw["job_url"] = _link_url(raw["job_url"], links.get((header_index + 1 + offset, columns["job_url"])))
            key = company_key(raw.get("company", ""))
            company_links = links_by_company.get(key, [])
            match = pick_link(company_links, raw.get("role") or default_role)
            if match and not raw.get("job_url"):         # a sheet of openings knows where to apply
                raw["job_url"] = match.url
            if company_links and not normalize_priority(raw.get("priority", "")):
                # its Fit: the matching opening's, else the company's best opening's
                raw["priority"] = match.priority or min(
                    (link.priority for link in company_links if link.priority),
                    key=lambda p: ("high", "medium", "low").index(p), default="")
            if (sheet_applied or key in applied_by_link) and not _row_applied(raw):
                raw["applied"] = "Yes"
            results.append(check_row(raw, row, check_domain, default_role, sheet.name))
    contacted = {r.raw["_address"] for r in results if r.contacted_elsewhere}

    for result in results:
        email = result.raw.get("_address", "")
        if email:
            if email in blocked:
                result.errors.append(f"on the do-not-contact list ({blocked[email].replace('_', ' ')})")
            elif email in existing:
                known = existing[email]
                result.errors.append(f"already a contact (added {known['created_at'][:10]} for "
                                     f"{known['company']}); MailMate never emails an address twice")
            elif email in contacted and not result.contacted_elsewhere:
                result.errors.append("already contacted outside MailMate (another row's Status says so)")
            elif email in first_seen and not result.contacted_elsewhere:
                result.errors.append(f"duplicate of {first_seen[email]}")
            elif not result.contacted_elsewhere:
                first_seen[email] = result.where
            if result.errors:
                result.contact = None
        report.rows.append(result)
    if not report.rows:
        report.file_errors.append("the file has a header but no jobs")
    return report
