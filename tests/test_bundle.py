"""The bundle: flat, with what the form cannot carry turned into Python."""

import ast
import datetime
import importlib.util
import math

import pytest

from robotarium.bundle import BundleError, build, python_literal
from robotarium.experiment import Experiment

from conftest import write


def experiment(tmp_path, main_source="import rps.robotarium\n", **changes):
    write(tmp_path / "main.py", main_source)
    fields = dict(title="T ({timestamp})", description="D", duration=60, robots=2,
                  main="main.py", root=tmp_path)
    fields.update(changes)
    return Experiment(**fields)


def test_the_files_land_flat_with_the_main_file_first(tmp_path):
    write(tmp_path / "lib" / "helpers.py", "")
    write(tmp_path / "backend.jl", "")
    when = datetime.datetime(2026, 10, 21, 9, 5)
    bundle = build(experiment(tmp_path, files=["lib/helpers.py", "backend.jl"]),
                   tmp_path / ".robotarium", when=when)
    assert bundle.directory == tmp_path / ".robotarium" / "bundle"
    assert [path.name for path in bundle.files] == ["main.py", "helpers.py", "backend.jl"]
    assert all(path.parent == bundle.directory for path in bundle.files)
    assert bundle.title == "T (20261021T0905)"
    assert bundle.generated == []


def test_the_runtime_is_vendored_when_imported(tmp_path):
    bundle = build(experiment(tmp_path, "from robotarium_runtime import RunLogger\n"),
                   tmp_path / "out")
    assert bundle.generated == ["robotarium_runtime.py", "robotarium_bundle.py"]
    assert (bundle.directory / "robotarium_runtime.py").read_text() == \
        open(importlib.util.find_spec("robotarium_runtime").origin).read()


def test_the_runtime_can_be_forced_off(tmp_path):
    bundle = build(experiment(tmp_path, "import robotarium_runtime\n", runtime=False),
                   tmp_path / "out")
    assert "robotarium_runtime.py" not in bundle.generated


def test_the_bundle_module_carries_the_record_config_and_julia(tmp_path):
    bundle = build(experiment(tmp_path, config={"gain": 2.5, "names": ["a", "b"]},
                              julia_packages=[{"name": "JSON3", "uuid":
                                               "0f8b85d8-7281-11e9-16c2-39a750bddbf1"}]),
                   tmp_path / "out")
    module = bundle.directory / "robotarium_bundle.py"
    namespace = {}
    exec(module.read_text(), namespace)
    assert namespace["CONFIG"] == {"gain": 2.5, "names": ["a", "b"]}
    assert namespace["JULIA"]["packages"][0]["name"] == "JSON3"
    assert namespace["EXPERIMENT"]["robots"] == 2
    assert namespace["EXPERIMENT"]["files"]["main.py"] == bundle.hashes()["main.py"]
    for node in ast.walk(ast.parse(module.read_text())):
        assert not isinstance(node, ast.Call), "the bundle module must be literals only"


def test_a_failed_build_leaves_the_last_good_bundle(tmp_path):
    good = build(experiment(tmp_path), tmp_path / "out")
    with pytest.raises(BundleError):
        build(experiment(tmp_path, robots=40), tmp_path / "out")
    assert (good.directory / "main.py").is_file()


@pytest.mark.parametrize("value", [math.nan, math.inf, datetime.date(2026, 1, 1), {1: "a"}])
def test_what_is_not_a_literal_is_refused_here(value):
    with pytest.raises(ValueError):
        python_literal({"x": value})


def test_literals_round_trip():
    value = {"a": [1, 2.5, None, True, "ü \"q\""], "b": {"c": []}, "d": [[1] * 40]}
    assert ast.literal_eval(python_literal(value)) == value
