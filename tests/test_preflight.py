"""Static checks over a bundle's files: what would fail on the testbed, found here."""

import pytest

from robotarium import libraries
from robotarium.bundle import build
from robotarium.experiment import Experiment
from robotarium.findings import ERROR, INFO, WARNING
from robotarium.preflight import check, julia_packages_used

from conftest import write

GOOD_MAIN = """\
import numpy as np
import rps.robotarium as robotarium

N = 3
r = robotarium.Robotarium(number_of_robots=N, show_figure=False)
r.debug()
"""

JSON3 = {"name": "JSON3", "uuid": "0f8b85d8-7281-11e9-16c2-39a750bddbf1", "version": "1"}


def findings_for(tmp_path, main=GOOD_MAIN, files=None, **changes):
    write(tmp_path / "main.py", main)
    for name, text in (files or {}).items():
        write(tmp_path / name, text)
    fields = dict(title="T", description="D", duration=60, robots=3, main="main.py",
                  files=[name for name in (files or {}) if "/" not in name], root=tmp_path)
    fields.update(changes)
    experiment = Experiment(**fields)
    bundle = build(experiment, tmp_path / ".robotarium")
    return check(bundle.files, experiment, libraries.load())


def messages(findings, level):
    return " | ".join(each.message for each in findings if each.level == level)


def test_a_sound_main_file_passes(tmp_path):
    assert messages(findings_for(tmp_path), ERROR) == ""
    assert messages(findings_for(tmp_path), WARNING) == ""


def test_a_local_module_left_out_is_an_error(tmp_path):
    write(tmp_path / "helpers.py", "")
    found = findings_for(tmp_path, GOOD_MAIN + "import helpers\n")
    assert "helpers is imported but not uploaded" in messages(found, ERROR)


def test_an_uploaded_module_is_fine(tmp_path):
    found = findings_for(tmp_path, GOOD_MAIN + "import helpers\n", files={"helpers.py": ""})
    assert messages(found, ERROR) == ""


def test_a_package_directory_cannot_travel(tmp_path):
    write(tmp_path / "mypkg" / "__init__.py", "")
    found = findings_for(tmp_path, GOOD_MAIN + "from mypkg import thing\n")
    assert "package directory" in messages(found, ERROR)


def test_a_relative_import_is_an_error(tmp_path):
    found = findings_for(tmp_path, GOOD_MAIN + "from .helpers import f\n",
                         files={"helpers.py": ""})
    assert "relative import" in messages(found, ERROR)


def test_an_import_the_testbed_lacks_is_warned_about(tmp_path):
    found = findings_for(tmp_path, GOOD_MAIN + "import pandas\nimport scipy\nimport cv2\n")
    assert "pandas is not in the Robotarium's libraries.txt" in messages(found, WARNING)
    assert "scipy" not in messages(found, WARNING) and "cv2" not in messages(found, WARNING)


def test_allow_imports_silences_it(tmp_path):
    found = findings_for(tmp_path, GOOD_MAIN + "import pandas\n", allow_imports=["pandas"])
    assert messages(found, WARNING) == ""


def test_a_guarded_import_may_fail(tmp_path):
    found = findings_for(tmp_path, GOOD_MAIN + "try:\n    import pandas\nexcept ImportError:\n"
                                               "    pandas = None\n")
    assert messages(found, WARNING) == ""


def test_this_package_is_not_on_the_testbed(tmp_path):
    found = findings_for(tmp_path, GOOD_MAIN + "import robotarium\n")
    assert "exists only on this machine" in messages(found, ERROR)


def test_the_runtime_when_turned_off_is_missing(tmp_path):
    found = findings_for(tmp_path, GOOD_MAIN + "import robotarium_runtime\n", runtime=False)
    assert "robotarium_runtime is imported but not in the bundle" in messages(found, ERROR)


@pytest.mark.parametrize("main", [
    GOOD_MAIN.replace("number_of_robots=N", "number_of_robots=5"),
    GOOD_MAIN.replace("N = 3", "N = 7"),
])
def test_a_robot_count_that_disagrees_with_the_form_is_an_error(tmp_path, main):
    assert "but the form would say 3 robots" in messages(findings_for(tmp_path, main), ERROR)


def test_a_robot_count_that_cannot_be_known_is_left_alone(tmp_path):
    main = GOOD_MAIN.replace("N = 3", "N = int('3')")
    assert messages(findings_for(tmp_path, main), ERROR) == ""


def test_a_missing_end_call_is_warned_about(tmp_path):
    found = findings_for(tmp_path, GOOD_MAIN.replace("r.debug()", ""))
    assert "r.debug()" in messages(found, WARNING)


def test_a_syntax_error_is_an_error(tmp_path):
    found = findings_for(tmp_path, GOOD_MAIN + "def broken(:\n")
    assert any(each.level == ERROR and each.line == 7 for each in found)


def test_numpy_newer_than_the_executors_is_warned_about(tmp_path):
    found = findings_for(tmp_path, GOOD_MAIN + "y = np.reshape(x, shape=(2, 1))\n")
    assert "numpy 2.1" in messages(found, WARNING)


# ---------------------------------------------------------------------------
# Julia
# ---------------------------------------------------------------------------

LOADER = GOOD_MAIN + "from robotarium_runtime import load_julia_backend\n"


def test_an_undeclared_julia_package_is_an_error(tmp_path):
    found = findings_for(tmp_path, LOADER, files={"backend.jl": "module B\nusing JSON3\nend\n"})
    assert "backend.jl loads JSON3, which nothing installs" in messages(found, ERROR)


def test_a_declared_julia_package_and_the_stdlib_pass(tmp_path):
    found = findings_for(tmp_path, LOADER,
                         files={"backend.jl": "module B\nusing JSON3, LinearAlgebra\n"
                                              "import Random: rand\nend\n"},
                         julia_packages=[JSON3])
    assert messages(found, ERROR) == "" and messages(found, WARNING) == ""


def test_packages_declared_in_code_soften_it_to_a_warning(tmp_path):
    main = GOOD_MAIN + ("from robotarium_runtime import load_julia_backend\n"
                        "load_julia_backend('backend.jl', packages=[('JSON3', 'x', None)])\n")
    found = findings_for(tmp_path, main, files={"backend.jl": "using JSON3\n"})
    assert messages(found, ERROR) == ""
    assert "JSON3" in messages(found, WARNING)


def test_an_included_file_must_be_uploaded_and_by_base_name(tmp_path):
    found = findings_for(tmp_path, LOADER, files={
        "backend.jl": 'include("ops.jl")\ninclude("lib/util.jl")\n', "lib/util.jl": ""})
    assert "ops.jl is not uploaded" in messages(found, ERROR)
    assert "util.jl is not uploaded" in messages(found, ERROR)


def test_a_julia_file_nothing_loads_is_warned_about(tmp_path):
    found = findings_for(tmp_path, files={"backend.jl": "module B\nend\n"})
    assert "no Python file imports juliacall or robotarium_runtime" in messages(found, WARNING)


def test_a_declared_but_unused_package_is_noted(tmp_path):
    found = findings_for(tmp_path, LOADER, files={"backend.jl": "module B\nend\n"},
                         julia_packages=[JSON3])
    assert "no uploaded .jl file loads it" in messages(found, INFO)


@pytest.mark.parametrize("source, expected", [
    ("using LinearAlgebra; using JSON3", ["LinearAlgebra", "JSON3"]),
    ("using CSV,\n    DataFrames\n", ["CSV", "DataFrames"]),
    ("import JSON3\tas J", ["JSON3"]),
    ("import Foo.Bar: baz, qux", ["Foo"]),
    ("using A: f,\n    g\nusing B", ["A", "B"]),
    ("x = 1  # using Trailing\n", []),
    ('@warn "No GPU found; using CPU instead"\nusing JSON3', ["JSON3"]),
    ('pyexec("import numpy as np; import scipy.linalg as sl", Main)', []),
    ('const LABEL = "robot, #"\nusing JSON3', ["JSON3"]),
    ("using CSV,\n\n    # a comment\n    DataFrames", ["CSV", "DataFrames"]),
    ("@everywhere using Distributed", ["Distributed"]),
    ("using Ünïcode", ["Ünïcode"]),
    ("x = A'; using B", ["B"]),
    ("c = ';'; using D", ["D"]),
    ('s = """\nusing NotReal; x\n"""\nusing E', ["E"]),
    ("#= outer #= inner =# using F =#\nusing G", ["G"]),
])
def test_julia_statement_forms_are_parsed(source, expected):
    assert julia_packages_used(source) == expected


def test_a_second_using_on_a_line_is_not_missed(tmp_path):
    found = findings_for(tmp_path, LOADER,
                         files={"backend.jl": "using LinearAlgebra; using JSON3\n"})
    assert "loads JSON3" in messages(found, ERROR)
    assert "LinearAlgebra" not in messages(found, ERROR)


def test_julia_usings_are_parsed():
    source = """
    module SheafBackend
    using CellularSheaves
    using JSON3, LinearAlgebra
    import Statistics: mean, std
    using .Inner
    #= using Commented =#
    # using AlsoCommented
    end
    """
    assert julia_packages_used(source) == ["CellularSheaves", "JSON3", "LinearAlgebra",
                                           "Statistics"]


def test_an_rps_folder_beside_the_main_file_is_not_a_problem(tmp_path):
    write(tmp_path / "rps" / "__init__.py", "")
    assert messages(findings_for(tmp_path), ERROR) == ""


def test_the_stdlib_is_the_testbeds(tmp_path):
    found = findings_for(tmp_path, GOOD_MAIN + "import tomllib\nimport imp\nimport distutils\n")
    assert "tomllib is not in the standard library of Python 3.10" in messages(found, WARNING)
    assert "imp" not in messages(found, WARNING).replace("import", "")
    assert "distutils" not in messages(found, WARNING)


def test_skipping_initialization_is_warned_about(tmp_path):
    main = GOOD_MAIN.replace("show_figure=False", "show_figure=False, skip_initialization=True")
    assert "skip_initialization" in messages(findings_for(tmp_path, main), WARNING)


def test_a_version_guarded_import_is_optional(tmp_path):
    main = GOOD_MAIN + ("import sys\nif sys.version_info >= (3, 11):\n    import tomllib\n"
                        "else:\n    import tomli as tomllib\n")
    assert messages(findings_for(tmp_path, main), WARNING) == ""
