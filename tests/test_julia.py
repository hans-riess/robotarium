"""
Looking up a Julia package's [julia] entry in the General registry (a stand-in for it), and
timing a cold start of a backend (against stand-ins for juliapkg and juliacall; one real one
runs with ROBOTARIUM_JULIA_TESTS=1).
"""

import json
import os
import sys
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

import pytest

from robotarium import budget, julia
from robotarium.bundle import build
from robotarium.experiment import Experiment

from conftest import write


@pytest.fixture
def registry(tmp_path, monkeypatch):
    write(tmp_path / "registry" / "J" / "JSON3" / "Package.toml",
          'name = "JSON3"\nuuid = "0f8b85d8-7281-11e9-16c2-39a750bddbf1"\n'
          'repo = "https://github.com/quinnj/JSON3.jl.git"\n')
    write(tmp_path / "registry" / "J" / "JSON3" / "Versions.toml",
          '["1.9.0"]\ngit-tree-sha1 = "a"\n\n["1.14.3"]\ngit-tree-sha1 = "b"\n\n'
          '["1.15.0"]\ngit-tree-sha1 = "c"\nyanked = true\n\n["1.2.0"]\ngit-tree-sha1 = "d"\n')

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *arguments):
            pass

    server = HTTPServer(("127.0.0.1", 0),
                        partial(Quiet, directory=str(tmp_path / "registry")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ROBOTARIUM_JULIA_REGISTRY", "http://127.0.0.1:%d" % server.server_port)
    yield
    server.shutdown()
    server.server_close()


def test_the_latest_unyanked_version_is_found(registry):
    package = julia.lookup("JSON3")
    assert package.uuid == "0f8b85d8-7281-11e9-16c2-39a750bddbf1"
    assert package.version == "1.14.3"
    assert julia.toml_entry(package) == ('{ name = "JSON3", uuid = '
                                         '"0f8b85d8-7281-11e9-16c2-39a750bddbf1", '
                                         'version = "1.14.3" },')


def test_an_unknown_package_is_named(registry):
    with pytest.raises(julia.JuliaToolError, match="not in the General registry"):
        julia.lookup("NoSuchPackage")


# ---------------------------------------------------------------------------
# a cold start, against stand-ins for juliapkg and juliacall
# ---------------------------------------------------------------------------

JSON3 = {"name": "JSON3", "uuid": "0f8b85d8-7281-11e9-16c2-39a750bddbf1", "version": "1"}

#: What juliapkg 0.1.24 and Pkg print, cut down; it also notes the environment it was given.
STAND_IN_JULIAPKG = r'''
import json, os, time
__version__ = "0.1.24"
_added = []
def require_julia(compat):
    _added.append(("julia", compat))
def add(name, uuid, version=None):
    _added.append((name, uuid, version))
def resolve():
    note = os.environ.get("STAND_IN_NOTE")
    if note:
        keys = ("JULIA_DEPOT_PATH", "PYTHON_JULIAPKG_PROJECT", "JULIAUP_DEPOT_PATH",
                "PYTHON_JULIAPKG_EXE", "JULIA_NUM_PRECOMPILE_TASKS", "JULIA_PKG_SERVER",
                "HTTPS_PROXY", "PATH")
        with open(note, "w") as handle:
            json.dump({key: os.environ.get(key) for key in keys}, handle)
    print("[juliapkg] Locating Julia =1.11.9")
    time.sleep(0.05)
    print("[juliapkg] Using Julia 1.11.9 at /stand-in/bin/julia")
    print("[juliapkg] Installing packages:")
    print("[juliapkg]   | import Pkg")
    time.sleep(0.05)
    print("    Updating registry at `~/.julia/registries/General`")
    print("\x1b[32m\x1b[1mPrecompiling\x1b[22m\x1b[39m project...")
    time.sleep(0.05)
    print("  %d dependencies successfully precompiled in 3 seconds. 2 already precompiled."
          % (10 + len(_added)))
'''

STAND_IN_STATE = r'''
import os
STATE = {"meta": os.environ.get("STAND_IN_META", "/nowhere/meta.json"),
         "install": "/nowhere/install"}
'''

STAND_IN_JULIACALL = r'''
import types
__version__ = "0.9.35"
class _Main:
    def include(self, path):
        self.Backend = types.SimpleNamespace(probe=lambda payload: "probed " + payload)
    def names(self, module):
        return ["Backend", "probe"]
    def seval(self, code):
        return "1.11.9"
Main = _Main()
'''


@pytest.fixture
def stand_ins(tmp_path, monkeypatch):
    root = tmp_path / "stand-ins"
    write(root / "juliapkg" / "__init__.py", STAND_IN_JULIAPKG)
    write(root / "juliapkg" / "state.py", STAND_IN_STATE)
    write(root / "juliacall" / "__init__.py", STAND_IN_JULIACALL)
    monkeypatch.setenv("PYTHONPATH", str(root))
    monkeypatch.setenv("STAND_IN_NOTE", str(tmp_path / "environment.json"))
    return root


def julia_project(tmp_path, packages=(JSON3,)):
    root = tmp_path / "exp"
    write(root / "main.py", "import robotarium_runtime\nimport rps.robotarium\n")
    write(root / "backend.jl", "module Backend\nusing JSON3\nend\n")
    experiment = Experiment(title="T", description="D", duration="auto", robots=1,
                            main="main.py", files=["backend.jl"], root=root,
                            julia_packages=list(packages), julia_version="=1.11.9")
    return experiment, build(experiment, root / ".robotarium")


def test_a_cold_start_is_timed_and_saved(tmp_path, stand_ins, monkeypatch):
    monkeypatch.setenv("JULIA_PKG_SERVER", "")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("JULIA_DEPOT_PATH", "/home/someone/.julia")
    experiment, bundle = julia_project(tmp_path)
    output = tmp_path / "exp" / ".robotarium"
    scratch = tmp_path / "scratch"
    record = julia.cold_check(bundle, experiment, output, calls=[("probe", {"n": 1})],
                              scratch=scratch, cpus=2, echo=False)

    saved = json.loads((output / "julia" / "cold-start.json").read_text())
    assert saved["key"] == record["key"] == \
        budget.project_key(experiment, saved["juliacall"], saved["juliapkg"])
    phases = saved["phases"]
    assert phases["julia"] > 0 and phases["packages"] > 0 and phases["precompile"] >= 0
    assert phases["precompiled"] == 12 and phases["precompile_reported"] == 3.0
    assert list(phases["first_calls"]) == list(phases["warm_calls"]) == ["probe"]
    assert saved["julia_version"] == "1.11.9" and saved["precompile_tasks"] == 2
    assert 0 < saved["startup_seconds"] <= saved["total_seconds"]
    assert record["names"] == ["Backend", "probe"]
    assert not scratch.exists()

    seen = json.loads((tmp_path / "environment.json").read_text())
    assert seen["JULIA_DEPOT_PATH"] == str(scratch.resolve() / "depot")
    assert seen["PYTHON_JULIAPKG_PROJECT"] == str(scratch.resolve() / "project")
    assert seen["JULIAUP_DEPOT_PATH"] == str(scratch.resolve() / "juliaup")
    assert seen["JULIA_NUM_PRECOMPILE_TASKS"] == "2" and seen["PYTHON_JULIAPKG_EXE"] is None
    assert seen["JULIA_PKG_SERVER"] == "" and seen["HTTPS_PROXY"] == "http://proxy.example:3128"

    table = julia.describe_cold_start(record)
    assert "precompiling" in table and "(12 packages; Pkg says 3 s)" in table
    assert "first probe()" in table

    # The budget now counts it, half again.
    from robotarium.experiment import Project
    estimate = budget.startup_estimate(Project(experiment=experiment, output_dir=output), bundle)
    assert estimate.source == "cold-start"
    assert estimate.seconds == pytest.approx(1.5 * saved["startup_seconds"])


def test_the_key_is_stable_and_follows_the_packages(tmp_path, stand_ins):
    experiment, bundle = julia_project(tmp_path)
    output = tmp_path / "exp" / ".robotarium"
    first = julia.cold_check(bundle, experiment, output, echo=False)["key"]
    assert julia.cold_check(bundle, experiment, output, echo=False)["key"] == first
    graphs = {"name": "Graphs", "uuid": "86223c79-3864-5bf0-83f7-82e725a168b6", "version": "1"}
    experiment, bundle = julia_project(tmp_path / "more", packages=(JSON3, graphs))
    assert julia.cold_check(bundle, experiment, output, echo=False)["key"] != first


def test_a_scratch_directory_must_be_empty(tmp_path, stand_ins):
    experiment, bundle = julia_project(tmp_path)
    write(tmp_path / "used" / "depot" / "x", "")
    with pytest.raises(julia.JuliaToolError, match="not empty"):
        julia.cold_check(bundle, experiment, tmp_path / "out", scratch=tmp_path / "used",
                         echo=False)


def test_a_kept_scratch_directory_stays(tmp_path, stand_ins):
    experiment, bundle = julia_project(tmp_path)
    record = julia.cold_check(bundle, experiment, tmp_path / "out", scratch=tmp_path / "s",
                              keep_scratch=True, echo=False)
    assert record["scratch"] == str((tmp_path / "s").resolve()) and (tmp_path / "s").is_dir()


def test_a_cold_environment_hides_the_installed_julia(tmp_path):
    for name in ("julia", "juliaup"):
        write(tmp_path / ("bin-%s" % name) / name, "#!/bin/sh\n").chmod(0o755)
    write(tmp_path / "other" / "tool", "")
    base = {"PATH": os.pathsep.join([str(tmp_path / "bin-julia"), str(tmp_path / "other"),
                                     str(tmp_path / "bin-juliaup")]),
            "JULIA_DEPOT_PATH": "/home/someone/.julia", "PYTHON_JULIAPKG_EXE": "/x/julia",
            "JULIA_PKG_USE_CLI_GIT": "true", "ROBOTARIUM_PASSWORD": "secret",
            "SSL_CERT_FILE": "/etc/ca.pem"}
    environment = julia.cold_environment(tmp_path / "scratch", base=base)
    assert environment["PATH"] == str(tmp_path / "other")
    assert environment["JULIA_DEPOT_PATH"] == str((tmp_path / "scratch" / "depot").resolve())
    assert "PYTHON_JULIAPKG_EXE" not in environment and "ROBOTARIUM_PASSWORD" not in environment
    assert environment["JULIA_PKG_USE_CLI_GIT"] == "true"
    assert environment["SSL_CERT_FILE"] == "/etc/ca.pem"
    assert "JULIA_NUM_PRECOMPILE_TASKS" not in environment


def test_keep_julia_points_juliapkg_at_the_installed_one(tmp_path, stand_ins):
    julia_exe = write(tmp_path / "install" / "bin" / "julia", "#!/bin/sh\n")
    base = dict(os.environ, PYTHON_JULIAPKG_EXE=str(julia_exe))
    environment = julia.cold_environment(tmp_path / "scratch", keep_julia=True, base=base)
    assert environment["PYTHON_JULIAPKG_EXE"] == str(julia_exe)

    # Without the variable: the one juliapkg's project recorded when it installed it.
    write(tmp_path / "meta.json", json.dumps({"executable": str(julia_exe)}))
    base = {key: value for key, value in os.environ.items() if key != "PYTHON_JULIAPKG_EXE"}
    base["STAND_IN_META"] = str(tmp_path / "meta.json")
    environment = julia.cold_environment(tmp_path / "scratch", keep_julia=True, base=base)
    assert environment["PYTHON_JULIAPKG_EXE"] == str(julia_exe)

    base["STAND_IN_META"] = str(tmp_path / "missing.json")
    base["PATH"] = str(tmp_path / "empty")
    with pytest.raises(julia.JuliaToolError, match="found no Julia"):
        julia.cold_environment(tmp_path / "scratch", keep_julia=True, base=base,
                               python=sys.executable)


def test_the_phases_are_read_from_juliapkgs_and_pkgs_lines():
    transcript = [
        (0.4, "[juliapkg] Found dependencies: /x/juliapkg.json"),
        (0.5, "[juliapkg] Locating Julia ^1.10.3, =1.11.9"),
        (0.6, "[juliapkg] WARNING: About to install Julia 1.11.9 to /p/install."),
        (35.5, "[juliapkg] Using Julia 1.11.9 at /p/install/bin/julia"),
        (36.0, "[juliapkg] Installing packages:"),
        (36.0, "[juliapkg]   | Pkg.precompile()"),
        (130.0, "   Installed JSON3 ─── v1.14.3"),
        (156.0, "Precompiling project..."),
        (402.5, "  157 dependencies successfully precompiled in 246 seconds. 22 already "
                "precompiled."),
    ]
    assert julia.phases(transcript) == {"julia": 35.0, "packages": 120.0, "precompile": 246.5,
                                        "precompiled": 157, "precompile_reported": 246.0}
    assert julia.phases([(1.0, "nothing to see")])["julia"] is None


@pytest.mark.parametrize("given, expected", [
    ("probe", ("probe", {})), ('build={"robots": 6}', ("build", {"robots": 6})),
    ("step!=[1, 2]", ("step!", [1, 2])),
])
def test_a_call_names_a_function_and_its_json(given, expected):
    assert julia.parse_call(given) == expected


@pytest.mark.parametrize("given", ["", "1x", "f={bad", "a.b"])
def test_what_is_not_a_call_is_refused(given):
    with pytest.raises(julia.JuliaToolError):
        julia.parse_call(given)


@pytest.mark.julia
@pytest.mark.skipif(os.environ.get("ROBOTARIUM_JULIA_TESTS") != "1",
                    reason="installs Julia and JSON3 from nothing; set ROBOTARIUM_JULIA_TESTS=1")
def test_a_real_cold_start_with_json3(tmp_path):
    root = tmp_path / "exp"
    write(root / "main.py", "import robotarium_runtime\nimport rps.robotarium\n")
    write(root / "backend.jl", "module Backend\nusing JSON3\nexport echo\n"
                               "echo(text::AbstractString) = JSON3.write(JSON3.read(text))\nend\n")
    experiment = Experiment(title="T", description="D", duration="auto", robots=1,
                            main="main.py", files=["backend.jl"], root=root,
                            julia_packages=[JSON3], julia_version="=1.11.9")
    bundle = build(experiment, root / ".robotarium")
    record = julia.cold_check(bundle, experiment, root / ".robotarium",
                              calls=[("echo", {"a": 1})], keep_julia=True, echo=True)
    assert record["phases"]["packages"] and record["startup_seconds"] > 0
    assert (root / ".robotarium" / "julia" / "cold-start.json").is_file()
