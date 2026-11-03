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


def test_a_stated_startup_beats_a_cold_start_here(tmp_path):
    found = project(tmp_path, julia_startup=90)
    write_cold_start(found)
    estimate = budget.startup_estimate(found)
    assert (estimate.seconds, estimate.source) == (90, "stated")


def test_a_testbed_timing_above_the_stated_startup_is_counted_and_warned_about(tmp_path):
    found = project(tmp_path, julia_startup=100)
    write_testbed_timing(found)                               # 150 s x 1.1 = 165 s
    estimate = budget.startup_estimate(found)
    assert (estimate.source, estimate.seconds) == ("testbed", pytest.approx(165.0))
    warning, = [each for each in estimate.findings if each.level == WARNING]
    assert "[julia] startup = 100 is less than the testbed's own timing" in warning.message
    assert "at least 165" in warning.message


def test_a_stated_startup_above_the_testbed_timing_stands(tmp_path):
    found = project(tmp_path, julia_startup=200)
    write_testbed_timing(found)
    estimate = budget.startup_estimate(found)
    assert (estimate.source, estimate.seconds) == ("stated", 200)
    assert [each.level for each in estimate.findings] == [INFO]
    assert "about 165 s" in estimate.findings[0].message


def test_a_cold_start_without_package_servers_is_an_upper_bound(tmp_path):
    found = project(tmp_path)
    write_cold_start(found, pkg_server="")
    estimate = budget.startup_estimate(found)
    assert (estimate.seconds, estimate.upper_bound) == (200.0, True)   # no margin on it
    assert "at most about 200 s" in estimate.findings[0].message
    write_cold_start(found, pkg_server=None)                 # Pkg's own server: x 1.5
    estimate = budget.startup_estimate(found)
    assert (estimate.seconds, estimate.upper_bound) == (300.0, False)


def test_a_cold_start_with_other_juliacall_than_the_testbeds_is_warned_about(tmp_path):
    found = project(tmp_path)
    write_cold_start(found, juliacall="0.9.1", key=budget.project_key(found.experiment,
                                                                        "0.9.1", "0.1.24"))
    estimate = budget.startup_estimate(found)
    assert estimate.source == "cold-start"
    assert any(each.level == WARNING and "juliacall 0.9.1" in each.message
               for each in estimate.findings)


# ---------------------------------------------------------------------------
# the duration checks without a simulation
# ---------------------------------------------------------------------------

def built(found):
    from robotarium.bundle import build
    return build(found.experiment, found.output_dir)


def messages(findings, level):
    return " | ".join(each.message for each in findings if each.level == level)


def test_a_startup_that_alone_takes_the_duration_is_an_error_without_a_simulation(tmp_path):
    from robotarium.findings import ERROR
    found = project(tmp_path, julia_startup="3m")            # 180 s against 120 s
    text = messages(budget.budget_findings(found, built(found)), ERROR)
    assert "Julia's startup alone, about 180 s" in text and "120 s declared" in text
    fits = project(tmp_path / "fits", julia_startup=60)
    assert messages(budget.budget_findings(fits, built(fits)), ERROR) == ""


def test_a_startup_over_the_maximum_is_an_error_under_auto(tmp_path):
    from robotarium.findings import ERROR
    found = project(tmp_path, duration="auto", julia_startup=600)
    assert "over the form's 600 s maximum" in messages(
        budget.budget_findings(found, built(found)), ERROR)


def test_an_upper_bound_alone_is_only_a_warning(tmp_path):
    from robotarium.findings import ERROR
    found = project(tmp_path, duration="auto")
    write_cold_start(found, startup_seconds=700.0, pkg_server="")
    findings = budget.budget_findings(found, built(found))
    assert messages(findings, ERROR) == ""
    assert "at most about 700 s" in messages(findings, WARNING)


def test_the_last_simulation_is_judged_with_the_startup(tmp_path):
    from robotarium import simulate
    from robotarium.findings import ERROR
    found = project(tmp_path, duration=120, julia_startup=60)
    bundle = built(found)
    report = simulate.SimulationReport(returncode=0, rps="/rps", robots=[2], init_steps=300,
                                       steps=1500, ended=True)      # 59.4 s of steps
    simulate.save_last(report, bundle, found.output_dir, found.experiment.load_config())
    text = messages(budget.budget_findings(found, bundle), ERROR)
    assert "about 119 s" not in text                          # 60 + 59.4 fits 120 s
    found.experiment.julia_startup = 90
    text = messages(budget.budget_findings(found, bundle), ERROR)
    assert "The run takes about 149 s" in text and "Declare 240" in text


def test_the_budget_line_adds_it_up(tmp_path):
    estimate = budget.startup_estimate(project(tmp_path, julia_startup=240))
    line = budget.budget_line(estimate, 95.0, "auto", steps=2879)
    assert line == ("Budget: 240 s Julia startup ([julia] startup = 240) + 95 s robots (2879 "
                    "steps) = 335 s, against 600 s declared (\"auto\").")
    assert "would be declared" in budget.budget_line(estimate, 95.0, "measured", steps=2879)
    assert budget.budget_line(None, 30.0, 60) == "Budget: 30 s robots = 30 s, against 60 s " \
                                                 "declared."
