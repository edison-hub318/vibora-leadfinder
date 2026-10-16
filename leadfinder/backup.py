"""Daily backups with SQLite's online backup API (safe while the database is in use).

Retention: the newest backup of each of the last 30 days that have one, plus the newest
backup of each of the last 12 months. Everything else is deleted.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db
from .config import Settings
from .timeutil import LONDON, UTC, now_london, now_utc, today_london

NAME_RE = re.compile(r"^leads-(\d{8})-(\d{6})(?:-([a-z0-9\-]+))?\.db$")
KEEP_DAILY = 30
KEEP_MONTHLY = 12


@dataclass
class BackupFile:
    path: Path
    taken_at: datetime  # UTC
    size: int
    label: str = ""

    @property
    def name(self) -> str:
        return self.path.name


def _parse(path: Path) -> BackupFile | None:
    m = NAME_RE.match(path.name)
    if not m:
        return None
    local = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=LONDON)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return BackupFile(path=path, taken_at=local.astimezone(UTC), size=size, label=m.group(3) or "")


def list_backups(settings: Settings) -> list[BackupFile]:
    d = settings.backup_dir
    if not d.exists():
        return []
    out = [b for b in (_parse(p) for p in d.iterdir() if p.is_file()) if b]
    return sorted(out, key=lambda b: b.taken_at, reverse=True)


def last_backup(settings: Settings) -> BackupFile | None:
    items = list_backups(settings)
    return items[0] if items else None


def backup_age_hours(settings: Settings) -> float | None:
    b = last_backup(settings)
    if b is None:
        return None
    return (now_utc() - b.taken_at).total_seconds() / 3600.0


def make_backup(conn: sqlite3.Connection, settings: Settings, label: str = "") -> BackupFile:
    d = settings.backup_dir
    d.mkdir(parents=True, exist_ok=True)
    stamp = now_london().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{label}" if label else ""
    target = d / f"leads-{stamp}{suffix}.db"
    n = 2
    while target.exists():  # two backups in the same second
        target = d / f"leads-{stamp}{suffix}-{n}.db"
        n += 1
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    db.set_setting(conn, "last_backup_date", today_london().isoformat())
    prune(settings)
    return _parse(target) or BackupFile(target, now_utc(), target.stat().st_size, label)


def prune(settings: Settings) -> list[Path]:
    """Apply retention. Returns the deleted files."""
    items = list_backups(settings)  # newest first
    keep: set[Path] = set()
    days: list[str] = []
    months: list[str] = []
    for b in items:
        local = b.taken_at.astimezone(LONDON)
        day = local.strftime("%Y-%m-%d")
        month = local.strftime("%Y-%m")
        if day not in days and len(days) < KEEP_DAILY:
            days.append(day)
            keep.add(b.path)
        if month not in months and len(months) < KEEP_MONTHLY:
            months.append(month)
            keep.add(b.path)
    deleted = []
    for b in items:
        if b.path not in keep:
            try:
                b.path.unlink()
                deleted.append(b.path)
            except OSError:
                pass
    return deleted


def ensure_daily_backup(conn: sqlite3.Connection, settings: Settings) -> BackupFile | None:
    """Back up once per day, the first time anything runs that day."""
    if db.get_setting(conn, "last_backup_date") == today_london().isoformat():
        return None
    try:
        return make_backup(conn, settings)
    except (OSError, sqlite3.Error):
        return None  # never block the app because the backup folder is unavailable


def restore(conn: sqlite3.Connection, settings: Settings, backup_name: str, confirm: str) -> BackupFile:
    """Restore a backup into the live database. Requires confirm == 'RESTORE'.
    Takes a safety backup of the current database first."""
    if confirm != "RESTORE":
        raise ValueError("Type RESTORE to confirm.")
    src_path = settings.backup_dir / Path(backup_name).name  # never allow paths outside the folder
    if not src_path.exists() or not NAME_RE.match(src_path.name):
        raise FileNotFoundError(f"No backup called {backup_name}.")
    safety = make_backup(conn, settings, label="pre-restore")
    src = sqlite3.connect(str(src_path))
    try:
        if conn.in_transaction:
            conn.commit()
        src.backup(conn)
    finally:
        src.close()
    db.set_setting(conn, "last_backup_date", today_london().isoformat())
    return safety


def backup_info(settings: Settings) -> dict[str, Any]:
    b = last_backup(settings)
    age = backup_age_hours(settings)
    return {
        "last": b,
        "age_hours": age,
        "stale": age is None or age > 48,
        "dir": str(settings.backup_dir),
        "default_dir": settings.backup_dir_is_default,
        "items": list_backups(settings),
    }
