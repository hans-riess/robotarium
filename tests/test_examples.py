"""
The shipped examples, through the CLI exactly as the README's walkthrough types it -- against
the local stand-in for the site, so it runs everywhere. tests/test_live.py does the same
against the real Robotarium, when asked to.
"""

import pytest

from robotarium import cli, ledger, scaffold
from robotarium.experiment import load_project

from conftest import HAS_SIMULATOR

needs_simulator = pytest.mark.skipif(not HAS_SIMULATOR, reason="rps is not installed")


def test_every_example_is_listed_and_checks_clean(tmp_path, capsys):
    assert "two_robots" in scaffold.examples()
    for name in scaffold.examples():
        directory = tmp_path / name
        assert cli.main(["init", str(directory), "--example", name]) == 0
        assert cli.main(["check", str(directory)]) == 0
        assert "0 error(s), 0 warning(s)." in capsys.readouterr().out


def test_init_example_writes_the_files_and_keeps_yours(tmp_path, capsys):
    assert cli.main(["init", str(tmp_path / "try"), "--example", "two_robots"]) == 0
    names = sorted(path.name for path in (tmp_path / "try").iterdir())
    assert names == [".env.example", ".gitignore", "main.py", "robotarium.toml"]
    project = load_project(tmp_path / "try")
    assert project.experiment.robots == 2 and project.draft is True
    assert "\n" not in project.experiment.description

    (tmp_path / "try" / "main.py").write_text("mine\n")
    cli.main(["init", str(tmp_path / "try"), "--example", "two_robots"])
    assert (tmp_path / "try" / "main.py").read_text() == "mine\n"


def test_an_unknown_example_is_refused(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(["init", str(tmp_path / "x"), "--example", "nope"])


@needs_simulator
def test_the_walkthrough_end_to_end(tmp_path, fake_site, logged_in, capsys, monkeypatch):
    directory = tmp_path / "try_robotarium"
    assert cli.main(["init", str(directory), "--example", "two_robots"]) == 0
    monkeypatch.chdir(directory)

    assert cli.main(["check"]) == 0
    assert cli.main(["simulate"]) == 0
    out = capsys.readouterr().out
    assert "both robots arrived" in out and "0 error(s), 0 warning(s)." in out

    assert cli.main(["submit", "--dry-run"]) == 0
    assert fake_site == []

    assert cli.main(["submit", "--yes"]) == 0
    uploads = [each["filename"] for each in fake_site if each["path"] == "/api/files/upload"]
    assert uploads == ["main.py", "robotarium_runtime.py", "robotarium_bundle.py"]
    create = next(each["json"] for each in fake_site if each["path"].endswith("create"))
    assert create["numRobots"] == "2" and create["status"] == "draft"
    assert create["title"].startswith("Two robots swap places (")
    assert create["estimatedDuration"] == "600"

    assert cli.main(["history"]) == 0
    assert "experiment exp-1 saved as a draft" in capsys.readouterr().out
    assert ledger.read(directory / ".robotarium")[-1]["outcome"] == "submitted"
