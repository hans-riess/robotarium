"""The experiment's fields and files, and reading them from robotarium.toml."""

import datetime
import os

import pytest

from robotarium.experiment import Experiment, load_project, parse_duration, parse_setting
from robotarium.findings import ERROR, INFO, WARNING, errors

from conftest import write


@pytest.mark.parametrize("given, seconds", [
    (90, 90), (89.2, 90), ("90", 90), ("90s", 90), ("2m", 120), ("1m30s", 90),
    ("1.5 min", 90), ("2 minutes", 120), ("45 sec", 45), ("auto", "auto"), ("AUTO", "auto"),
    ("measured", "measured"),
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
    with pytest.raises(ValueError, match="robots, description"):
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


def test_globs_work_under_a_directory_with_brackets(tmp_path):
    root = tmp_path / "exp[1]"
    write(root / "data" / "a.npy", "")
    paths = experiment(root, files=["data/*.npy"]).upload_files()
    assert [path.name for path in paths] == ["main.py", "a.npy"]


def test_an_existing_file_under_brackets_is_a_file_not_a_pattern(tmp_path):
    root = tmp_path / "exp[1]"
    write(root / "data.npy", "")
    found = experiment(root, files=[str((root / "data.npy").resolve())])
    assert [path.name for path in found.upload_files()] == ["main.py", "data.npy"]
    assert not [each for each in found.check() if "matches no files" in each.message]


def test_a_home_glob_does_not_read_the_home_directory_as_a_pattern(tmp_path, monkeypatch):
    home = tmp_path / "home[me]"
    write(home / "data" / "a.npy", "")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))     # what ~ means on Windows
    for entry in ("~/data/*.npy", "~" + os.sep + "data" + os.sep + "*.npy"):
        paths = experiment(tmp_path, files=[entry]).upload_files()
        assert [path.name for path in paths] == ["main.py", "a.npy"]


def test_duration_may_be_left_out_and_is_then_auto(tmp_path):
    write(tmp_path / "robotarium.toml", '[experiment]\ntitle = "t"\ndescription = "d"\n'
                                        'robots = 2\nmain = "main.py"\n')
    write(tmp_path / "main.py", "")
    assert load_project(tmp_path).experiment.duration == "auto"


def test_measured_is_a_duration(tmp_path):
    assert errors(experiment(tmp_path, duration="measured").check()) == []


# ---------------------------------------------------------------------------
# --set: one manifest, several variants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("gain=0.5", ("gain", 0.5)),
    ("controller=baseline", ("controller", "baseline")),
    ('label="12"', ("label", "12")),
    ("steps=12", ("steps", 12)),
    ("leader=true", ("leader", True)),
    ("offsets=[0, 5, -5]", ("offsets", [0, 5, -5])),
    ("gains={ alpha = 2 }", ("gains", {"alpha": 2})),
    ("gains.alpha=1.5", ("gains.alpha", 1.5)),
    (" name = two words ", ("name", "two words")),
])
def test_a_setting_is_read_as_toml_or_else_as_text(given, expected):
    assert parse_setting(given) == expected


@pytest.mark.parametrize("given", ["controller", "=1", "a..b=1", "a b=1"])
def test_what_is_not_a_setting_is_refused(given):
    with pytest.raises(ValueError, match="--set"):
        parse_setting(given)


def test_settings_apply_over_the_config_and_nest(tmp_path):
    original = {"controller": "sheaf", "gains": {"alpha": 1.0, "beta": 2.0}}
    found = experiment(tmp_path, config=original)
    found.set_config(["controller=baseline", "gains.alpha=3", "extra.depth=1"])
    assert found.load_config() == {"controller": "baseline", "gains": {"alpha": 3, "beta": 2.0},
                                   "extra": {"depth": 1}}
    assert original == {"controller": "sheaf", "gains": {"alpha": 1.0, "beta": 2.0}}
    notes = found.check()
    assert any(each.level == INFO and "controller='baseline' (the config says 'sheaf')"
               in each.message for each in notes)
    assert any(each.level == WARNING and "adds extra.depth" in each.message for each in notes)


def test_a_setting_cannot_reach_inside_a_value(tmp_path):
    found = experiment(tmp_path, config={"gain": 1.0})
    with pytest.raises(ValueError, match="gain is 1.0, not a table"):
        found.set_config(["gain.x=1"])


def test_a_config_file_takes_settings_too(tmp_path):
    write(tmp_path / "params.toml", "gain = 1\n")
    found = experiment(tmp_path, config="params.toml")
    found.set_config({"gain": 2})
    assert found.load_config() == {"gain": 2}
    assert (tmp_path / "params.toml").read_text() == "gain = 1\n"


def test_settings_without_a_config_make_one(tmp_path):
    found = experiment(tmp_path)
    found.set_config(["controller=baseline"])
    assert found.load_config() == {"controller": "baseline"}


def test_a_setting_keeps_the_type_of_the_value_it_replaces(tmp_path):
    original = {"label": "sheaf", "alpha": 1.5, "show": True, "steps": 10, "when": "today"}
    found = experiment(tmp_path, config=original)
    found.set_config(["label=12", "alpha=2", "show=False", "steps=12", "when=2026-11-03"])
    config = found.load_config()
    assert config == {"label": "12", "alpha": 2.0, "show": False, "steps": 12,
                      "when": "2026-11-03"}
    assert isinstance(config["alpha"], float) and config["show"] is False
    found = experiment(tmp_path, config=original)
    found.set_config(["label=nan", "label=true", 'label="quoted"'])
    assert found.load_config()["label"] == "quoted"
    found = experiment(tmp_path, config=original)
    found.set_config(["label=nan"])
    assert found.load_config()["label"] == "nan"
    assert not any(each.level == WARNING for each in found.check())


def test_a_setting_that_changes_a_values_type_is_warned_about(tmp_path):
    found = experiment(tmp_path, config={"show": True, "gain": 1})
    found.set_config(["show=maybe", "gain=0.5"])
    warnings = [each.message for each in found.check() if each.level == WARNING]
    assert len(warnings) == 1
    assert "replaces the config's bool True with a string" in warnings[0]


def test_the_title_can_name_a_config_value(tmp_path):
    found = experiment(tmp_path, title="Arrow {config.controller} a={config.gains.alpha}",
                       config={"controller": "sheaf", "gains": {"alpha": 1.5}})
    found.set_config(["controller=baseline"])
    assert found.render_title(git="") == "Arrow baseline a=1.5"
    missing = experiment(tmp_path, title="Arrow {config.nope}", config={"a": 1})
    assert any("{config.KEY}" in each.message for each in errors(missing.check()))


# ---------------------------------------------------------------------------
# [julia] startup and version
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("measured", "measured"), (None, "measured"), (240, 240), ("4m", 240), ("90s", 90),
])
def test_the_julia_startup_is_measured_or_seconds(tmp_path, given, expected):
    found = experiment(tmp_path, julia_startup=given)
    assert found.julia_startup == expected
    assert errors(found.check()) == []


@pytest.mark.parametrize("given", ["auto", "soon", 601, -1, True])
def test_a_julia_startup_that_is_not_one_is_refused(tmp_path, given):
    found = errors(experiment(tmp_path, julia_startup=given).check())
    assert any("[julia] startup" in each.message for each in found)


def test_the_julia_startup_is_read_from_the_manifest(tmp_path):
    write(tmp_path / "robotarium.toml", MANIFEST.replace('version = "1.11"',
                                                         'version = "1.11"\nstartup = "4m"'))
    write(tmp_path / "ABOUT.md", "Three robots.")
    assert load_project(tmp_path).experiment.julia_startup == 240


@pytest.mark.parametrize("version, expected", [
    ("1.11.9", "\"=1.11.9\""), ("1.11", "an exact release"), ("^1.11.9", "\"=1.11.9\""),
])
def test_a_julia_version_that_is_not_a_pin_is_warned_about(tmp_path, version, expected):
    found = [each for each in experiment(tmp_path, julia_version=version).check()
             if each.level == WARNING]
    assert len(found) == 1 and "caret specifier" in found[0].message
    assert expected in found[0].message and "below 2" in found[0].message


def test_an_exact_julia_pin_is_not_warned_about(tmp_path):
    found = experiment(tmp_path, julia_version="=1.11.9").check()
    assert [each for each in found if each.level == WARNING] == []
    # Pkg reads a hyphen range from a release to itself as that release.
    found = experiment(tmp_path, julia_version="1.11.9 - 1.11.9").check()
    assert [each for each in found if each.level == WARNING] == []
    ranged = experiment(tmp_path, julia_version="1.10 - 1.11").check()
    assert any("exact pin" in each.message for each in ranged if each.level == WARNING)


def test_a_short_equals_pin_is_said_to_mean_the_dot_zero_release(tmp_path):
    found = [each for each in experiment(tmp_path, julia_version="=1.11").check()
             if each.level == WARNING]
    assert len(found) == 1
    assert "pins Julia 1.11.0 exactly" in found[0].message
    assert "more than one" not in found[0].message


def test_a_julia_package_listed_twice_is_refused(tmp_path):
    uuid = "0f8b85d8-7281-11e9-16c2-39a750bddbf1"
    found = experiment(tmp_path, julia_packages=[
        {"name": "JSON3", "uuid": uuid, "version": "1"}, {"name": "JSON3", "uuid": uuid}])
    assert any("JSON3 is listed 2 times" in each.message for each in errors(found.check()))


# ---------------------------------------------------------------------------
# [simulator] recording keys
# ---------------------------------------------------------------------------

def test_the_simulators_recording_keys_are_read(tmp_path):
    write(tmp_path / "robotarium.toml", MANIFEST.replace(
        'fast = false', 'fast = false\nseed = 7\nvideo = "videos/run.mp4"\nfps = 15\nevery = 2\n'
                        'dpi = 150\nframes = [0, 14.5, "end"]\nframes_format = "pdf"'))
    write(tmp_path / "ABOUT.md", "Three robots.")
    project = load_project(tmp_path)
    assert project.simulator_seed == 7
    assert project.recording == {"video": tmp_path / "videos" / "run.mp4", "fps": 15,
                                 "every": 2, "dpi": 150, "frames": [0, 14.5, "end"],
                                 "frames_format": "pdf"}


# ---------------------------------------------------------------------------
# files from outside the project directory
# ---------------------------------------------------------------------------

def test_a_shared_file_from_outside_the_project_is_uploaded_flat(tmp_path):
    write(tmp_path / "common" / "overlay.py", "")
    write(tmp_path / "common" / "sheafcore.jl", "")
    root = tmp_path / "d1"
    found = experiment(root, files=["../common/*.py", "../common/sheafcore.jl"])
    assert [path.name for path in found.upload_files()] == ["main.py", "overlay.py",
                                                            "sheafcore.jl"]
    notes = [each for each in found.check() if each.level == INFO]
    assert any("outside the project directory (../common/overlay.py)" in each.message
               for each in notes)
    assert errors(found.check()) == []


def test_two_shared_files_with_one_base_name_collide(tmp_path):
    write(tmp_path / "a" / "x.py", "")
    write(tmp_path / "b" / "x.py", "")
    found = errors(experiment(tmp_path / "exp", files=["../a/x.py", "../b/x.py"]).check())
    assert any("2 files would be uploaded as x.py" in each.message for each in found)


def test_a_file_outside_the_git_work_tree_is_warned_about(tmp_path):
    import shutil
    import subprocess
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "repo" / "exp"
    write(tmp_path / "shared" / "outside.py", "")
    write(tmp_path / "repo" / "common" / "inside.py", "")
    write(root / "main.py", "import rps.robotarium\n")
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    found = experiment(root, files=["../../shared/outside.py", "../common/inside.py"]).check()
    warned = [each for each in found if each.level == WARNING]
    assert [each.file for each in warned] == ["outside.py"]
    assert "outside the git work tree" in warned[0].message
