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
    wall = run.meta["notes"][0].pop("wall")
    assert run.meta["notes"] == [{"step": 2, "seconds": 0.066, "message": "halfway"}]
    assert isinstance(wall, float) and wall >= 0
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


# ---------------------------------------------------------------------------
# RunLogger: float32, copy_every, wall time
# ---------------------------------------------------------------------------

def test_dtype_keeps_floats_in_float32_and_leaves_the_rest(tmp_path, fresh_runtime):
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False, dtype="float32")
    log.record(poses=np.ones((3, 2)), step_count=np.int64(4), label="a", flag=True)
    run = fresh_runtime.load_run(log.save())
    assert run.data["poses"].dtype == np.float32
    assert run.data["step_count"].dtype == np.int64
    assert run.data["flag"].dtype == bool and run.data["label"][0] == "a"


def test_dtype_must_be_a_floating_type(tmp_path, fresh_runtime):
    with pytest.raises(ValueError, match="floating"):
        fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False, dtype="int32")


def test_copy_every_prints_every_nth_write_and_the_last(tmp_path, fresh_runtime, capsys):
    log = fresh_runtime.RunLogger("demo", directory=tmp_path, autosave_every=1, copy_every=3,
                                  save_at_exit=False, echo=False, stdout_copy=True)
    for step in range(7):
        log.record(x=step)
    begin = "-----BEGIN ROBOTARIUM RUN %s " % log.path.name
    assert capsys.readouterr().out.count(begin) == 2          # writes 3 and 6
    log._save_if_dirty()                                       # at exit: write 7 is unprinted
    out = capsys.readouterr().out
    assert out.count(begin) == 1
    found, _ = run_copies(out)
    assert fresh_runtime.load_run(io.BytesIO(found[log.path.name])).data["x"].tolist() == \
        list(range(7))
    log._save_if_dirty()                                       # nothing new: nothing printed
    assert capsys.readouterr().out == ""
    log.save(final=True)
    assert capsys.readouterr().out.count(begin) == 1


def test_a_note_keeps_the_wall_time_since_import(tmp_path, fresh_runtime, monkeypatch):
    monkeypatch.setattr(fresh_runtime, "_IMPORTED", fresh_runtime.time.monotonic() - 100.0)
    log = fresh_runtime.RunLogger(directory=tmp_path, save_at_exit=False, echo=False)
    log.note("now")
    assert 100.0 <= log.meta["notes"][0]["wall"] < 160.0


# ---------------------------------------------------------------------------
# Julia timings
# ---------------------------------------------------------------------------

@pytest.fixture
def no_timings(fresh_runtime, monkeypatch):
    monkeypatch.setattr(fresh_runtime, "_TIMINGS", {})
    monkeypatch.setattr(fresh_runtime, "_TIMING_PRINTED", [None])
    return fresh_runtime


def test_loading_the_backend_is_timed_and_printed(tmp_path, monkeypatch, fake_julia,
                                                  no_timings, capsys):
    runtime = no_timings
    assert runtime.julia_timings() == {}
    write(tmp_path / "backend.jl", "module Backend\nend\n")
    monkeypatch.setattr(runtime, "here", lambda: tmp_path)
    packages = [("JSON3", "0f8b85d8-7281-11e9-16c2-39a750bddbf1", "1")]
    backend = runtime.load_julia_backend("backend.jl", packages=packages,
                                         julia_version="=1.11.9")
    timings = runtime.julia_timings()
    for name in ("resolve_seconds", "start_seconds", "include_seconds", "total_seconds",
                 "loaded_at"):
        assert timings[name] >= 0
    assert timings["first_calls"] == {}
    assert timings["key"] == runtime.julia_key("=1.11.9", packages, timings["juliacall"],
                                               timings["juliapkg"])
    out = capsys.readouterr().out
    marker = [line for line in out.splitlines() if line.startswith("-----ROBOTARIUM TIMING ")]
    assert len(marker) == 1 and json.loads(marker[0][23:-5])["key"] == timings["key"]
    assert "robotarium_runtime: Julia" in out

    runtime.call_json(backend.build, {"n": 1})
    runtime.call_json(backend.build, {"n": 2})
    assert list(runtime.julia_timings()["first_calls"]) == ["<lambda>"]

    # The first save prints the timings again, now with the first calls, and keeps them.
    log = runtime.RunLogger(directory=tmp_path, save_at_exit=False, stdout_copy=False)
    log.record(x=1)
    run = runtime.load_run(log.save())
    assert run.meta["julia"]["first_calls"] == {"<lambda>": pytest.approx(0, abs=1)}
    assert capsys.readouterr().out.count("-----ROBOTARIUM TIMING ") == 1
    log.save()
    assert "ROBOTARIUM TIMING" not in capsys.readouterr().out


def test_the_key_depends_on_what_is_installed(no_timings):
    key = no_timings.julia_key
    packages = [{"name": "JSON3", "uuid": "u", "version": "1"}]
    assert key("=1.11.9", packages, "0.9.35", "0.1.24") == \
        key("=1.11.9", [("JSON3", "u", "1")], "0.9.35", "0.1.24")
    assert key("=1.11.9", packages, "0.9.35", "0.1.24") != \
        key("=1.11.8", packages, "0.9.35", "0.1.24")
    assert key("=1.11.9", packages, "0.9.35", "0.1.24") != \
        key("=1.11.9", packages + [("Graphs", "g", None)], "0.9.35", "0.1.24")
    assert key("=1.11.9", packages, "0.9.35", "0.1.24") != \
        key("=1.11.9", packages, "0.9.36", "0.1.24")


# ---------------------------------------------------------------------------
# FloorOverlay
# ---------------------------------------------------------------------------

def test_an_overlay_without_a_figure_does_nothing_and_imports_no_matplotlib(tmp_path):
    import subprocess
    script = (
        "import sys, types\n"
        "import robotarium_runtime as runtime\n"
        "overlay = runtime.FloorOverlay(types.SimpleNamespace(_axes_handle=None))\n"
        "overlay.segment('a', (0, 0), (1, 1)); overlay.ring('b', (0, 0), 0.1)\n"
        "overlay.arrow('c', (0, 0), (1, 0)); overlay.clock(1.0, label='x'); overlay.clear()\n"
        "print(overlay.enabled, overlay.keys(), 'matplotlib' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         check=True).stdout
    assert out.strip() == "False [] False"


@pytest.fixture
def arena():
    """A figure laid out as rps lays its own out, without the simulator."""
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure, axes = plt.subplots(figsize=(8.25, 5.25))
    axes.set_xlim(-1.65, 1.65)
    axes.set_ylim(-1.05, 1.05)
    axes.set_aspect("equal")
    yield types.SimpleNamespace(_fig=figure, _axes_handle=axes)
    plt.close(figure)


def test_an_overlay_updates_its_artists_in_place(arena, fresh_runtime):
    overlay = fresh_runtime.FloorOverlay(arena)
    assert overlay.enabled and overlay.figure is arena._fig
    for step in range(3):
        x = np.array([[0.1 * step, 0.5], [0.0, 0.2], [0.0, 1.0]])
        overlay.segment("link", x[:, 0], x[:, 1], color="#c03030", style="--")
        overlay.polyline("path", np.array([[0, 1, 1], [0, 0, 1]]), closed=True)
        overlay.ring("slot", x[:, 1], 0.08)
        overlay.dot("goal", (0.3, 0.3), filled=False)
        overlay.arrow("heading", x[:, 0], (0.2, 0.1 * step))
        overlay.text("label", (0.5, 0.5), "step %d" % step)
        overlay.clock(step * 0.033, label="assembling")
        if step == 0:
            first = {key: overlay._artists[key] for key in overlay.keys()}
    assert {key: overlay._artists[key] for key in overlay.keys()} == first
    assert overlay.keys() == ["link", "path", "slot", "goal", "heading", "label", "__clock__"]
    assert len(arena._axes_handle.lines) == 2
    assert list(first["link"].get_xdata()) == [0.2, 0.5]
    assert first["link"].get_linestyle() == "--"
    assert list(first["path"].get_xdata()) == [0, 1, 1, 0]
    assert first["slot"].get_center() == (0.5, 0.2) and not first["slot"].get_fill()
    assert first["__clock__"].get_text() == "t = 0.1 s  assembling"
    assert first["label"].get_text() == "step 2"
    arena._fig.canvas.draw()


def test_a_zero_arrow_hides_and_keys_can_be_hidden_and_removed(arena, fresh_runtime):
    overlay = fresh_runtime.FloorOverlay(arena)
    overlay.arrow("a", (0, 0), (0.2, 0))
    overlay.arrow("a", (0, 0), (0, 0))
    assert not overlay._artists["a"].get_visible()
    overlay.arrow("a", (0, 0), (0.1, 0.1))
    assert overlay._artists["a"].get_visible()
    overlay.ring("r1", (0, 0), 0.1)
    overlay.ring("r2", (0, 0), 0.2)
    overlay.hide("r1")
    assert not overlay._artists["r1"].get_visible()
    overlay.show("r1")
    assert overlay._artists["r1"].get_visible()
    overlay.clear("r")
    assert overlay.keys() == ["a"]
    overlay.remove("a")
    overlay.hide("never drawn")
    assert overlay.keys() == [] and len(arena._axes_handle.patches) == 0


def test_a_drawing_error_turns_the_overlay_off_without_failing_the_run(arena, fresh_runtime,
                                                                        capsys):
    overlay = fresh_runtime.FloorOverlay(arena)
    overlay.segment("bad", (1,), (2, 3))
    assert not overlay.enabled and isinstance(overlay.error, ValueError)
    assert "FloorOverlay is off" in capsys.readouterr().out
    overlay.segment("fine", (0, 0), (1, 1))
    assert overlay.keys() == []
