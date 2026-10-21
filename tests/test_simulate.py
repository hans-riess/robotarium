"""Rehearsing in the Robotarium simulator: what the report says and what it is judged by."""

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
def test_auto_takes_the_duration_from_the_simulation(project_dir):
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
