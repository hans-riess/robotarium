"""The command line, as a person types it."""

import json
import os

import pytest

from robotarium import cli

from conftest import HAS_SIMULATOR, PASSWORD, USERNAME, write


def test_init_writes_a_project_that_checks_clean(tmp_path, capsys):
    assert cli.main(["init", str(tmp_path / "exp"), "--robots", "6"]) == 0
    assert cli.main(["check", str(tmp_path / "exp")]) == 0
    out = capsys.readouterr().out
    assert "0 error(s), 0 warning(s)." in out
    assert "Number of Robots (1-18)      6" in out


def test_init_with_julia_declares_its_package(tmp_path, capsys):
    assert cli.main(["init", str(tmp_path / "exp"), "--julia"]) == 0
    assert cli.main(["check", str(tmp_path / "exp")]) == 0
    assert "backend.jl" in capsys.readouterr().out


def test_init_never_overwrites_without_force(tmp_path, capsys):
    write(tmp_path / "exp" / "main.py", "mine\n")
    cli.main(["init", str(tmp_path / "exp")])
    assert (tmp_path / "exp" / "main.py").read_text() == "mine\n"
    assert "kept" in capsys.readouterr().out


def test_check_fails_on_an_error(tmp_path, capsys):
    cli.main(["init", str(tmp_path / "exp")])
    manifest = tmp_path / "exp" / "robotarium.toml"
    manifest.write_text(manifest.read_text().replace("robots = 4", "robots = 19"))
    assert cli.main(["check", str(tmp_path / "exp")]) == 1
    assert "the testbed has 1-18" in capsys.readouterr().err


def test_submit_from_flags_alone(tmp_path, fake_site, logged_in, capsys):
    write(tmp_path / "go.py", "import rps.robotarium as robotarium\n"
                              "r = robotarium.Robotarium(number_of_robots=2)\nr.debug()\n")
    write(tmp_path / "waypoints.npy", "")
    code = cli.main(["submit", "--title", "Flags", "--description", "Two robots.",
                     "--duration", "90s", "--robots", "2", "--main", str(tmp_path / "go.py"),
                     "--file", str(tmp_path / "waypoints.npy"), "--yes"])
    assert code == 0
    uploads = [each["filename"] for each in fake_site if each["path"] == "/api/files/upload"]
    assert uploads == ["go.py", "waypoints.npy"]
    create = next(each["json"] for each in fake_site if each["path"].endswith("create"))
    assert (create["title"], create["estimatedDuration"], create["numRobots"]) == \
        ("Flags", "90", "2")
    out = capsys.readouterr().out
    assert USERNAME not in out and PASSWORD not in out


def test_flags_override_the_manifest(tmp_path, fake_site, logged_in):
    cli.main(["init", str(tmp_path / "exp")])
    code = cli.main(["submit", str(tmp_path / "exp"), "--title", "Override", "--duration", "60",
                     "--no-simulate", "--queue", "--yes"])
    assert code == 0
    create = next(each["json"] for each in fake_site if each["path"].endswith("create"))
    assert (create["title"], create["status"]) == ("Override", "submitted")


def test_history_lists_what_was_sent(tmp_path, fake_site, logged_in, capsys):
    cli.main(["init", str(tmp_path / "exp")])
    cli.main(["submit", str(tmp_path / "exp"), "--duration", "60", "--no-simulate", "--yes"])
    capsys.readouterr()
    assert cli.main(["history", str(tmp_path / "exp")]) == 0
    assert "experiment exp-1 saved as a draft" in capsys.readouterr().out
    assert cli.main(["history", str(tmp_path / "exp"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["outcome"] == "submitted"


def test_credentials_says_where_without_saying_what(tmp_path, capsys):
    env = write(tmp_path / ".env", "ROBOTARIUM_USERNAME=%s\nROBOTARIUM_PASSWORD=%s\n"
                % (USERNAME, PASSWORD))
    os.chmod(env, 0o600)
    assert cli.main(["credentials", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "s***@e***.edu" in out and ".env" in out
    assert USERNAME not in out and PASSWORD not in out


def test_missing_credentials_fail_with_directions(tmp_path, fake_site, capsys,
                                                  monkeypatch):
    monkeypatch.setattr("sys.stdin", None)
    cli.main(["init", str(tmp_path / "exp")])
    code = cli.main(["submit", str(tmp_path / "exp"), "--duration", "60", "--no-simulate",
                     "--yes"])
    assert code == 1
    assert "robotarium login" in capsys.readouterr().err
    assert fake_site == []


@pytest.mark.skipif(not HAS_SIMULATOR, reason="rps is not installed")
def test_simulate_then_inspect_the_run_log(project_dir, capsys):
    assert cli.main(["simulate", str(project_dir)]) == 0
    run = next((project_dir / ".robotarium" / "runs").glob("*.npz"))
    capsys.readouterr()
    assert cli.main(["inspect", str(run)]) == 0
    out = capsys.readouterr().out
    assert "poses                150x3x4" in out and "finished 150 steps" in out



def test_a_file_glob_on_the_command_line_is_read_from_here(tmp_path, fake_site, logged_in,
                                                         monkeypatch):
    here = tmp_path / "work[1]"
    write(here / "go.py", "import rps.robotarium as robotarium\n"
                          "r = robotarium.Robotarium(number_of_robots=2)\nr.debug()\n")
    write(here / "data" / "a.npy", "")
    write(here / "data" / "b.npy", "")
    monkeypatch.chdir(here)
    code = cli.main(["submit", "--title", "G", "--description", "D", "--duration", "60",
                     "--robots", "2", "--main", "go.py", "--file", "data/*.npy", "--yes"])
    assert code == 0
    uploads = [each["filename"] for each in fake_site if each["path"] == "/api/files/upload"]
    assert uploads == ["go.py", "a.npy", "b.npy"]



def test_flags_alone_default_to_the_maximum_duration(tmp_path, fake_site, logged_in):
    write(tmp_path / "go.py", "import rps.robotarium as robotarium\n"
                              "r = robotarium.Robotarium(number_of_robots=2)\nr.debug()\n")
    code = cli.main(["submit", "--title", "T", "--description", "D", "--robots", "2",
                     "--main", str(tmp_path / "go.py"), "--yes"])
    assert code == 0
    create = next(each["json"] for each in fake_site if each["path"].endswith("create"))
    assert create["estimatedDuration"] == "600"



def test_login_refuses_a_password_that_is_not_text_without_showing_it(tmp_path, capsys,
                                                                      monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "someone@example.edu")
    monkeypatch.setattr("getpass.getpass", lambda prompt: "zqj\udcffxvk")
    assert cli.main(["login", "--store", "file", "--verify"]) == 1
    err = capsys.readouterr().err
    assert "not valid text" in err and "zqj" not in err and "xvk" not in err
