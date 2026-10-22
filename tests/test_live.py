"""
The two-robot example, submitted to the REAL Robotarium as a draft: the one test of what the
local stand-in cannot show, that the site still answers the way client.py expects.

Skipped unless asked for, since it needs an account and leaves a draft on it:

    ROBOTARIUM_LIVE_TEST=1 ROBOTARIUM_USERNAME=you@example.edu ROBOTARIUM_PASSWORD=... \\
        pytest -m live

(or keep the login in the keyring or a .env, as docs/credentials.md describes, and set only
ROBOTARIUM_LIVE_TEST). The draft is saved, not queued: nothing runs on the physical robots.
Delete it from https://www.robotarium.gatech.edu/dashboard afterwards.
"""

import json
import os

import pytest

from robotarium import cli, ledger

from conftest import HAS_SIMULATOR

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("ROBOTARIUM_LIVE_TEST") != "1",
                       reason="submits to the real Robotarium; set ROBOTARIUM_LIVE_TEST=1"),
]


@pytest.fixture(autouse=True)
def real_login(monkeypatch, tmp_path):
    """
    The real login -- the environment, the keyring, a .env, the per-user file -- which the
    suite's isolation fixture (conftest.py) hides from every other test. Undoing its patches
    puts all of it back for these tests only.
    """
    monkeypatch.undo()
    monkeypatch.chdir(tmp_path)


def test_the_login_works(capsys):
    assert cli.main(["credentials", "--verify"]) == 0
    assert "Logged in" in capsys.readouterr().out


def test_the_example_is_saved_as_a_draft(tmp_path, capsys):
    directory = tmp_path / "live_two_robots"
    assert cli.main(["init", str(directory), "--example", "two_robots"]) == 0
    arguments = ["submit", str(directory), "--draft", "--yes"]
    if not HAS_SIMULATOR:
        arguments += ["--no-simulate", "--duration", "60"]
    assert cli.main(arguments) == 0, capsys.readouterr().err
    entry = ledger.read(directory / ".robotarium")[-1]
    assert entry["outcome"] == "submitted" and entry["experiment_id"]
    print("Saved draft %s; delete it from the dashboard." % entry["experiment_id"])

    # Read it back as `fetch` reads results: the record, and the uploaded files by their ids.
    capsys.readouterr()
    assert cli.main(["fetch", "--project", str(directory), "--scripts"]) == 0, \
        capsys.readouterr().err
    results = directory / ".robotarium" / "results" / entry["experiment_id"]
    assert json.loads((results / "experiment.json").read_text())["status"] == "draft"
    assert (results / "scripts" / "main.py").read_bytes() == \
        (directory / ".robotarium" / "bundle" / "main.py").read_bytes()
