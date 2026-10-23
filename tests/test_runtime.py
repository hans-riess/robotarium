"""robotarium_runtime: what travels to the testbed with the experiment."""

import importlib.abc
import importlib.util
import io
import json
import sys
import types

import numpy as np
import pytest

from robotarium.results import run_copies

from conftest import write


def test_a_run_is_recorded_and_read_back(tmp_path, fresh_runtime):
    log = fresh_runtime.RunLogger("demo", directory=tmp_path, meta={"seed": 3},
                                  save_at_exit=False, echo=False)
    for step in range(5):
        log.record(poses=np.full((3, 2), step), error=step * 0.5)
    log.note("halfway", step=2)
    path = log.save()

    run = fresh_runtime.load_run(path)
    assert run.data["poses"].shape == (5, 3, 2)
    assert run.data["poses"][4, 0, 0] == 4
    assert list(run.steps["error"]) == [0, 1, 2, 3, 4]
    assert run.meta["seed"] == 3 and run.meta["records"] == 5
    assert run.meta["notes"] == [{"step": 2, "seconds": 0.066, "message": "halfway"}]
    assert not path.with_name(path.name + ".partial").exists()


def test_a_changing_shape_is_refused(tmp_path, fresh_runtime):
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False)
    log.record(poses=np.zeros((3, 2)))
    with pytest.raises(ValueError, match="poses"):
        log.record(poses=np.zeros((3, 4)))


def test_autosave_writes_as_it_goes(tmp_path, fresh_runtime):
    log = fresh_runtime.RunLogger(directory=tmp_path, autosave_every=2, save_at_exit=False)
    log.record(x=1)
    assert not log.path.exists()
    log.record(x=2)
    assert fresh_runtime.load_run(log.path).data["x"].tolist() == [1, 2]


def test_the_log_directory_follows_the_environment(tmp_path, monkeypatch, fresh_runtime):
    monkeypatch.setenv("ROBOTARIUM_LOG_DIR", str(tmp_path / "runs"))
    log = fresh_runtime.RunLogger("x", save_at_exit=False)
    assert log.path.parent == tmp_path / "runs"


def test_the_bundle_module_is_read(tmp_path, monkeypatch, fresh_runtime):
    write(tmp_path / "robotarium_bundle.py",
          "EXPERIMENT = {'title': 'T'}\nCONFIG = {'gain': 2}\nJULIA = {'packages': []}\n")
    monkeypatch.setattr(fresh_runtime, "here", lambda: tmp_path)
    assert fresh_runtime.bundle()["experiment"] == {"title": "T"}
    assert fresh_runtime.config() == {"gain": 2}


def test_outside_a_bundle_the_manifest_supplies_the_config(tmp_path, monkeypatch,
                                                           fresh_runtime):
    write(tmp_path / "robotarium.toml", "[experiment]\ntitle = 'x'\n\n[config]\ngain = 4\n")
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(fresh_runtime, "here", lambda: tmp_path / "src")
    assert fresh_runtime.config() == {"gain": 4}


def test_no_config_anywhere_gives_the_default(tmp_path, monkeypatch, fresh_runtime):
    monkeypatch.setattr(fresh_runtime, "here", lambda: tmp_path)
    assert fresh_runtime.config(default={"a": 1}) == {"a": 1}


# ---------------------------------------------------------------------------
# the Julia backend, against stand-ins for juliapkg and juliacall
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_julia(monkeypatch):
    """juliapkg and juliacall stand-ins that record the order things happen in."""
    events = []
    juliapkg = types.ModuleType("juliapkg")
    juliapkg.add = lambda name, uuid, version=None: events.append(("add", name, version))
    juliapkg.require_julia = lambda compat: events.append(("julia", compat))
    juliapkg.resolve = lambda: events.append(("resolve",))

    class Main:
        @staticmethod
        def include(path):
            events.append(("include", path))
            Main.Backend = types.SimpleNamespace(
                build=lambda payload: types.SimpleNamespace(
                    matrix=[[1, 2], [3, 4]], echoed=json.loads(payload)))

    class Juliacall(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        """Makes `import juliacall` a recorded event: that is when Julia starts."""

        def find_spec(self, name, path=None, target=None):
            return importlib.util.spec_from_loader(name, self) if name == "juliacall" else None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            events.append(("start",))
            module.Main = Main

    monkeypatch.setitem(sys.modules, "juliapkg", juliapkg)
    monkeypatch.delitem(sys.modules, "juliacall", raising=False)
    monkeypatch.setattr(sys, "meta_path", [Juliacall()] + sys.meta_path)
    return events


def test_packages_are_declared_before_julia_starts(tmp_path, monkeypatch, fake_julia,
                                                   fresh_runtime):
    write(tmp_path / "backend.jl", "module Backend\nusing JSON3\nend\n")
    monkeypatch.setattr(fresh_runtime, "here", lambda: tmp_path)
    backend = fresh_runtime.load_julia_backend(
        "backend.jl", packages=[("JSON3", "0f8b85d8-7281-11e9-16c2-39a750bddbf1", "1")],
        julia_version="1.11")
    assert fake_julia == [("julia", "1.11"), ("add", "JSON3", "1"), ("resolve",), ("start",),
                          ("include", str((tmp_path / "backend.jl").resolve()))]

    result = fresh_runtime.call_json(backend.build, {"n": np.int64(3), "x": np.ones(2)})
    assert result.echoed == {"n": 3, "x": [1.0, 1.0]}
    assert fresh_runtime.as_arrays(result, ["matrix"])["matrix"].dtype == float

    assert fresh_runtime.load_julia_backend("backend.jl") is backend


def test_the_bundles_packages_are_the_default(tmp_path, monkeypatch, fake_julia,
                                              fresh_runtime):
    write(tmp_path / "backend.jl", "module Backend\nend\n")
    write(tmp_path / "robotarium_bundle.py",
          "JULIA = {'version': None, 'packages': [{'name': 'JSON3', 'uuid': 'u', "
          "'version': None}]}\n")
    monkeypatch.setattr(fresh_runtime, "here", lambda: tmp_path)
    fresh_runtime.load_julia_backend("backend.jl")
    assert ("add", "JSON3", None) in fake_julia


def test_a_missing_backend_file_says_to_upload_it(tmp_path, monkeypatch, fresh_runtime):
    monkeypatch.setattr(fresh_runtime, "here", lambda: tmp_path)
    with pytest.raises(fresh_runtime.JuliaBackendError, match="Upload it"):
        fresh_runtime.load_julia_backend("backend.jl")


def test_the_module_name_is_read_from_the_source(fresh_runtime):
    assert fresh_runtime.julia_module_name("# c\nmodule SheafBackend\nend") == "SheafBackend"
    assert fresh_runtime.julia_module_name("f(x) = x") is None


def test_autosave_counts_records_not_step_numbers(tmp_path, fresh_runtime):
    log = fresh_runtime.RunLogger(directory=tmp_path, autosave_every=10, save_at_exit=False)
    saves = []
    real_save = log.save
    log.save = lambda: saves.append(1) or real_save()
    for step in range(0, 200, 2):
        log.record(step=step, x=step)
    assert len(saves) == 10
    run = fresh_runtime.load_run(log.path)
    assert run.meta["records"] == 100 and run.steps["x"][-1] == 198


def test_a_refused_record_keeps_nothing_of_it(tmp_path, fresh_runtime):
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False)
    log.record(a=np.zeros(2), b=np.zeros(3))
    with pytest.raises(ValueError, match="'b'"):
        log.record(a=np.ones(2), b=np.zeros(4))
    log.record(a=np.ones(2), b=np.ones(3))
    run = fresh_runtime.load_run(log.save())
    assert run.data["a"].shape == (2, 2) and run.data["b"].shape == (2, 3)
    assert list(run.steps["a"]) == list(run.steps["b"]) == [0, 1]


def test_a_note_belongs_to_the_last_recorded_step(tmp_path, fresh_runtime):
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False, echo=False)
    log.note("before anything")
    log.record(step=10, x=1)
    log.note("at ten")
    assert [note["step"] for note in log.meta["notes"]] == [0, 10]


def test_a_value_that_needs_pickling_is_refused(tmp_path, fresh_runtime):
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False)
    with pytest.raises(ValueError, match="'target'"):
        log.record(target=None, x=1.0)
    log.record(label="phase one", x=1.0)
    assert fresh_runtime.load_run(log.save()).data["label"][0] == "phase one"


def test_a_failed_write_leaves_no_partial_file(tmp_path, fresh_runtime, monkeypatch):
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False)
    log.record(x=1)

    def full(*arguments, **keywords):
        raise OSError("No space left on device")

    monkeypatch.setattr(fresh_runtime.np, "savez_compressed", full)
    with pytest.raises(OSError):
        log.save()
    assert list(tmp_path.iterdir()) == []



@pytest.mark.parametrize("value", [
    np.zeros(2, dtype=[("a", float), ("b", object)]),
    np.array([None, 1], dtype=object),
])
def test_anything_holding_objects_is_refused(tmp_path, fresh_runtime, value):
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False)
    with pytest.raises(ValueError, match="not numbers or text"):
        log.record(x=value)


@pytest.mark.parametrize("name", ["file", "allow_pickle", "__meta__", "__steps__x"])
def test_reserved_names_are_refused(tmp_path, fresh_runtime, name):
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False)
    with pytest.raises(ValueError, match="reserves"):
        log.record(**{name: 1.0})


def test_a_series_must_keep_a_type_it_can_be_stacked_in(tmp_path, fresh_runtime):
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False)
    log.record(when=np.datetime64("2026-10-21T15:00"))
    with pytest.raises(ValueError, match="cannot be kept as one array"):
        log.record(when=1.5)
    log.record(when=np.datetime64("2026-10-21T15:00:01"))
    assert fresh_runtime.load_run(log.save()).data["when"].shape == (2,)


def test_two_loggers_of_one_name_write_two_files(tmp_path, fresh_runtime):
    first = fresh_runtime.RunLogger("same", directory=tmp_path, save_at_exit=False)
    second = fresh_runtime.RunLogger("same", directory=tmp_path, save_at_exit=False)
    assert first.path != second.path


def test_each_write_is_also_printed_for_the_testbeds_log(tmp_path, fresh_runtime, capsys):
    log = fresh_runtime.RunLogger("demo", directory=tmp_path, autosave_every=2,
                                  save_at_exit=False, echo=False, stdout_copy=True)
    for step in range(3):
        print("step %d" % step)
        log.record(poses=np.full((3, 2), step))
    path = log.save()
    out = capsys.readouterr().out
    assert out.count("-----BEGIN ROBOTARIUM RUN %s " % path.name) == 2
    found, damaged = run_copies(out)
    assert found == {path.name: path.read_bytes()} and damaged == []


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_by_default_a_terminal_gets_no_copy(tmp_path, fresh_runtime, monkeypatch, capsys):
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False)
    log.record(x=1)
    log.save()
    assert "BEGIN ROBOTARIUM RUN" in capsys.readouterr().out
    terminal = Terminal()
    monkeypatch.setattr(sys, "stdout", terminal)
    log.save()
    assert terminal.getvalue() == ""


def test_a_write_that_fails_still_leaves_the_copy(tmp_path, fresh_runtime, monkeypatch,
                                                  capsys):
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False, stdout_copy=True)
    log.record(x=1)

    def read_only(*arguments):
        raise OSError("Read-only file system")

    monkeypatch.setattr(fresh_runtime.os, "replace", read_only)
    with pytest.raises(OSError):
        log.save()
    assert list(run_copies(capsys.readouterr().out)[0]) == [log.path.name]
    assert list(tmp_path.iterdir()) == []
