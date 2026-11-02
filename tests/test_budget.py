"""The Julia startup estimate the duration checks count, and where it comes from."""

import json

import pytest

from robotarium import budget, limits
from robotarium.experiment import Experiment, Project
from robotarium.findings import INFO, WARNING

from conftest import write

JSON3 = {"name": "JSON3", "uuid": "0f8b85d8-7281-11e9-16c2-39a750bddbf1", "version": "1"}


def project(tmp_path, **changes):
    write(tmp_path / "main.py", "import rps.robotarium\n")
    write(tmp_path / "backend.jl", "module Backend\nend\n")
    fields = dict(title="T", description="D", duration=120, robots=2, main="main.py",
                  files=["backend.jl"], root=tmp_path, julia_packages=[JSON3],
                  julia_version="=1.11.9")
    fields.update(changes)
    return Project(experiment=Experiment(**fields))


def test_a_project_without_julia_has_no_startup(tmp_path):
    assert budget.startup_estimate(project(tmp_path, files=[], julia_packages=[])) is None


def test_a_jl_file_alone_counts_as_julia(tmp_path):
    assert budget.startup_estimate(project(tmp_path, julia_packages=[])).source == "assumed"


def test_nothing_measured_is_an_assumed_180_s_with_a_warning(tmp_path):
    estimate = budget.startup_estimate(project(tmp_path))
    assert (estimate.seconds, estimate.source) == (limits.ASSUMED_JULIA_STARTUP, "assumed")
    assert [each.level for each in estimate.findings] == [WARNING]
    assert "julia check --cold" in estimate.findings[0].message
    # Under "auto" the form's maximum is declared, and the checks warn only near it.
    auto = budget.startup_estimate(project(tmp_path, duration="auto"))
    assert [each.level for each in auto.findings] == [INFO]


def test_a_stated_startup_is_taken_as_it_is(tmp_path):
    estimate = budget.startup_estimate(project(tmp_path, julia_startup="4m"))
    assert (estimate.seconds, estimate.source) == (240, "stated")


def write_cold_start(found, **changes):
    experiment = found.experiment
    record = {"juliacall": "0.9.35", "juliapkg": "0.1.24", "startup_seconds": 200.0,
              "measured_at": "2026-11-02T12:00:00+00:00",
              "key": budget.project_key(experiment, "0.9.35", "0.1.24")}
    record.update(changes)
    write(found.output_dir / budget.COLD_START, json.dumps(record))


def test_a_cold_start_measured_here_counts_half_again(tmp_path):
    found = project(tmp_path)
    write_cold_start(found)
    estimate = budget.startup_estimate(found)
    assert (estimate.seconds, estimate.source) == (300.0, "cold-start")
    assert "200 s a cold start took here" in estimate.findings[0].message


def test_a_cold_start_of_other_packages_is_not_used(tmp_path):
    found = project(tmp_path)
    write_cold_start(found, key="something else")
    estimate = budget.startup_estimate(found)
    assert estimate.source == "assumed"
    assert "measured with other Julia packages" in estimate.findings[0].message


def write_testbed_timing(found, name="exp-1", total=150.0, key=None):
    testbed = budget.testbed_versions()
    write(found.output_dir / "results" / name / budget.TESTBED_TIMING, json.dumps(
        {"total_seconds": total,
         "key": key or budget.project_key(found.experiment, *testbed)}))


def test_the_testbeds_own_timing_comes_first(tmp_path):
    found = project(tmp_path)
    write_cold_start(found)
    write_testbed_timing(found)
    estimate = budget.startup_estimate(found)
    assert estimate.source == "testbed"
    assert estimate.seconds == pytest.approx(165.0)
    assert "exp-1" in estimate.findings[0].message
    # ... but not one of other packages.
    other = project(tmp_path / "other")
    write_testbed_timing(other, key="other")
    assert budget.startup_estimate(other).source == "assumed"


def test_a_stated_startup_beats_every_measurement(tmp_path):
    found = project(tmp_path, julia_startup=90)
    write_cold_start(found)
    write_testbed_timing(found)
    assert budget.startup_estimate(found).seconds == 90


def test_the_budget_line_adds_it_up(tmp_path):
    estimate = budget.startup_estimate(project(tmp_path, julia_startup=240))
    line = budget.budget_line(estimate, 95.0, "auto", steps=2879)
    assert line == ("Budget: 240 s Julia startup ([julia] startup = 240) + 95 s robots (2879 "
                    "steps) = 335 s, against 600 s declared (\"auto\").")
    assert "would be declared" in budget.budget_line(estimate, 95.0, "measured", steps=2879)
    assert budget.budget_line(None, 30.0, 60) == "Budget: 30 s robots = 30 s, against 60 s " \
                                                 "declared."
