"""The command line, as a person types it."""

import json
import os

import pytest

from robotarium import cli, ledger
from robotarium.bundle import build
from robotarium.experiment import load_project

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



def test_output_cut_short_by_a_pipe_is_not_a_traceback(tmp_path):
    import subprocess
    import sys
    cli.main(["init", str(tmp_path / "exp"), "--example", "two_robots"])
    shell = subprocess.run("'%s' -m robotarium check '%s' | head -1"
                           % (sys.executable, tmp_path / "exp"), shell=True,
                           capture_output=True, text=True, timeout=120)
    assert "Traceback" not in shell.stderr and "BrokenPipeError" not in shell.stderr
    assert shell.stdout.strip() == "0 error(s), 0 warning(s)."


# ---------------------------------------------------------------------------
# --set, recording options, the budget
# ---------------------------------------------------------------------------

def variant_project(tmp_path):
    cli.main(["init", str(tmp_path / "exp"), "--example", "two_robots"])
    manifest = tmp_path / "exp" / "robotarium.toml"
    text = manifest.read_text().replace('title = "Two robots swap places ({timestamp})"',
                                        'title = "Swap {config.controller}"')
    manifest.write_text(text.replace("max_iterations = 1500",
                                     'max_iterations = 1500\ncontroller = "sheaf"'))
    return tmp_path / "exp"


def bundled_config(project):
    namespace = {}
    exec((project / ".robotarium" / "bundle" / "robotarium_bundle.py").read_text(), namespace)
    return namespace["CONFIG"], namespace["EXPERIMENT"]["title"]


@pytest.mark.parametrize("command", ["check", "bundle"])
def test_set_makes_the_bundle_the_variants(tmp_path, capsys, command):
    project = variant_project(tmp_path)
    assert cli.main([command, str(project), "--set", "controller=baseline",
                     "--set", "max_iterations=10"]) == 0
    assert bundled_config(project) == ({"max_iterations": 10, "controller": "baseline"},
                                       "Swap baseline")
    assert "Title                        Swap baseline" in capsys.readouterr().out


def test_a_bad_setting_is_one_line(tmp_path, capsys):
    project = variant_project(tmp_path)
    assert cli.main(["check", str(project), "--set", "controller"]) == 1
    assert "robotarium: --set takes KEY=VALUE" in capsys.readouterr().err


def test_submit_sends_the_variant_and_records_it(tmp_path, fake_site, logged_in):
    project = variant_project(tmp_path)
    code = cli.main(["submit", str(project), "--set", "controller=baseline", "--no-simulate",
                     "--yes"])
    assert code == 0
    sent = next(each["content"] for each in fake_site
                if each.get("filename") == "robotarium_bundle.py").decode()
    assert '"controller": "baseline"' in sent
    create = next(each["json"] for each in fake_site if each["path"].endswith("create"))
    assert create["title"] == "Swap baseline"
    entry = ledger.read(project / ".robotarium")[-1]
    assert entry["set"] == {"controller": "baseline"}


def test_the_recording_options_become_a_recording(tmp_path):
    project_dir = variant_project(tmp_path)
    project = load_project(project_dir)
    bundle = build(project.experiment, project.output_dir)
    arguments = cli.build_parser().parse_args(
        ["simulate", str(project_dir), "--video", "--frames", "0,14.5,end", "--every", "2",
         "--dpi", "120", "--seed", "3"])
    recording = cli.recording_for(project, bundle, arguments)
    assert recording.video.parent == project.output_dir / "runs"
    assert recording.video.name.startswith("swap_sheaf_") and recording.video.suffix == ".mp4"
    assert recording.frames_dir == recording.video.with_name(recording.video.stem + "_frames")
    assert (recording.frames, recording.every, recording.fps, recording.dpi) == \
        ([0.0, 14.5, "end"], 2, 15, 120)

    arguments = cli.build_parser().parse_args(
        ["simulate", str(project_dir), "--video", str(tmp_path / "out.mp4"), "--frames", "end",
         "--frames-dir", str(tmp_path / "stills"), "--frames-format", "pdf", "--fps", "60"])
    recording = cli.recording_for(project, bundle, arguments)
    assert recording.video == (tmp_path / "out.mp4").resolve() and recording.fps == 60
    assert recording.frames_dir == (tmp_path / "stills").resolve()
    assert recording.frames_format == "pdf"

    arguments = cli.build_parser().parse_args(["simulate", str(project_dir)])
    assert cli.recording_for(project, bundle, arguments) is None


def test_the_manifest_can_ask_for_a_recording(tmp_path):
    project_dir = variant_project(tmp_path)
    manifest = project_dir / "robotarium.toml"
    manifest.write_text(manifest.read_text().replace(
        "headless = true", 'headless = true\nframes = [0, "end"]\ndpi = 90\nseed = 11'))
    project = load_project(project_dir)
    bundle = build(project.experiment, project.output_dir)
    arguments = cli.build_parser().parse_args(["simulate", str(project_dir), "--dpi", "100"])
    recording = cli.recording_for(project, bundle, arguments)
    assert recording.video is None and recording.frames == [0.0, "end"]
    assert recording.dpi == 100 and recording.frames_dir.name.endswith("_frames")
    assert project.simulator_seed == 11


def test_a_video_without_ffmpeg_fails_before_running(tmp_path, capsys, monkeypatch):
    project = variant_project(tmp_path)
    monkeypatch.setattr("robotarium.simulate.shutil.which", lambda name: None)
    assert cli.main(["simulate", str(project), "--video"]) == 1
    captured = capsys.readouterr()
    assert "needs ffmpeg" in captured.err and "Running" not in captured.out


def test_the_cold_start_options_need_cold(tmp_path, capsys):
    cli.main(["init", str(tmp_path / "exp"), "--julia"])
    assert cli.main(["julia", "check", str(tmp_path / "exp"), "--keep-julia"]) == 1
    assert "go with --cold" in capsys.readouterr().err


def test_check_shows_the_budget_from_the_last_simulation(tmp_path, capsys):
    from robotarium.simulate import SimulationReport, save_last
    cli.main(["init", str(tmp_path / "exp"), "--julia"])
    manifest = tmp_path / "exp" / "robotarium.toml"
    manifest.write_text(manifest.read_text().replace('duration = "auto"', "duration = 120"))
    project = load_project(tmp_path / "exp")
    bundle = build(project.experiment, project.output_dir)
    save_last(SimulationReport(returncode=0, rps="/rps", robots=[4], init_steps=300,
                               steps=1500, ended=True), bundle, project.output_dir,
              project.experiment.load_config())
    capsys.readouterr()
    assert cli.main(["check", str(tmp_path / "exp")]) == 1
    out = capsys.readouterr().out
    assert "Budget: 180 s Julia startup (180 s assumed) + 59 s robots (1800 steps) = 239 s, " \
           "against 120 s declared." in out
    assert "assumed to take 180 s" in out and "Declare 300" in out
    # A variant is another run: the last simulation says nothing about it.
    assert cli.main(["check", str(tmp_path / "exp"), "--set", "radius=0.3"]) == 0
    assert "robots' steps: `robotarium simulate` counts them" in capsys.readouterr().out
