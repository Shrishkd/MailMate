"""Uploaded job list (.csv / .xlsx / .docx table) -> checked contacts and a readable report.

Nothing is written here. The report says, row by row, what will be imported and what won't
and why; the app writes the valid rows only after you confirm.
Row numbers match what you see in Excel: the header is row 1, the first job is row 2.
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
    "role": ("role", "job role", "job title", "title", "position", "job", "designation", "opening"),
    "requirements": ("requirements", "requirement", "skills", "required skills", "job requirements",
                     "jd", "job description", "description", "qualifications"),
    "email": ("email", "e mail", "email address", "email id", "mail", "recruiter email",
              "recruiter e mail", "recruiter email id", "contact email", "hr email", "recruiter mail"),
    "name": ("name", "recruiter", "recruiter name", "contact", "contact name", "hr", "hr name",
             "contact person", "hiring manager"),
    "job_url": ("job url", "url", "link", "job link", "posting", "posting url", "job posting",
                "apply link", "application link"),
}
REQUIRED = ("company", "role", "email")
FIELD_LABELS = {"company": "Company", "role": "Role", "requirements": "Requirements",
                "email": "Recruiter email", "name": "Recruiter name", "job_url": "Job URL"}

# Spreadsheet stand-ins for "nothing here".
EMPTY_MARKERS = {"", "-", "--", "n/a", "na", "nil", "none", "null", "nan", "tbd", "?"}
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000


class Contact(BaseModel):
    company: str
    role: str
    requirements: str = ""
    email: str
    name: str = ""
    job_url: str = ""


class RowResult(BaseModel):
    row: int                      # as numbered in the spreadsheet (header = 1)
    raw: dict[str, str]           # the cells we read, by field
    contact: Contact | None = None
    errors: list[str] = []
    warnings: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors


class ImportReport(BaseModel):
    filename: str
    file_errors: list[str] = []
    columns: dict[str, str] = {}      # field -> header as written in the file
    ignored_columns: list[str] = []
    blank_rows: int = 0
    rows: list[RowResult] = []

    @property
    def valid(self) -> list[RowResult]:
        return [r for r in self.rows if r.ok]

    @property
    def rejected(self) -> list[RowResult]:
        return [r for r in self.rows if not r.ok]

    def as_text(self) -> str:
        """Plain-text version of the report (the app shows the same thing as tables)."""
        lines = [f"File: {self.filename}"]
        if self.file_errors:
            lines += [f"  CANNOT READ: {e}" for e in self.file_errors]
            return "\n".join(lines)
        lines.append("Columns: " + ", ".join(f"{FIELD_LABELS[f]} <- '{h}'" for f, h in self.columns.items()))
        if self.ignored_columns:
            lines.append("Ignored columns: " + ", ".join(f"'{c}'" for c in self.ignored_columns))
        lines.append(f"{len(self.rows)} rows read: {len(self.valid)} will be imported, "
                     f"{len(self.rejected)} rejected, {self.blank_rows} blank rows skipped")
        for r in self.rows:
            who = r.raw.get("email") or "(no email)"
            mark = "OK  " if r.ok else "SKIP"
            lines.append(f"  {mark} row {r.row:>3}  {who}  [{r.raw.get('company', '')}]")
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


def _read_xlsx(data: bytes) -> list[list[str]]:
    frame = pd.read_excel(io.BytesIO(data), sheet_name=0, header=None, dtype=str,
                          keep_default_na=False, engine="openpyxl")
    return frame.values.tolist()


def _read_docx(data: bytes) -> list[list[str]]:
    """The first table whose first row looks like a header (names a company and an email column)."""
    tables = Document(io.BytesIO(data)).tables
    for table in tables:
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        if rows and {"company", "email"} <= {_ALIAS_LOOKUP.get(_norm_header(h)) for h in rows[0]}:
            return rows
    return [[cell.text for cell in row.cells] for row in tables[0].rows] if tables else []


def read_rows(filename: str, data: bytes) -> list[list[str]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        return _read_csv(data)
    if suffix == ".xlsx":
        return _read_xlsx(data)
    if suffix == ".docx":
        return _read_docx(data)
    raise ValueError(f"unsupported file type '{suffix}'; use {', '.join(SUPPORTED)}")


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


def check_row(raw: dict[str, str], row: int, check_domain: DomainChecker) -> RowResult:
    result = RowResult(row=row, raw=raw)
    company, role = _one_line(raw.get("company", "")), _one_line(raw.get("role", ""))
    if not company:
        result.errors.append("company is missing")
    if not role:
        result.errors.append("role is missing")
    for field, value in (("company", company), ("role", role)):
        if len(value) > 150:
            result.errors.append(f"{field} is {len(value)} characters long; is this the right column?")

    address = parse_address(raw.get("email", ""))
    if address.error:
        result.errors.append(address.error)
    else:
        domain = check_domain(address.email.split("@")[1])
        if domain.status == "no_mail":
            result.errors.append(f"can't receive email: {domain.reason}")
        elif domain.status == "unknown":
            result.warnings.append(f"{domain.reason}; it will be checked again before sending")

    requirements = raw.get("requirements", "").strip()
    if not requirements:
        result.warnings.append("no requirements; the personal sentence will be more generic")
    job_url = raw.get("job_url", "").strip()
    if job_url and not re.match(r"https?://\S+$", job_url):
        result.warnings.append(f"job URL '{job_url[:60]}' doesn't look like a web link")

    if result.ok:
        result.contact = Contact(company=company, role=role, requirements=requirements,
                                 email=address.email, job_url=job_url,
                                 name=_one_line(raw.get("name", "")) or address.display_name)
    return result


def analyse(filename: str, data: bytes, conn: sqlite3.Connection, check_domain: DomainChecker) -> ImportReport:
    report = ImportReport(filename=filename)
    if len(data) > MAX_FILE_BYTES:
        report.file_errors.append(f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        return report
    try:
        table = read_rows(filename, data)
    except ValueError as exc:
        report.file_errors.append(str(exc))
        return report
    except Exception as exc:  # corrupt file, wrong extension, password-protected workbook...
        report.file_errors.append(f"couldn't open the file ({type(exc).__name__}); is it really a "
                                  f"{Path(filename).suffix} file?")
        return report

    table = [[_clean(c) for c in r] for r in table]
    lines_above_header = 0
    while table and not any(table[0]):  # tolerate blank lines above the header
        table.pop(0)
        lines_above_header += 1
    if not table:
        report.file_errors.append("the file is empty")
        return report

    header, body = table[0], table[1:]
    columns, report.ignored_columns, errors = map_columns(header)
    report.columns = {f: header[i] for f, i in columns.items()}
    if errors:
        report.file_errors += errors
        return report
    if len(body) > MAX_ROWS:
        report.file_errors.append(f"{len(body)} rows; split the file into parts of at most {MAX_ROWS}")
        return report

    existing, blocked = db.known_contacts(conn), db.suppressed(conn)
    first_seen: dict[str, int] = {}
    for offset, cells in enumerate(body):
        row = offset + 2 + lines_above_header
        if not any(cells):
            report.blank_rows += 1
            continue
        raw = {f: (cells[i] if i < len(cells) else "") for f, i in columns.items()}
        result = check_row(raw, row, check_domain)
        if result.contact:
            email = result.contact.email
            if email in blocked:
                result.errors.append(f"on the do-not-contact list ({blocked[email].replace('_', ' ')})")
            elif email in existing:
                known = existing[email]
                result.errors.append(f"already a contact (added {known['created_at'][:10]} for "
                                     f"{known['company']}); MailMate never emails an address twice")
            elif email in first_seen:
                result.errors.append(f"duplicate of row {first_seen[email]}")
            else:
                first_seen[email] = row
            if result.errors:
                result.contact = None
        report.rows.append(result)
    if not report.rows:
        report.file_errors.append("the file has a header but no jobs")
    return report
