"""Rehearsing in the Robotarium simulator: what the report says and what it is judged by."""

import base64
import collections
import hashlib
import io
import os
import sys

import pytest

from robotarium import simulate
from robotarium.bundle import build
from robotarium.experiment import Experiment, load_project
from robotarium.findings import ERROR, WARNING
from robotarium.simulate import SimulationReport
from robotarium.submission import prepare

from conftest import HAS_SIMULATOR, write

needs_simulator = pytest.mark.skipif(not HAS_SIMULATOR, reason="rps is not installed")


def levels(findings, level):
    return " | ".join(each.message for each in findings if each.level == level)


def experiment(**changes):
    fields = dict(title="T", description="D", duration=60, robots=4, main="/x/main.py")
    fields.update(changes)
    return Experiment(**fields)


def test_a_clean_run_has_no_errors():
    report = SimulationReport(returncode=0, rps="/rps", robots=[4], init_steps=300,
                              steps=900, ended=True)
    found = report.findings(experiment())
    assert levels(found, ERROR) == "" and levels(found, WARNING) == ""
    assert report.testbed_seconds == pytest.approx(39.6)
    assert report.suggested_duration == 60


@pytest.mark.parametrize("errors, expected", [
    ({"robots_too_close": 3}, "too close"),
    ({"robots_outside_boundaries": 1}, "outside the arena"),
])
def test_what_the_testbed_rejects_is_an_error(errors, expected):
    report = SimulationReport(returncode=0, rps="/rps", robots=[4], steps=10, ended=True,
                              errors=errors)
    assert expected in levels(report.findings(experiment()), ERROR)


def test_actuator_limits_are_a_warning():
    report = SimulationReport(returncode=0, rps="/rps", robots=[4], steps=100, ended=True,
                              errors={"exceeded_actuator_limits": 10})
    assert "10 of 100" in levels(report.findings(experiment()), WARNING)


def test_a_robot_count_that_disagrees_with_the_form_is_an_error():
    report = SimulationReport(returncode=0, rps="/rps", robots=[6], steps=10, ended=True)
    assert "ran 6 robots" in levels(report.findings(experiment()), ERROR)


def test_a_run_longer_than_declared_is_an_error():
    report = SimulationReport(returncode=0, rps="/rps", robots=[4], steps=3000, ended=True)
    assert "Declare 180" in levels(report.findings(experiment(duration=60)), ERROR)


def test_a_missing_simulator_says_how_to_get_it():
    found = SimulationReport(returncode=3).findings()
    assert "pip install" in levels(found, ERROR)


def test_a_crash_is_an_error():
    report = SimulationReport(returncode=1, rps="/rps", robots=[4], steps=5,
                              exception="Traceback ...\nZeroDivisionError: division by zero")
    assert "ZeroDivisionError" in levels(report.findings(experiment()), ERROR)


def test_a_simulator_path_must_be_a_checkout(tmp_path):
    with pytest.raises(FileNotFoundError, match="rps/__init__.py"):
        simulate.simulator_path(tmp_path)


@needs_simulator
def test_the_scaffolded_experiment_runs_clean(project_dir):
    project = load_project(project_dir)
    bundle = build(project.experiment, project.output_dir)
    report = simulate.run(bundle, log_dir=project.output_dir / "runs", echo=False, timeout=300)
    assert report.returncode == 0, report.output
    assert report.robots == [4] and report.steps == 150 and report.init_steps > 0
    assert report.ended
    assert levels(report.findings(project.experiment), ERROR) == ""
    assert list((project.output_dir / "runs").glob("circle_*.npz"))


@needs_simulator
def test_measured_takes_the_duration_from_the_simulation(project_dir):
    manifest = project_dir / "robotarium.toml"
    manifest.write_text(manifest.read_text().replace('duration = "auto"', 'duration = "measured"'))
    prepared = prepare(project_dir, echo=False)
    assert prepared.experiment.duration == prepared.report.suggested_duration
    assert prepared.bundle.duration == prepared.report.suggested_duration
    assert levels(prepared.findings, ERROR) == ""


@needs_simulator
def test_a_collision_course_is_caught(tmp_path):
    # Both robots driven to the same point, with no barrier certificate to stop them.
    write(tmp_path / "main.py", """\
import numpy as np
import rps.robotarium as robotarium
from rps.utilities.controllers import create_si_position_controller
from rps.utilities.transformations import create_si_to_uni_mapping

poses = np.array([[-0.5, 0.5], [0.0, 0.0], [0.0, np.pi]])
r = robotarium.Robotarium(number_of_robots=2, initial_conditions=poses)
si_to_uni, uni_to_si = create_si_to_uni_mapping()
controller = create_si_position_controller()
for _ in range(300):
    x = r.get_poses()
    r.set_velocities(np.arange(2), si_to_uni(controller(uni_to_si(x), np.zeros((2, 2))), x))
    r.step()
r.debug()
""")
    experiment = Experiment(title="T", description="D", duration=60, robots=2, main="main.py",
                            root=tmp_path)
    bundle = build(experiment, tmp_path / ".robotarium")
    report = simulate.run(bundle, echo=False, timeout=300)
    assert "too close" in levels(report.findings(experiment), ERROR)


def test_a_timeout_is_reported_with_the_rest_of_the_verdict():
    report = SimulationReport(returncode=-2, timed_out=True, rps="/rps", robots=[6], steps=40,
                              errors={"robots_too_close": 2})
    found = levels(report.findings(experiment()), ERROR)
    assert "stopped at the timeout, after 40 steps" in found
    assert "too close" in found and "ran 6 robots" in found
    assert "failed in the simulator" not in found


def test_a_killed_run_does_not_claim_a_step_count():
    found = levels(SimulationReport(returncode=-9, timed_out=True).findings(experiment()), ERROR)
    assert "unknown number of steps" in found


def _bundle_of(tmp_path, source, robots=2):
    write(tmp_path / "main.py", source)
    experiment = Experiment(title="T", description="D", duration=60, robots=robots,
                            main="main.py", root=tmp_path)
    return experiment, build(experiment, tmp_path / ".robotarium")


@needs_simulator
def test_initialization_is_never_skipped(tmp_path):
    experiment, bundle = _bundle_of(tmp_path, """\
import numpy as np
import rps.robotarium as robotarium
poses = np.array([[-0.5, 0.5], [0.0, 0.0], [0.0, 0.0]])
r = robotarium.Robotarium(number_of_robots=2, initial_conditions=poses, skip_initialization=True)
for _ in range(10):
    r.get_poses()
    r.step()
r.debug()
""")
    report = simulate.run(bundle, echo=False, timeout=300)
    assert report.init_steps > 0 and report.steps == 10 and report.skipped_initialization
    assert "overridden" in " ".join(each.message for each in report.findings(experiment))


@needs_simulator
def test_a_timed_out_run_still_reports(tmp_path):
    experiment, bundle = _bundle_of(tmp_path, """\
import time
import rps.robotarium as robotarium
r = robotarium.Robotarium(number_of_robots=2)
while True:
    r.get_poses()
    r.step()
    time.sleep(0.01)
""")
    report = simulate.run(bundle, echo=False, timeout=8)
    assert report.timed_out and report.robots == [2] and report.steps > 0
    assert "stopped at the timeout" in levels(report.findings(experiment), ERROR)


@needs_simulator
def test_a_run_that_never_ends_stops_at_the_forms_maximum(tmp_path):
    experiment, bundle = _bundle_of(tmp_path, """\
import rps.robotarium as robotarium
r = robotarium.Robotarium(number_of_robots=2)
while True:
    try:
        r.get_poses()
        r.step()
    except Exception:
        pass
""")
    report = simulate.run(bundle, echo=False, timeout=300)
    assert report.too_long and not report.timed_out
    assert report.total_steps == int(600 / 0.033) + 2
    assert "longer than the form's 600 s maximum" in levels(report.findings(experiment), ERROR)


@needs_simulator
def test_a_finished_run_is_not_a_timed_out_one(tmp_path):
    experiment, bundle = _bundle_of(tmp_path, """\
import rps.robotarium as robotarium
r = robotarium.Robotarium(number_of_robots=2)
r.get_poses()
r.step()
r.debug()
""")
    report = simulate.run(bundle, echo=False, timeout=120)
    assert report.returncode == 0 and not report.timed_out


@needs_simulator
def test_a_process_the_main_file_leaves_behind_does_not_hold_the_run(tmp_path):
    experiment, bundle = _bundle_of(tmp_path, """\
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
time.sleep(60)
""")
    import time
    started = time.time()
    report = simulate.run(bundle, echo=False, timeout=3)
    assert report.timed_out and time.time() - started < 30


@needs_simulator
def test_the_run_never_sees_the_login(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOTARIUM_PASSWORD", "hunter2-secret")
    monkeypatch.setenv("ROBOTARIUM_USERNAME", "someone@example.edu")
    experiment, bundle = _bundle_of(tmp_path, """\
import os
print("seen:", os.environ.get("ROBOTARIUM_PASSWORD"), os.environ.get("ROBOTARIUM_USERNAME"))
""")
    report = simulate.run(bundle, echo=False, timeout=60)
    assert "seen: None None" in report.output


@needs_simulator
def test_an_exit_message_is_kept(tmp_path):
    experiment, bundle = _bundle_of(tmp_path, "import sys\nsys.exit('bad parameters')\n")
    report = simulate.run(bundle, echo=False, timeout=60)
    assert report.returncode == 1 and "bad parameters" in report.output


def test_an_rps_without_skip_initialization_still_runs(tmp_path):
    """rps before V2 has an explicit __init__ signature that refuses unknown keywords."""
    write(tmp_path / "oldsim" / "rps" / "__init__.py", "")
    write(tmp_path / "oldsim" / "rps" / "robotarium.py", """\
class Robotarium:
    def __init__(self, number_of_robots=-1, show_figure=True, sim_in_real_time=True,
                 initial_conditions=None):
        self.number_of_robots = number_of_robots
        self._errors = {}

    def get_poses(self):
        return None

    def step(self):
        pass

    def call_at_scripts_end(self):
        print("old rps: done")
""")
    experiment, bundle = _bundle_of(tmp_path / "exp", """\
import rps.robotarium as robotarium
r = robotarium.Robotarium(number_of_robots=2, show_figure=False)
r.get_poses()
r.step()
r.call_at_scripts_end()
""")
    report = simulate.run(bundle, simulator=tmp_path / "oldsim", echo=False, timeout=60)
    assert report.returncode == 0, report.output
    assert report.robots == [2] and report.steps == 1 and report.ended



def test_a_cut_short_run_gets_no_duration_advice():
    for report in (SimulationReport(returncode=4, rps="/rps", robots=[4], steps=18183,
                                    too_long=True, ended=False),
                   SimulationReport(returncode=-2, rps="/rps", robots=[4], steps=900,
                                    timed_out=True)):
        found = report.findings(experiment(duration=60))
        text = " ".join(each.message for each in found)
        assert "Suggested estimated duration" not in text and "Declare" not in text


@needs_simulator
def test_a_bare_except_cannot_keep_a_run_past_the_cap(tmp_path):
    experiment, bundle = _bundle_of(tmp_path, """\
import rps.robotarium as robotarium
r = robotarium.Robotarium(number_of_robots=2)
while True:
    try:
        r.get_poses()
        r.step()
    except:
        pass
""")
    report = simulate.run(bundle, echo=False, timeout=300)
    assert report.too_long and not report.timed_out


@needs_simulator
def test_output_that_is_not_text_cannot_hang_the_run(tmp_path):
    experiment, bundle = _bundle_of(tmp_path, """\
import sys
for _ in range(2000):
    sys.stdout.buffer.write(b"\\xff\\xfe not utf-8 " * 10 + b"\\n")
sys.stdout.flush()
print("done")
""")
    report = simulate.run(bundle, echo=False, timeout=120)
    assert report.returncode == 0 and not report.timed_out and "done" in report.output


@needs_simulator
def test_a_run_that_ends_while_its_output_is_drained_is_not_timed_out(tmp_path, monkeypatch):
    import time
    # Few enough lines to fit any pipe's buffer (Windows' is small), so the run is finished
    # long before the console, slowed below, has shown them all.
    experiment, bundle = _bundle_of(tmp_path, """\
for index in range(300):
    print("line %d" % index)
""")
    real_write = sys.stdout.write

    def slow(text):
        time.sleep(0.01)
        return real_write("")

    monkeypatch.setattr(sys.stdout, "write", slow)
    report = simulate.run(bundle, echo=True, timeout=1.5)
    assert report.returncode == 0 and not report.timed_out


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
@needs_simulator
def test_terminating_robotarium_ends_the_run_too(tmp_path):
    import signal
    import subprocess
    import time
    pid_file = tmp_path / "child.pid"
    experiment, bundle = _bundle_of(tmp_path / "exp", """\
import os, time
open(%r, "w").write(str(os.getpid()))
time.sleep(120)
""" % str(pid_file))
    parent = subprocess.Popen([sys.executable, "-c", (
        "import sys; from pathlib import Path; from robotarium import simulate\n"
        "from robotarium.bundle import Bundle\n"
        "b = Bundle(directory=Path(%r), files=[Path(%r)], title='t', description='d',"
        " duration=60, robots=2, built_at='x')\n"
        "simulate.run(b, echo=False)\n") % (str(bundle.directory), str(bundle.main))])
    deadline = time.time() + 60
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.1)
    child = int(pid_file.read_text())
    parent.send_signal(signal.SIGTERM)
    parent.wait(timeout=30)
    time.sleep(0.5)
    try:
        os.kill(child, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, "the run outlived a terminated robotarium"



@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_nohup_and_custom_handlers_are_left_alone():
    import signal
    previous = signal.getsignal(signal.SIGHUP)
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)       # as `nohup` leaves it
        restore = simulate._stop_with_this_process()
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        restore()

        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        restore = simulate._stop_with_this_process()
        assert signal.getsignal(signal.SIGHUP) not in (signal.SIG_DFL, signal.SIG_IGN)
        restore()
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGHUP, previous)


@needs_simulator
def test_the_step_cap_still_saves_the_run_log(tmp_path):
    experiment, bundle = _bundle_of(tmp_path, """\
import rps.robotarium as robotarium
from robotarium_runtime import RunLogger
log = RunLogger("capped")
r = robotarium.Robotarium(number_of_robots=2)
while True:
    x = r.get_poses()
    log.record(poses=x)
    r.step()
""")
    report = simulate.run(bundle, echo=False, timeout=300, log_dir=tmp_path / "runs")
    assert report.too_long
    saved = list((tmp_path / "runs").glob("capped_*.npz"))
    assert len(saved) == 1 and not list((tmp_path / "runs").glob("*.partial"))


@needs_simulator
def test_the_run_writes_utf8_whatever_the_console(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")    # a console that cannot show a check
    experiment, bundle = _bundle_of(tmp_path, 'print("café ✓ θ")\n')
    report = simulate.run(bundle, echo=False, timeout=60)
    assert report.returncode == 0 and "café ✓ θ" in report.output


def test_a_run_logs_copy_is_one_line_on_the_console(capsys):
    content = b"PK" * 3000
    stream = io.StringIO("step 1\n-----BEGIN ROBOTARIUM RUN run_1.npz %d %s-----\n%s"
                         "-----END ROBOTARIUM RUN run_1.npz-----\nRun log: run_1.npz\n" % (
                             len(content), hashlib.sha256(content).hexdigest(),
                             base64.encodebytes(content).decode("ascii")))
    tail = collections.deque(maxlen=60)
    simulate._relay(stream, tail, echo=True)
    shown = ["step 1\n", "(a copy of run_1.npz, 6.0 KB, printed for the testbed's log.txt)\n",
             "Run log: run_1.npz\n"]
    assert list(tail) == shown and capsys.readouterr().out == "".join(shown)


# ---------------------------------------------------------------------------
# the duration budget, with Julia's startup
# ---------------------------------------------------------------------------

def run_of(steps, init_steps=300, **changes):
    return SimulationReport(returncode=0, rps="/rps", robots=[4], init_steps=init_steps,
                            steps=steps, ended=True, **changes)


def test_startup_and_steps_must_fit_a_fixed_duration():
    report = run_of(1500)                                   # 59.4 s of steps
    assert levels(report.findings(experiment(duration=120)), ERROR) == ""
    found = levels(report.findings(experiment(duration=120), startup=180), ERROR)
    assert "about 239 s on the testbed (180 s of Julia startup, then 59 s of steps)" in found
    assert "Declare 300" in found


def test_under_auto_a_long_startup_is_warned_about_then_refused():
    assert levels(run_of(9000).findings(experiment(duration="auto")), WARNING) == ""
    near = run_of(9000).findings(experiment(duration="auto"), startup=270)   # 307 + 270 s
    assert "577 s of the 600 s that \"auto\" declares" in levels(near, WARNING)
    over = run_of(12000).findings(experiment(duration="auto"), startup=270)  # 406 + 270 s
    assert "over the form's 600 s maximum" in levels(over, ERROR)


def test_measured_counts_the_startup_in_its_suggestion():
    report = run_of(1500)
    assert report.suggested() == report.suggested_duration == 120
    assert report.suggested(startup=180) == 300
    assert "Suggested estimated duration: 300 s" in " ".join(
        each.message for each in report.findings(experiment(duration="measured"), startup=180))


def test_a_run_without_julia_is_judged_as_before():
    report = run_of(3000)
    assert report.findings(experiment(duration=60)) == \
        report.findings(experiment(duration=60), startup=None)


def test_a_slow_loop_is_a_warning_and_a_quick_one_a_note():
    slow = run_of(100, loop_ms={"median": 20.0, "p99": 41.5, "max": 80.0, "count": 99})
    assert "99th percentile 41.5 ms" in levels(slow.findings(experiment()), WARNING)
    quick = run_of(100, loop_ms={"median": 1.0, "p99": 2.0, "max": 3.0, "count": 99})
    assert levels(quick.findings(experiment()), WARNING) == ""
    assert "within the testbed's 33 ms step" in " ".join(
        each.message for each in quick.findings(experiment()))


def test_the_shims_loop_statistics():
    from robotarium._simshim import loop_statistics
    assert loop_statistics([]) is None
    stats = loop_statistics([0.001] * 98 + [0.050, 0.100])
    assert stats["median"] == 1.0 and stats["max"] == 100.0 and stats["count"] == 100
    assert 1.0 < stats["p99"] <= 100.0


# ---------------------------------------------------------------------------
# recording: videos and stills
# ---------------------------------------------------------------------------

def test_frames_are_read_in_order_without_repeats():
    assert simulate.parse_frames("0, 14.5,end,14.5, 30s") == [0.0, 14.5, "end", 30.0]
    assert simulate.parse_frames([0, 30, "END"]) == [0.0, 30.0, "end"]
    assert simulate.parse_frames(None) == []
    for bad in ("soon", "-1", "nan"):
        with pytest.raises(ValueError):
            simulate.parse_frames(bad)


def test_a_still_is_taken_at_the_first_step_at_or_after_its_time():
    from robotarium._simshim import still_step as shim_still_step
    for seconds, step in ((0, 0), (14.5, 440), (14, 425), (0.33, 10), (0.034, 2)):
        assert simulate.still_step(seconds) == shim_still_step(seconds) == step


def test_still_names_have_one_dot():
    assert [simulate.still_name(each) for each in (0, 14, 14.5, 0.25, "end")] == \
        ["t0s.png", "t14s.png", "t14p5s.png", "t0p25s.png", "end.png"]
    assert simulate.still_name(3, "pdf") == "t3s.pdf"


def test_a_recording_checks_its_settings(tmp_path):
    recording = simulate.Recording(video=tmp_path / "a.mp4", every=2)
    assert recording.fps == 15 and recording.dpi == 200
    environment = recording.environment()
    assert environment["ROBOTARIUM_SIM_VIDEO"] == str((tmp_path / "a.mp4").resolve())
    assert environment["ROBOTARIUM_SIM_EVERY"] == "2"
    for bad in (dict(video=tmp_path / "a.avi"), dict(frames="0", frames_format="jpg",
                                                     frames_dir=tmp_path),
                dict(frames="0"), dict(video=tmp_path / "a.mp4", dpi=0),
                dict(video=tmp_path / "a.mp4", fps=0)):
        with pytest.raises(ValueError):
            simulate.Recording(**bad)


def test_a_video_without_ffmpeg_is_refused_before_anything_runs(tmp_path, monkeypatch):
    experiment_, bundle = _bundle_of(tmp_path, "print('ran')\n")
    monkeypatch.setattr(simulate.shutil, "which", lambda name: None)
    started = []
    monkeypatch.setattr(simulate.subprocess, "Popen", lambda *a, **k: started.append(a))
    with pytest.raises(ValueError, match="needs ffmpeg"):
        simulate.run(bundle, recording=simulate.Recording(video=tmp_path / "run.mp4"))
    assert started == []
    # Stills alone need no ffmpeg.
    simulate.Recording(frames="0", frames_dir=tmp_path).check()


def test_what_a_recording_reports_becomes_findings():
    report = run_of(300, figure_forced=True, frames_missing=[30.0, 45.0],
                    recording_error="ffmpeg exited with code 1 writing run.mp4")
    found = report.findings(experiment())
    assert "show_figure=False was overridden" in levels(found, "info")
    warned = levels(found, WARNING)
    assert "No still at 30 s: the experiment ended at 9.9 s" in warned
    assert "No still at 45 s" in warned and "ffmpeg exited" in warned


def test_the_last_simulation_is_kept_for_its_files(tmp_path):
    experiment_, bundle = _bundle_of(tmp_path, "print('ran')\n")
    report = run_of(900, init_steps=200)
    assert simulate.save_last(report, bundle, tmp_path / ".robotarium", {"a": 1}).is_file()
    last = simulate.last_simulation(bundle, tmp_path / ".robotarium", {"a": 1})
    assert (last["steps"], last["init_steps"]) == (900, 200)
    assert last["testbed_seconds"] == pytest.approx(36.3)
    assert simulate.last_simulation(bundle, tmp_path / ".robotarium", {"a": 2}) is None
    write(tmp_path / "main.py", "print('changed')\n")
    changed = build(experiment_, tmp_path / ".robotarium")
    assert simulate.last_simulation(changed, tmp_path / ".robotarium") is None
    assert simulate.save_last(run_of(10, too_long=True), bundle, tmp_path / "x") is None


RECORDED = """\
import numpy as np
import rps.robotarium as robotarium
from robotarium_runtime import FloorOverlay, RunLogger
log = RunLogger("recorded")
r = robotarium.Robotarium(number_of_robots=2, show_figure=False)
overlay = FloorOverlay(r)
for k in range(20):
    x = r.get_poses()
    overlay.clock(k * 0.033)
    log.record(poses=x)
    r.set_velocities(np.arange(2), np.array([[0.1, 0.1], [0.0, 0.0]]))
    r.step()
r.debug()
"""


def _png_size(path):
    import struct
    return struct.unpack(">II", path.read_bytes()[16:24])


@needs_simulator
def test_stills_are_taken_at_experiment_times(tmp_path):
    experiment_, bundle = _bundle_of(tmp_path, RECORDED)
    frames = tmp_path / "frames"
    report = simulate.run(bundle, echo=False, timeout=300, log_dir=tmp_path / "runs",
                          recording=simulate.Recording(frames="0,0.5,end,30",
                                                       frames_dir=frames, dpi=40))
    assert report.returncode == 0, report.output
    assert sorted(path.name for path in frames.iterdir()) == ["end.png", "t0p5s.png",
                                                             "t0s.png"]
    assert _png_size(frames / "t0s.png") == (330, 210)          # 8.25 x 5.25 in at 40 dpi
    assert report.stills["t0p5s"]["seconds"] == 0.5
    assert report.stills["end"]["seconds"] == pytest.approx(0.66)
    assert report.figure_forced and report.frames_missing == [30.0]
    found = report.findings(experiment_)
    assert "No still at 30 s" in levels(found, WARNING)
    assert report.loop_ms["count"] == 19


@needs_simulator
@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_video_is_an_mp4(tmp_path):
    experiment_, bundle = _bundle_of(tmp_path, RECORDED)
    video = tmp_path / "videos" / "run.mp4"
    report = simulate.run(bundle, echo=False, timeout=300, log_dir=tmp_path / "runs",
                          recording=simulate.Recording(video=video, dpi=40, every=2))
    assert report.returncode == 0, report.output
    assert report.video == str(video.resolve())
    assert video.read_bytes()[4:8] == b"ftyp"


@needs_simulator
def test_one_seed_gives_one_start(tmp_path):
    experiment_, bundle = _bundle_of(tmp_path, RECORDED)
    import robotarium_runtime

    def first_poses(seed, name):
        runs = tmp_path / name
        report = simulate.run(bundle, echo=False, timeout=300, log_dir=runs, seed=seed)
        assert report.returncode == 0 and report.seed == seed, report.output
        return robotarium_runtime.load_run(next(runs.glob("recorded_*.npz"))).data["poses"][0]

    first = first_poses(4, "a")
    assert (first_poses(4, "b") == first).all()
    assert not (first_poses(5, "c") == first).all()
