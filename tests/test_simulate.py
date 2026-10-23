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
