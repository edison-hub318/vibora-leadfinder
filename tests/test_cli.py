from __future__ import annotations

import pytest
from typer.testing import CliRunner

from leadfinder import cli

from .conftest import TEST_PASSWORD
from .fakes import FakePlaces, crawley_plumbers, fake_clients

runner = CliRunner()


@pytest.fixture
def app_env(home, monkeypatch):
    places = FakePlaces(pages={"plumbers in Crawley, UK": [crawley_plumbers()]})
    clients = fake_clients(places=places)
    monkeypatch.setattr(cli, "_settings_home", home)
    monkeypatch.setattr(cli, "_clients_factory", lambda s: clients)
    from rich.console import Console

    monkeypatch.setattr(cli, "console", Console(width=200, safe_box=True, emoji=False))
    monkeypatch.setattr(cli, "err", Console(width=200, safe_box=True, emoji=False))
    cli.reset_ctx()
    yield home
    cli.reset_ctx()


def invoke(*args, input=None):
    cli.reset_ctx()
    res = runner.invoke(cli.app, list(args), input=input)
    return res


def test_full_cli_flow(app_env):
    pw = f"{TEST_PASSWORD}\n{TEST_PASSWORD}\n"
    r = invoke("search", "plumbers", "--town", "Crawley")
    assert r.exit_code == 1 and "user add" in r.output

    r = invoke("user", "add", "Edison Young", input=pw)
    assert r.exit_code == 0, r.output
    r = invoke("search", "plumbers", "--town", "Crawley")
    assert r.exit_code == 1 and "login" in r.output  # write commands need a login

    r = invoke("login", "edison  young", input=f"{TEST_PASSWORD}\n")
    assert r.exit_code == 0 and "Edison Young" in r.output
    assert "Edison Young" in invoke("whoami").output

    r = invoke("search", "plumbers", "--town", "Crawley", "--max", "5", "--no-audit")
    assert r.exit_code == 0, r.output
    assert "qualified" in r.output and "Top 10 new leads" in r.output and "Estimated cost" in r.output

    r = invoke("list", "--top", "5")
    assert r.exit_code == 0 and "Smith Plumbing" in r.output

    r = invoke("next")
    assert r.exit_code == 0 and "TPS not checked" in r.output and "Opener" in r.output

    r = invoke("log", "1", "--outcome", "callback", "--callback", "2026-10-20", "--note", "Ring after 2",
               "--duration", "1:20", "--objection", "bad_timing")
    assert r.exit_code == 0, r.output
    assert "Callback" in r.output

    r = invoke("show", "1")
    assert r.exit_code == 0 and "Ring after 2" in r.output and "EY" in r.output

    r = invoke("tps", "1", "--clear")
    assert r.exit_code == 0

    r = invoke("stats")
    assert r.exit_code == 0 and "Calls today: 1" in r.output

    r = invoke("export", str(app_env / "out.csv"))
    assert r.exit_code == 0 and (app_env / "out.csv").exists()

    r = invoke("log", "2", "--outcome", "meeting_booked", "--meeting", "2026-10-21T10:00")
    assert r.exit_code == 0 and "calendar.google.com" in r.output

    r = invoke("stage", "2", "won", "--setup", "900", "--retainer", "120")
    assert r.exit_code == 0 and "won" in r.output

    r = invoke("dnc", "add", "01293 888 999", "--reason", "Asked")
    assert r.exit_code == 0
    assert "+441293888999" in invoke("dnc", "list").output

    r = invoke("backup")
    assert r.exit_code == 0 and "Backup saved" in r.output


def test_bad_outcome_and_objection(app_env):
    invoke("user", "add", "Jack Alcock", input=f"{TEST_PASSWORD}\n{TEST_PASSWORD}\n")
    invoke("login", "Jack Alcock", input=f"{TEST_PASSWORD}\n")
    invoke("search", "plumbers", "--town", "Crawley", "--max", "2", "--no-audit")
    assert invoke("log", "1", "--outcome", "nope").exit_code == 1
    assert invoke("log", "1", "--outcome", "not_interested", "--objection", "made_up").exit_code == 1


def test_short_password_rejected(app_env):
    r = invoke("user", "add", "Edison Young", input="short\nshort\n" + f"{TEST_PASSWORD}\n{TEST_PASSWORD}\n")
    assert "Too short" in r.output and r.exit_code == 0


def test_wrong_password_message(app_env):
    invoke("user", "add", "Edison Young", input=f"{TEST_PASSWORD}\n{TEST_PASSWORD}\n")
    r = invoke("login", "Edison Young", input="wrong-password\n")
    assert r.exit_code == 1 and "Name or password incorrect" in r.output
    r = invoke("login", "Nobody Here", input="wrong-password\n")
    assert "Name or password incorrect" in r.output


def test_ui_refuses_without_users(app_env):
    r = invoke("ui", "--no-browser")
    assert r.exit_code == 1 and "user add" in r.output and "Edison Young" in r.output
