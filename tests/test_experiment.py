"""The experiment's fields and files, and reading them from robotarium.toml."""

import datetime

import pytest

from robotarium.experiment import Experiment, load_project, parse_duration
from robotarium.findings import ERROR, INFO, errors

from conftest import write


@pytest.mark.parametrize("given, seconds", [
    (90, 90), (89.2, 90), ("90", 90), ("90s", 90), ("2m", 120), ("1m30s", 90),
    ("1.5 min", 90), ("2 minutes", 120), ("45 sec", 45), ("auto", "auto"), ("AUTO", "auto"),
])
def test_durations_are_read_as_whole_seconds(given, seconds):
    assert parse_duration(given) == seconds


@pytest.mark.parametrize("given", ["", "soon", "2h", "m", True])
def test_what_is_not_a_duration_is_refused(given):
    with pytest.raises(ValueError):
        parse_duration(given)


def experiment(tmp_path, **changes):
    write(tmp_path / "main.py", "import rps.robotarium\n")
    fields = dict(title="T", description="D", duration=60, robots=4, main="main.py",
                  root=tmp_path)
    fields.update(changes)
    return Experiment(**fields)


def test_a_sound_experiment_has_no_errors(tmp_path):
    assert errors(experiment(tmp_path).check()) == []


@pytest.mark.parametrize("robots", [0, 19, True, 2.5])
def test_the_robot_count_must_be_what_the_arena_holds(tmp_path, robots):
    assert errors(experiment(tmp_path, robots=robots).check())


@pytest.mark.parametrize("duration", [0, 601, "11m", "later"])
def test_the_duration_must_be_within_the_forms_cap(tmp_path, duration):
    assert errors(experiment(tmp_path, duration=duration).check())


def test_auto_is_a_duration(tmp_path):
    assert errors(experiment(tmp_path, duration="auto").check()) == []


@pytest.mark.parametrize("field", ["title", "description"])
def test_title_and_description_are_required(tmp_path, field):
    assert errors(experiment(tmp_path, **{field: "  "}).check())


def test_files_the_form_refuses_are_errors(tmp_path):
    write(tmp_path / "params.yaml", "a: 1\n")
    write(tmp_path / "data" / "x.npy", "")
    (tmp_path / "pkg").mkdir()
    found = experiment(tmp_path, files=["params.yaml", "pkg", "missing.py"]).check()
    messages = " ".join(each.message for each in errors(found))
    assert "does not take .yaml" in messages
    assert "is a directory" in messages
    assert "missing.py does not exist" in messages


def test_two_files_with_one_base_name_collide(tmp_path):
    write(tmp_path / "a" / "util.py", "")
    write(tmp_path / "b" / "util.py", "")
    found = errors(experiment(tmp_path, files=["a/util.py", "b/util.py"]).check())
    assert any("would be uploaded as util.py" in each.message for each in found)


def test_a_file_from_a_subdirectory_is_noted_as_flattened(tmp_path):
    write(tmp_path / "data" / "waypoints.npy", "")
    found = experiment(tmp_path, files=["data/*.npy"]).check()
    assert any(each.level == INFO and "flat" in each.message for each in found)


def test_globs_expand_and_the_main_file_comes_first(tmp_path):
    write(tmp_path / "b.py", "")
    write(tmp_path / "a.py", "")
    paths = experiment(tmp_path, files=["*.py"]).upload_files()
    assert [path.name for path in paths] == ["main.py", "a.py", "b.py"]


def test_a_generated_name_is_reserved(tmp_path):
    write(tmp_path / "robotarium_runtime.py", "")
    assert errors(experiment(tmp_path, files=["robotarium_runtime.py"]).check())


def test_a_main_file_must_be_python_or_matlab(tmp_path):
    write(tmp_path / "main.jl", "")
    assert errors(experiment(tmp_path, main="main.jl").check())


def test_the_title_fills_its_placeholders(tmp_path):
    when = datetime.datetime(2026, 10, 21, 15, 31)
    title = experiment(tmp_path, title="{main} x{robots} {timestamp} {date} {time} {git}") \
        .render_title(when=when, git="abc1234")
    assert title == "main x4 20261021T1531 2026-10-21 15:31 abc1234"


def test_an_unknown_placeholder_is_an_error(tmp_path):
    assert errors(experiment(tmp_path, title="Run {seed}").check())


def test_a_julia_package_needs_a_real_uuid(tmp_path):
    bad = experiment(tmp_path, julia_packages=[{"name": "JSON3", "uuid": "nope"}])
    assert any("robotarium julia lookup JSON3" in each.message for each in errors(bad.check()))


# ---------------------------------------------------------------------------
# robotarium.toml
# ---------------------------------------------------------------------------

MANIFEST = '''
[experiment]
title = "Run ({date})"
description_file = "ABOUT.md"
duration = "2m"
robots = 3
main = "main.py"
files = ["helpers.py"]
allow_imports = ["mything"]

[config]
gain = 2.5

[julia]
version = "1.11"
packages = [{ name = "JSON3", uuid = "0f8b85d8-7281-11e9-16c2-39a750bddbf1", version = "1" }]

[simulator]
path = "../sim"
fast = false

[submit]
draft = true
simulate = true

[output]
dir = "out"
'''


def test_a_manifest_is_read_whole(tmp_path):
    write(tmp_path / "robotarium.toml", MANIFEST)
    write(tmp_path / "ABOUT.md", "  Three robots.\n")
    project = load_project(tmp_path)
    experiment = project.experiment
    assert (experiment.title, experiment.description, experiment.duration,
            experiment.robots) == ("Run ({date})", "Three robots.", 120, 3)
    assert experiment.main == tmp_path / "main.py"
    assert experiment.load_config() == {"gain": 2.5}
    assert experiment.julia_version == "1.11"
    assert experiment.julia_packages[0].name == "JSON3"
    assert experiment.allow_imports == ["mything"]
    assert project.simulator_path == (tmp_path.parent / "sim").resolve()
    assert project.simulator_fast is False and project.simulator_headless is True
    assert project.draft is True and project.simulate_first is True
    assert project.output_dir == tmp_path / "out"


def test_the_manifest_is_found_from_below(tmp_path):
    write(tmp_path / "robotarium.toml", MANIFEST)
    write(tmp_path / "ABOUT.md", "Three robots.")
    (tmp_path / "deep" / "er").mkdir(parents=True)
    assert load_project(tmp_path / "deep" / "er").manifest == tmp_path / "robotarium.toml"


def test_a_misspelt_key_fails_loudly(tmp_path):
    write(tmp_path / "robotarium.toml", MANIFEST.replace("robots = 3", "robot = 3"))
    with pytest.raises(ValueError, match="experiment.robot"):
        load_project(tmp_path)


def test_a_missing_field_is_named(tmp_path):
    write(tmp_path / "robotarium.toml", '[experiment]\ntitle = "x"\nmain = "main.py"\n')
    with pytest.raises(ValueError, match="duration, robots, description"):
        load_project(tmp_path)


def test_no_manifest_says_how_to_make_one(tmp_path):
    with pytest.raises(FileNotFoundError, match="robotarium init"):
        load_project(tmp_path)


def test_a_config_file_is_read(tmp_path):
    write(tmp_path / "params.json", '{"gain": 1}')
    assert experiment(tmp_path, config="params.json").load_config() == {"gain": 1}


def test_a_config_that_is_not_plain_data_is_an_error(tmp_path):
    write(tmp_path / "params.toml", "when = 2026-10-21\n")
    found = errors(experiment(tmp_path, config="params.toml").check())
    assert any("cannot travel to the testbed" in each.message for each in found)


def test_a_matlab_main_file_is_accepted(tmp_path):
    write(tmp_path / "main.m", "r = Robotarium('NumberOfRobots', 4);\n")
    assert errors(experiment(tmp_path, main="main.m").check()) == []
