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
from pathlib import Path

import pandas as pd
from docx import Document
from pydantic import BaseModel

from mailmate import db
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
    "job_url": ("job url", "url", "link", "job link", "posting", "posting url", "job posting",
                "apply link", "application link"),
    "email_status": ("hunter status", "email status", "verification", "verification status",
                     "email verification", "verified"),
    "status": ("status", "outreach status", "contact status"),
    "date_sent": ("date sent", "sent on", "sent date", "emailed on", "date emailed"),
}
REQUIRED = ("company", "email")
FIELD_LABELS = {"company": "Company", "role": "Role applying for", "title": "Their title",
                "requirements": "Requirements", "email": "Recruiter email", "name": "Recruiter name",
                "first_name": "First name", "last_name": "Last name", "job_url": "Job URL",
                "email_status": "Email status", "status": "Status", "date_sent": "Date sent"}

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
              "not interested", "interviewing", "interview", "rejected", "closed", "done", "applied"}
_BAD_ADDRESS = {"invalid", "disposable", "undeliverable", "bounced", "blocked"}
_RISKY_ADDRESS = {"accept all", "accept_all", "acceptall", "catch all", "catch_all", "unverified", "unknown",
                  "risky"}
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


class ImportReport(BaseModel):
    filename: str
    file_errors: list[str] = []
    sheets: list[SheetReport] = []
    rows: list[RowResult] = []

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


def _read_xlsx(data: bytes) -> list[tuple[str, list[list[str]]]]:
    frames = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str,
                           keep_default_na=False, engine="openpyxl")
    return [(name, frame.values.tolist()) for name, frame in frames.items()]


def _read_docx(data: bytes) -> list[list[str]]:
    """The first table that has a header naming a company and an email column."""
    tables = [[[cell.text for cell in row.cells] for row in t.rows] for t in Document(io.BytesIO(data)).tables]
    for rows in tables:
        if _find_header(rows) is not None:
            return rows
    return tables[0] if tables else []


def read_tables(filename: str, data: bytes) -> list[tuple[str, list[list[str]]]]:
    """[(sheet name, rows)]. CSV and Word files have one table with no sheet name."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        tables = [("", _read_csv(data))]
    elif suffix == ".xlsx":
        tables = _read_xlsx(data)
    elif suffix == ".docx":
        tables = [("", _read_docx(data))]
    else:
        raise ValueError(f"unsupported file type '{suffix}'; use {', '.join(SUPPORTED)}")
    return [(name, [[_clean(c) for c in r] for r in rows]) for name, rows in tables]


def _find_header(table: list[list[str]]) -> int | None:
    """Index of the first row (within the first few) that has a company and an email column."""
    for i, cells in enumerate(table[:HEADER_SEARCH_ROWS]):
        fields = {_ALIAS_LOOKUP.get(_norm_header(_clean(c))) for c in cells}
        if set(REQUIRED) <= fields:
            return i
    return None


def job_sheets(filename: str, data: bytes) -> list[str]:
    """Workbook sheets that look like contact lists (have Company + Email headers). [] for CSV/Word."""
    try:
        return [name for name, rows in read_tables(filename, data) if name and _find_header(rows) is not None]
    except Exception:
        return []


# --- checking -----------------------------------------------------------------------------

def map_columns(header: list[str]) -> tuple[dict[str, int], list[str], list[str]]:
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
    for field in REQUIRED:
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

    verification = _norm_header(raw.get("email_status", "")).replace(" ", "_")
    if verification.replace("_", " ") in _BAD_ADDRESS:
        result.errors.append(f"the email checker says this address is {raw['email_status']}")
    elif verification in _RISKY_ADDRESS or verification.replace("_", " ") in _RISKY_ADDRESS:
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
                                 title=_one_line(raw.get("title", "")))
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

    tables = [(name, rows) for name, rows in tables if any(any(r) for r in rows)]
    if not tables:
        report.file_errors.append("the file is empty")
        return report
    if sheets is not None:
        tables = [(name, rows) for name, rows in tables if name in sheets]
        if not tables:
            report.file_errors.append("no sheet selected")
            return report
    elif len(tables) > 1:
        usable = [(name, rows) for name, rows in tables if _find_header(rows) is not None]
        if not usable:
            report.file_errors.append("no sheet has both a Company and an Email column in its first "
                                      f"{HEADER_SEARCH_ROWS} rows")
            return report
        tables = usable

    # Pass 1: find each sheet's header and columns.
    parsed = []
    for name, table in tables:
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
        report.sheets.append(sheet)
        parsed.append((sheet, columns, header_index, body))
    if report.file_errors:
        return report

    # Pass 2: check rows. Addresses whose Status says "already contacted" are refused in every sheet.
    existing, blocked = db.known_contacts(conn), db.suppressed(conn)
    first_seen: dict[str, str] = {}
    results = []
    for sheet, columns, header_index, body in parsed:
        for offset, cells in enumerate(body):
            row = header_index + offset + 2
            raw = {f: (cells[i] if i < len(cells) else "") for f, i in columns.items()}
            if not raw.get("company") and not raw.get("email"):
                sheet.blank_rows += 1      # blank, or a note below the table
                continue
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
