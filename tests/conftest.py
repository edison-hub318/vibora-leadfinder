from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

from leadfinder import db, timeutil, users
from leadfinder.config import REPO_ROOT, load_settings

from .fakes import fake_clients

# Fixed "now" for every test: Friday 16 October 2026, 11:00 London (10:00 UTC).
NOW = datetime(2026, 10, 16, 10, 0, 0, tzinfo=timeutil.UTC)
# Test-only password, generated per run so none is ever stored in the repo.
TEST_PASSWORD = "pw-" + __import__("secrets").token_hex(8)

CONFIG_FILES = ["trades.yaml", "chains.txt", "towns.yaml", "free_tier.yaml", "website_rules.yaml"]


@pytest.fixture(autouse=True)
def frozen_time():
    timeutil.set_now(NOW)
    yield
    timeutil.set_now(None)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    for f in CONFIG_FILES:
        shutil.copy(REPO_ROOT / f, tmp_path / f)
    shutil.copytree(REPO_ROOT / "templates", tmp_path / "templates")
    (tmp_path / ".env").write_text(
        "GOOGLE_API_KEY=test-google-key\nMY_NAME=Edison Young\nMY_PHONE=07700 900999\n"
        "BUSINESS_ADDRESS=1 Test Street, Crawley RH10 1AA\nFROM_NAME=Vibora\nFROM_EMAIL=hello@example.org\n"
        "SMTP_HOST=smtp.example.org\nDAILY_SEND_LIMIT=3\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def settings(home: Path):
    return load_settings(home)


@pytest.fixture
def conn(settings):
    c = db.connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def edison(conn):
    return users.add_user(conn, "Edison Young", TEST_PASSWORD)


@pytest.fixture
def jack(conn):
    return users.add_user(conn, "Jack Alcock", TEST_PASSWORD)


@pytest.fixture
def clients():
    return fake_clients()
