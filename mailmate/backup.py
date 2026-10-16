"""Copies of the database, so a bad import, a wrong click or a bug can be undone.

One copy a day, made the first time the app opens that day (India date), kept for 14 days; plus copies
made by hand, which are never deleted automatically. They live in a `backups` folder next to the
database (data/backups/, git-ignored like the rest of data/). SQLite's backup API makes a consistent copy
even while the app is using the database.
"""

import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

KEEP_DAILY = 14
_DAILY = re.compile(r"^mailmate-\d{4}-\d{2}-\d{2}\.db$")


def folder_for(db_path: Path) -> Path:
    return Path(db_path).parent / "backups"


def _copy(conn: sqlite3.Connection, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")     # never leave a half-written file under the real name
    dest = sqlite3.connect(partial)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    partial.replace(target)
    return target


def daily_backup(conn: sqlite3.Connection, folder: Path, today: date, keep: int = KEEP_DAILY) -> Path | None:
    """Today's copy, if there isn't one yet (then None). Daily copies older than the newest `keep` go."""
    target = folder / f"mailmate-{today:%Y-%m-%d}.db"
    if target.exists():
        return None
    _copy(conn, target)
    daily = sorted(p for p in folder.iterdir() if _DAILY.match(p.name))
    for old in daily[:-keep]:
        old.unlink()
    return target


def backup_now(conn: sqlite3.Connection, folder: Path, now: datetime) -> Path:
    """A copy made by hand (kept until you delete it)."""
    return _copy(conn, folder / f"mailmate-{now:%Y-%m-%d-%H%M%S}-manual.db")


def backups(folder: Path) -> list[Path]:
    """Every copy, newest first."""
    if not folder.exists():
        return []
    return sorted((p for p in folder.glob("mailmate-*.db")), key=lambda p: p.stat().st_mtime, reverse=True)
