"""Accounts, password hashing, login lockout and the CLI's "who am I" file."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, URLSafeSerializer

from . import db
from .config import Settings
from .timeutil import from_db, now_db, now_utc, to_db

MIN_PASSWORD = 8
MAX_FAILURES = 5
LOCKOUT_MINUTES = 15

_hasher = PasswordHasher()


class AuthError(Exception):
    pass


@dataclass
class User:
    id: int
    full_name: str
    initials: str
    daily_call_target: int = 40

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0] if self.full_name else ""


def name_key(name: str) -> str:
    """Full-name matching ignores case and extra spaces."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def tidy_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def initials_for(name: str) -> str:
    parts = [p for p in tidy_name(name).split(" ") if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _row_user(r: sqlite3.Row | None) -> User | None:
    if r is None:
        return None
    return User(id=r["id"], full_name=r["full_name"], initials=r["initials"], daily_call_target=r["daily_call_target"])


def hash_password(pw: str) -> str:
    if len(pw or "") < MIN_PASSWORD:
        raise AuthError(f"Password must be at least {MIN_PASSWORD} characters.")
    return _hasher.hash(pw)


def add_user(conn: sqlite3.Connection, full_name: str, password: str, daily_call_target: int = 40) -> User:
    name = tidy_name(full_name)
    if not name:
        raise AuthError("Full name is required.")
    if get_user_by_name(conn, name):
        raise AuthError(f"An account for '{name}' already exists.")
    h = hash_password(password)
    cur = conn.execute(
        "INSERT INTO users(full_name, name_key, initials, password_hash, created_at, daily_call_target) VALUES (?,?,?,?,?,?)",
        (name, name_key(name), initials_for(name), h, now_db(), daily_call_target),
    )
    db.commit(conn)
    return get_user(conn, cur.lastrowid)  # type: ignore[return-value]


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user_id))
    db.commit(conn)


def change_password(conn: sqlite3.Connection, user_id: int, current: str, new: str) -> None:
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None or not _verify(row["password_hash"], current):
        raise AuthError("Current password is incorrect.")
    set_password(conn, user_id, new)


def get_user(conn: sqlite3.Connection, user_id: int | None) -> User | None:
    if user_id is None:
        return None
    return _row_user(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def get_user_by_name(conn: sqlite3.Connection, name: str) -> User | None:
    return _row_user(conn.execute("SELECT * FROM users WHERE name_key=?", (name_key(name),)).fetchone())


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, full_name, initials, created_at, last_login, daily_call_target FROM users ORDER BY id"
    ).fetchall()]


def user_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def _verify(h: str, pw: str) -> bool:
    try:
        return _hasher.verify(h, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def is_locked(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT locked_until FROM login_attempts WHERE name_key=?", (name_key(name),)).fetchone()
    if row is None or not row["locked_until"]:
        return False
    until = from_db(row["locked_until"])
    return bool(until and until > now_utc())


def authenticate(conn: sqlite3.Connection, name: str, password: str) -> User:
    """Check a name + password. Raises AuthError('Name or password incorrect') on any failure
    (never says which part was wrong). Locks the name for 15 minutes after 5 failures."""
    key = name_key(name)
    generic = AuthError("Name or password incorrect")
    if is_locked(conn, key):
        raise AuthError("Too many failed attempts. Try again in 15 minutes.")
    row = conn.execute("SELECT * FROM users WHERE name_key=?", (key,)).fetchone()
    ok = False
    if row is not None:
        ok = _verify(row["password_hash"], password or "")
    else:
        # Spend similar time as a real check so names can't be discovered by timing.
        _verify(_DUMMY_HASH, password or "")
    if not ok:
        _record_failure(conn, key)
        raise generic
    conn.execute("DELETE FROM login_attempts WHERE name_key=?", (key,))
    conn.execute("UPDATE users SET last_login=? WHERE id=?", (now_db(), row["id"]))
    if _hasher.check_needs_rehash(row["password_hash"]):
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (_hasher.hash(password), row["id"]))
    db.commit(conn)
    return _row_user(row)  # type: ignore[return-value]


def _record_failure(conn: sqlite3.Connection, key: str) -> None:
    row = conn.execute("SELECT failures, locked_until FROM login_attempts WHERE name_key=?", (key,)).fetchone()
    failures = 1
    if row is not None:
        # A finished lockout starts a fresh count.
        lu = from_db(row["locked_until"]) if row["locked_until"] else None
        failures = 1 if (lu and lu <= now_utc()) else row["failures"] + 1
    locked_until = to_db(now_utc() + timedelta(minutes=LOCKOUT_MINUTES)) if failures >= MAX_FAILURES else None
    conn.execute(
        "INSERT INTO login_attempts(name_key, failures, locked_until, last_failure) VALUES (?,?,?,?) "
        "ON CONFLICT(name_key) DO UPDATE SET failures=excluded.failures, locked_until=excluded.locked_until, "
        "last_failure=excluded.last_failure",
        (key, failures, locked_until, now_db()),
    )
    db.commit(conn)


_DUMMY_HASH = _hasher.hash("not-a-real-password-just-for-timing")


# ---------------------------------------------------------------------------
# CLI login state
# ---------------------------------------------------------------------------

def _cli_serializer(settings: Settings) -> URLSafeSerializer:
    return URLSafeSerializer(settings.secret_key, salt="leadfinder-cli")


def cli_login(settings: Settings, user: User) -> None:
    token = _cli_serializer(settings).dumps({"uid": user.id})
    settings.cli_state_path.write_text(
        json.dumps({"user": user.full_name, "token": token}, indent=2), encoding="utf-8"
    )


def cli_logout(settings: Settings) -> None:
    if settings.cli_state_path.exists():
        settings.cli_state_path.unlink()


def cli_current_user(conn: sqlite3.Connection, settings: Settings) -> User | None:
    p = settings.cli_state_path
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        uid = _cli_serializer(settings).loads(data["token"])["uid"]
    except (BadSignature, KeyError, ValueError, TypeError):
        return None
    return get_user(conn, uid)


def log_activity(
    conn: sqlite3.Connection,
    user_id: int | None,
    kind: str,
    summary: str,
    lead_id: int | None = None,
    tag: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO activity(user_id, lead_id, kind, summary, tag, created_at) VALUES (?,?,?,?,?,?)",
        (user_id, lead_id, kind, summary, tag, now_db()),
    )
    db.commit(conn)
