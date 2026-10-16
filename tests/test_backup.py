"""Database copies: one a day, the last 14 kept, copies made by hand never pruned, every copy readable."""

import sqlite3
from datetime import date, datetime, timedelta

from mailmate import backup, db


def test_one_copy_a_day_and_it_is_a_full_database(conn, tmp_path):
    db.set_settings(conn, my_name="Asha Rao")
    first = backup.daily_backup(conn, tmp_path, date(2026, 10, 16))
    assert first.name == "mailmate-2026-10-16.db"
    assert backup.daily_backup(conn, tmp_path, date(2026, 10, 16)) is None          # same day: nothing new
    copy = sqlite3.connect(first)
    assert copy.execute("SELECT value FROM settings WHERE key = 'my_name'").fetchone() == ("Asha Rao",)
    copy.close()
    assert not list(tmp_path.glob("*.partial"))


def test_only_the_last_14_daily_copies_are_kept_but_manual_ones_stay(conn, tmp_path):
    manual = backup.backup_now(conn, tmp_path, datetime(2026, 9, 1, 10, 30, 5))
    start = date(2026, 9, 1)
    for n in range(20):
        backup.daily_backup(conn, tmp_path, start + timedelta(days=n))
    daily = sorted(p.name for p in tmp_path.glob("mailmate-????-??-??.db"))
    assert len(daily) == 14
    assert daily[0] == "mailmate-2026-09-07.db" and daily[-1] == "mailmate-2026-09-20.db"
    assert manual.exists() and manual.name == "mailmate-2026-09-01-103005-manual.db"
    assert len(backup.backups(tmp_path)) == 15


def test_the_folder_sits_next_to_the_database(tmp_path):
    assert backup.folder_for(tmp_path / "data" / "mailmate.db") == tmp_path / "data" / "backups"
    assert backup.backups(tmp_path / "nothing-here") == []
