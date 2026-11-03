"""
Helpers that travel WITH an experiment to the Robotarium: loading a Julia backend, logging a run,
drawing on the arena figure, recording a local video, and reading what the bundle was built
with.

    from robotarium_runtime import FloorOverlay, RunLogger, load_julia_backend, config

The testbed has none of the `robotarium` package installed, so this one file is copied into
every bundle whose .py files import it (`robotarium bundle` does that), and it is uploaded as a
supporting file beside the main file. It is installed as a top-level module too, so the same
import works on a development machine. That is why it is a single file, and why it needs only the
standard library and numpy -- which the testbed has -- plus juliacall when a Julia backend is
loaded, which the testbed also has (it is in the simulator's libraries.txt).

Keep it runnable on Python 3.10: it runs on the testbed's interpreter, not the one that built
the bundle.
"""

import ast
import atexit
import base64
import datetime
import hashlib
import io
import json
import os
import platform
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

__version__ = "0.1.0"

#: The testbed's fixed step, in seconds; see robotarium.limits.STEP_SECONDS.
STEP_SECONDS = 0.033

#: Where RunLogger writes when no directory is given: `robotarium simulate` points it at
#: .robotarium/runs/ so local runs do not litter the bundle. Unset on the testbed, where it
#: writes beside the main file.
LOG_DIR_VARIABLE = "ROBOTARIUM_LOG_DIR"

#: The lines RunLogger prints a copy of its file between: the file's name, its size in bytes
#: and its SHA-256, then the file in base64, then the end line. robotarium.results reads them.
COPY_BEGIN = "-----BEGIN ROBOTARIUM RUN %s %d %s-----"
COPY_END = "-----END ROBOTARIUM RUN %s-----"

#: The line julia timings are printed on, as compact JSON; robotarium.results reads it out of
#: log.txt into julia_timing.json.
TIMING_MARKER = "-----ROBOTARIUM TIMING %s-----"

#: When this module was imported: the zero of each note's "wall" time and of the timings.
_IMPORTED = time.monotonic()


def _emit(text: str) -> None:
    """
    Writes text to stdout whole, and flushes it.

    Julia's runtime (libuv) makes stdout non-blocking when juliacall starts it. A plain write of
    a long text into a pipe that is full then keeps what fits and drops the rest without an
    error, so a run log's copy printed mid-run arrives cut short, its end line missing and the
    next line glued to what came through. A non-blocking stdout is therefore written through its
    file descriptor, waiting while the pipe is full; any other is written as usual.
    """
    stream = sys.stdout
    if stream is None:
        return
    descriptor = _non_blocking(stream)
    if descriptor is None:
        stream.write(text)
        stream.flush()
        return
    # What the stream holds already goes first, and it can meet a full pipe too.
    while True:
        try:
            stream.flush()
            break
        except BlockingIOError:
            _wait_writable(descriptor)
    data = memoryview(text.encode(getattr(stream, "encoding", None) or "utf-8",
                                  getattr(stream, "errors", None) or "strict"))
    while data:
        try:
            written = os.write(descriptor, data)
        except BlockingIOError:
            _wait_writable(descriptor)
            continue
        except InterruptedError:
            continue
        data = data[written:]


def _non_blocking(stream):
    """The stream's file descriptor when it is non-blocking (POSIX only); else None."""
    try:
        import fcntl
        descriptor = stream.fileno()
        if fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_NONBLOCK:
            return descriptor
    except (ImportError, AttributeError, ValueError, OSError):  # io.UnsupportedOperation too
        pass
    return None


def _wait_writable(descriptor: int) -> None:
    """Until a full pipe has room again (a second at most, then the write is tried anyway)."""
    if os.name == "posix":
        try:
            select.select([], [descriptor], [], 1.0)
            return
        except (OSError, ValueError):
            pass
    time.sleep(0.01)


# ---------------------------------------------------------------------------
# where the bundle is, and what it was built with
# ---------------------------------------------------------------------------

def here() -> Path:
    """
    The directory the main file runs from, which is where every uploaded file lands: the form
    flattens the bundle into one directory. Falls back to this file's own directory, which in a
    bundle is the same one, then to the working directory.
    """
    main = sys.modules.get("__main__")
    main_file = getattr(main, "__file__", None)
    if main_file:
        return Path(main_file).resolve().parent
    return Path(__file__).resolve().parent


def bundle_path(name: str) -> Path:
    """
    A file uploaded with the experiment, by its base name. Resolve supporting files with this
    (or with Path(__file__).parent) rather than by a path relative to the working directory,
    which on the testbed need not be the bundle's directory.

    Args:
        name (str): The file's base name, e.g. "backend.jl" or "waypoints.npy".
    """
    for directory in (here(), Path(__file__).resolve().parent, Path.cwd()):
        candidate = directory / name
        if candidate.exists():
            return candidate
    return here() / name


def bundle() -> dict:
    """
    What `robotarium bundle` recorded about this build, from the generated robotarium_bundle.py
    beside the main file: {"experiment": {title, robots, duration, built_at, git, files},
    "config": {...} or None, "julia": {"version", "packages"}}. Empty when the main file is run
    outside a bundle.
    """
    for directory in (here(), Path(__file__).resolve().parent):
        path = directory / "robotarium_bundle.py"
        if path.is_file():
            values = _literals(path)
            return {"experiment": dict(values.get("EXPERIMENT") or {}),
                    "config": values.get("CONFIG"),
                    "julia": dict(values.get("JULIA") or {})}
    return {}


def _literals(path: Path) -> dict:
    """The module-level NAME = <literal> assignments of a generated module, without running it."""
    values = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return values


def config(default=None):
    """
    The run's parameters: the [experiment] config the bundle was built with, carried as a dict
    literal because the form takes no .toml, .json or .yaml. Run outside a bundle, on a
    development machine, it is read from the nearest robotarium.toml instead, so `python
    main.py` and the bundle see the same numbers. `default` when there is neither.

    Args:
        default: What to return when no config is declared.
    """
    carried = _carried().get("config")
    return default if carried is None else carried


def _carried() -> dict:
    """What the bundle carries, or failing that what the project's robotarium.toml declares."""
    return bundle() or _manifest()


def _manifest() -> dict:
    """
    The config and [julia] table of the nearest robotarium.toml at or above the main file's
    directory, in the shape bundle() returns. {} without one, and on Python < 3.11, which has
    no tomllib -- the testbed, where the bundle carries all of this anyway.
    """
    try:
        import tomllib
    except ImportError:
        return {}
    start = here()
    for directory in (start, *start.parents):
        manifest = directory / "robotarium.toml"
        if manifest.is_file():
            break
    else:
        return {}
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    carried = {"experiment": {}, "config": data.get("config"), "julia": data.get("julia") or {}}
    named = (data.get("experiment") or {}).get("config")
    if named:
        path = directory / named
        if path.suffix == ".toml":
            carried["config"] = tomllib.loads(path.read_text(encoding="utf-8"))
        elif path.suffix == ".json":
            carried["config"] = json.loads(path.read_text(encoding="utf-8"))
        else:
            import yaml
            carried["config"] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return carried


# ---------------------------------------------------------------------------
# a Julia backend
# ---------------------------------------------------------------------------

class JuliaBackendError(RuntimeError):
    """The Julia backend could not be set up; the message says which step failed."""


_DECLARED = set()
_BACKENDS = {}

#: What loading the backend took, filled in as it happens; see julia_timings().
_TIMINGS = {}
_TIMING_PRINTED = [None]


def julia_timings() -> dict:
    """
    How long each step of starting the Julia backend took in this process, in seconds:

        {"resolve_seconds": ..., "start_seconds": ..., "include_seconds": ...,
         "first_calls": {"build_operators": ...}, "total_seconds": ...,
         "julia_version": "1.11.9", "juliacall": "0.9.35", "juliapkg": "0.1.24",
         "key": "<sha256>", "loaded_at": ...}

    resolve is juliapkg installing Julia and the packages (minutes the first time, on the
    testbed every time); start is juliacall starting Julia; include is the .jl file loading
    its packages; first_calls are each function's first call_json, which compiles it.
    total_seconds is their sum: what the run spends on Julia before its robots can move.
    loaded_at is when the backend was ready, in seconds since this module was imported. key
    identifies the Julia version, packages and juliacall/juliapkg versions it was measured
    with (see julia_key). Empty when no backend was loaded.
    """
    if not _TIMINGS:
        return {}
    timings = dict(_TIMINGS)
    timings["first_calls"] = dict(_TIMINGS.get("first_calls") or {})
    timings["total_seconds"] = round(sum(timings.get(name) or 0.0 for name in (
        "resolve_seconds", "start_seconds", "include_seconds"))
        + sum(timings["first_calls"].values()), 3)
    return timings


def julia_key(julia_version, packages, juliacall_version, juliapkg_version) -> str:
    """
    What a Julia startup time depends on, as a sha256: the [julia] version specifier, the
    packages (name, uuid, version), and the juliacall and juliapkg versions. A timing is only
    compared with a project that has the same key.
    """
    # A package listed twice, once without a version, must not fail the sort (None and a
    # string do not compare).
    entries = sorted((_package_entry(each) for each in packages or ()),
                     key=lambda entry: tuple("" if part is None else part for part in entry))
    text = json.dumps({"julia": None if julia_version is None else str(julia_version),
                       "packages": [list(each) for each in entries],
                       "juliacall": juliacall_version, "juliapkg": juliapkg_version},
                      sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _distribution_version(name):
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:  # noqa: BLE001 -- not installed, or a stand-in with no metadata
        module = sys.modules.get(name)
        return getattr(module, "__version__", None)


#: What the marker leaves out of julia_timings(): the key already identifies the packages and
#: the platform, and the marker is kept to one short line in log.txt.
_MARKER_OMITS = ("packages", "platform")


def _print_timing() -> None:
    """
    The human-readable line and the marker, when the timings changed since last printed. Never
    raises: the timings are bookkeeping, and the run must not fail over them.
    """
    try:
        timings = julia_timings()
        if not timings:
            return
        text = json.dumps({key: value for key, value in timings.items()
                           if key not in _MARKER_OMITS}, sort_keys=True, separators=(",", ":"))
        if text == _TIMING_PRINTED[0]:
            return
        _TIMING_PRINTED[0] = text
        calls = timings["first_calls"]
        _emit("robotarium_runtime: Julia %s: packages %.1f s, start %.1f s, include %.1f s%s; "
              "%.1f s in all\n%s\n" % (
                  timings.get("julia_version") or "?", timings.get("resolve_seconds") or 0.0,
                  timings.get("start_seconds") or 0.0, timings.get("include_seconds") or 0.0,
                  "".join(", first %s() %.1f s" % (name, seconds)
                          for name, seconds in calls.items()), timings["total_seconds"],
                  TIMING_MARKER % text))
    except Exception as error:  # noqa: BLE001 -- see above
        _report_bookkeeping(error)


def _report_bookkeeping(error) -> None:
    try:
        print("robotarium_runtime: could not record the Julia timings: %s: %s"
              % (type(error).__name__, error), flush=True)
    except Exception:  # noqa: BLE001 -- nowhere left to say it
        pass


def declare_julia_packages(packages, julia_version=None) -> None:
    """
    Has juliapkg install the backend's packages, BEFORE Julia starts.

    They must be resolved together with juliacall's own packages, in one Pkg operation, and
    installed by a separate Julia process. Pkg.add-ing them from inside a running session does
    not work: juliacall has already loaded PythonCall's dependencies by then, so a package the
    add has to downgrade stays loaded at its old version and whatever needs the new one fails to
    precompile. That happened on the Robotarium (robosheaf, with CellularSheaves.jl: Parsers 3
    was loaded, CSV needed Parsers 2). And a juliapkg.json cannot carry the declaration instead:
    the form takes no .json.

    Args:
        packages: (name, uuid, version) tuples or {"name", "uuid", "version"} dicts; version
            may be None.
        julia_version (str): A compat specifier for Julia itself, e.g. "1.11"; None for any.
    """
    try:
        import juliapkg
    except ImportError as error:
        raise JuliaBackendError("juliacall is not installed in this Python. Locally: "
                                "pip install juliacall") from error

    entries = [_package_entry(each) for each in packages or ()]
    pending = [each for each in entries if each not in _DECLARED]
    if not pending and not julia_version:
        return

    if "juliacall" in sys.modules and pending:
        print("robotarium_runtime: WARNING: Julia is already running, so %s may load against "
              "the wrong versions of their dependencies. Call load_julia_backend (or "
              "declare_julia_packages) before anything imports juliacall."
              % ", ".join(name for name, _, _ in pending), flush=True)

    if julia_version:
        juliapkg.require_julia(str(julia_version))
    for name, uuid, version in pending:
        if version:
            juliapkg.add(name, uuid, version=str(version))
        else:
            juliapkg.add(name, uuid)

    started = time.monotonic()
    try:
        juliapkg.resolve()
    except Exception as error:
        raise JuliaBackendError(
            "Could not install the Julia packages %s. Check each name, UUID and version; on "
            "the testbed, the cause can also be that it could not reach the package servers."
            % ", ".join(name for name, _, _ in entries)) from error
    finally:
        _TIMINGS["resolve_seconds"] = round(
            (_TIMINGS.get("resolve_seconds") or 0.0) + time.monotonic() - started, 3)
    _DECLARED.update(pending)


def load_julia_backend(path="backend.jl", module=None, packages=None, julia_version=None):
    """
    Declares the backend's Julia packages, starts Julia, includes the .jl file and returns the
    Julia module it defines -- the backend.jl method, in one call:

        backend = load_julia_backend("backend.jl")
        result = backend.consensus_gains(json.dumps(spec))

    Args:
        path: The .jl file. A bare name is looked up beside the main file (see bundle_path).
        module (str): The module the file defines; default the first `module X` in it. A file
            that defines none returns juliacall's Main.
        packages: As declare_julia_packages takes them; default the [julia] packages the bundle
            was built with (or, outside a bundle, that robotarium.toml declares).
        julia_version (str): As declare_julia_packages takes it; default likewise.
    """
    path = Path(path)
    if not path.is_absolute():
        path = bundle_path(str(path))
    path = path.resolve()
    if path in _BACKENDS:
        return _BACKENDS[path]
    if not path.is_file():
        raise JuliaBackendError("%s was not found beside the main file. Upload it as a "
                                "supporting file (list it in [experiment] files)." % path.name)

    declared = _carried().get("julia") or {}
    if packages is None:
        packages = declared.get("packages") or ()
    if julia_version is None:
        julia_version = declared.get("version")
    _TIMINGS.setdefault("first_calls", {})
    declare_julia_packages(packages, julia_version)

    starting = "juliacall" not in sys.modules
    started = time.monotonic()
    from juliacall import Main as jl
    if starting:
        _TIMINGS["start_seconds"] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    try:
        jl.include(str(path))
    except Exception as error:
        raise JuliaBackendError(
            "Julia could not load %s: %s. If it says a package is missing, declare it under "
            "[julia] packages in robotarium.toml (or pass packages=)." % (path.name, error)
        ) from error
    finally:
        _TIMINGS["include_seconds"] = round(
            (_TIMINGS.get("include_seconds") or 0.0) + time.monotonic() - started, 3)

    if module is None:
        module = julia_module_name(path.read_text(encoding="utf-8"))
    backend = getattr(jl, module) if module else jl
    _BACKENDS[path] = backend

    # Bookkeeping, after the backend has loaded: whatever goes wrong here is reported, and the
    # run goes on.
    try:
        juliacall_version = _distribution_version("juliacall")
        juliapkg_version = _distribution_version("juliapkg")
        _TIMINGS.update(
            julia_version=_julia_version(jl), juliacall=juliacall_version,
            juliapkg=juliapkg_version,
            julia_requested=None if julia_version is None else str(julia_version),
            packages=[list(_package_entry(each)) for each in packages or ()],
            key=julia_key(julia_version, packages, juliacall_version, juliapkg_version),
            loaded_at=round(time.monotonic() - _IMPORTED, 3), cpus=os.cpu_count(),
            platform=platform.platform())
    except Exception as error:  # noqa: BLE001 -- see above
        _report_bookkeeping(error)
    _print_timing()
    return backend


def _julia_version(jl):
    try:
        return str(jl.seval("string(VERSION)"))
    except Exception:  # noqa: BLE001 -- a Main without seval: a stand-in, or an old juliacall
        return None


def julia_module_name(source: str):
    """The name of the first module a Julia source defines, or None."""
    match = re.search(r"^\s*(?:bare)?module\s+([A-Za-z_][A-Za-z0-9_!]*)", source, re.MULTILINE)
    return match.group(1) if match else None


def call_json(function, payload):
    """
    Calls a Julia function with `payload` as a JSON string: the simplest way to hand nested
    Python data to Julia, which parses it with JSON3.jl (or JSON.jl).

    Args:
        function: A Julia function, e.g. backend.build_operators.
        payload: Anything json.dumps takes; numpy arrays are converted to lists.
    """
    text = json.dumps(payload, default=_jsonable)
    calls = _TIMINGS.get("first_calls")
    if calls is None:
        return function(text)
    name = _function_name(function)
    if name in calls:
        return function(text)
    started = time.monotonic()
    try:
        return function(text)
    finally:
        calls[name] = round(time.monotonic() - started, 3)
        # Into log.txt now, with this first call: a run the testbed stops before the run log's
        # next write would otherwise leave only the marker printed at load, without it.
        _print_timing()


def _function_name(function) -> str:
    """A Julia function's name, as juliacall shows it; a Python one's __name__."""
    if type(function).__module__.startswith("juliacall"):
        try:
            return str(function)
        except Exception:  # noqa: BLE001
            pass
    return str(getattr(function, "__name__", None) or function)


def as_arrays(result, names, dtype=float) -> dict:
    """
    Fields of a Julia result -- a NamedTuple or struct -- as numpy arrays, by name.

        operators = as_arrays(backend.build(spec), ["delta", "laplacian"])

    Args:
        result: What a Julia function returned.
        names: The field names to take.
        dtype: The arrays' dtype.
    """
    return {name: np.asarray(getattr(result, name), dtype=dtype) for name in names}


def _package_entry(package) -> tuple:
    if isinstance(package, dict):
        return (str(package["name"]), str(package["uuid"]),
                None if package.get("version") is None else str(package["version"]))
    name, uuid, *rest = package
    version = rest[0] if rest else None
    return (str(name), str(uuid), None if version is None else str(version))


# ---------------------------------------------------------------------------
# logging a run
# ---------------------------------------------------------------------------

#: Names np.savez_compressed takes as its own parameters, so a series cannot have them.
_RESERVED = ("file", "allow_pickle")

#: The paths RunLoggers in this process write to.
_CLAIMED = set()


class RunLogger:
    """
    Records a run step by step and writes it as one .npz file: an array per recorded quantity,
    the steps each was recorded at, and a JSON record of what ran (the bundle's title, robots,
    commit and file hashes, when it started, notes made along the way).

        log = RunLogger("consensus")
        for t in range(iterations):
            x = r.get_poses()
            ...
            log.record(poses=x, velocities=dxu)
        log.save()

    On the testbed it writes beside the main file, but so far the testbed has returned none of
    the files a run writes, only what it prints (log.txt): hans-riess/robotarium#7. So each
    write is also printed, base64-encoded between marker lines, and `robotarium fetch` turns it
    back into the .npz. `autosave_every` rewrites the file every that many records, so a run the
    testbed stops at its declared duration still leaves its data; it is also written when the
    interpreter exits. Every write prints the whole file again, so a long run that autosaves
    often fills log.txt with copies: `copy_every` prints only the first write and every that
    many after it (and the last), and `dtype="float32"` halves what floats take.

    Args:
        name (str): The file name's stem; the start time is appended.
        directory: Where to write; default $ROBOTARIUM_LOG_DIR, else the main file's directory.
        meta (dict): Anything else to keep in the record; must be JSON-serialisable.
        autosave_every (int): Rewrite the file every this many record() calls; 0 for never.
        save_at_exit (bool): Write the file when the interpreter exits, if it has changed.
        echo (bool): Print notes as well as keeping them.
        stdout_copy (bool): Print a copy of the file each time it is written; default when
            stdout is not a terminal, as on the testbed and in `robotarium simulate`.
        dtype: A floating dtype, such as "float32", to store every floating-point value in;
            integers, booleans and text are kept as they are. None keeps each as recorded.
            float32 keeps about seven significant digits, so record times relative to the
            run's start, not absolute clock readings: time.monotonic() a day after boot is
            stored to the nearest 0.008 s, and later ones more coarsely.
        copy_every (int): With stdout_copy, print the copy on the first write and on every
            this many writes after it (writes 1, N + 1, 2N + 1, ...). The write at exit, and
            save(final=True), always print it, so the last copy in log.txt is the whole run.
            A run killed outright, with no exit handlers, leaves log.txt the last copy
            printed: up to N - 1 writes behind.
    """

    def __init__(self, name="run", directory=None, meta=None, autosave_every=0,
                 save_at_exit=True, echo=True, stdout_copy=None, dtype=None, copy_every=1):
        self.started = datetime.datetime.now().astimezone()
        directory = Path(directory or os.environ.get(LOG_DIR_VARIABLE) or here())
        stem = "%s_%s" % (name, self.started.strftime("%Y%m%d_%H%M%S"))
        self.path = directory / ("%s.npz" % stem)
        # Two loggers of one name started within a second must not write the same file.
        number = 1
        while self.path.resolve() in _CLAIMED or self.path.exists():
            number += 1
            self.path = directory / ("%s_%d.npz" % (stem, number))
        _CLAIMED.add(self.path.resolve())
        self.autosave_every = int(autosave_every)
        self.echo = echo
        self.stdout_copy = stdout_copy
        self.dtype = None
        if dtype is not None:
            self.dtype = np.dtype(dtype)
            if self.dtype.kind != "f":
                raise ValueError("RunLogger: dtype must be a floating type such as \"float32\", "
                                 "not %s." % self.dtype)
        self.copy_every = max(1, int(copy_every))
        self._saves = 0
        self._copy_pending = False
        self._series = {}
        self._steps = {}
        self._next_step = 0
        self._last_step = None
        self._records = 0
        self._dirty = False
        built = bundle().get("experiment") or {}
        self.meta = {"name": name, "started_at": self.started.isoformat(timespec="seconds"),
                     "step_seconds": STEP_SECONDS, "bundle": built, "notes": [],
                     "python": platform.python_version(), "numpy": np.__version__}
        self.meta.update(meta or {})
        if save_at_exit:
            atexit.register(self._save_if_dirty)

    def record(self, step=None, **values) -> None:
        """
        Records one step's values. Each keyword is one quantity; its shape must stay the same
        from step to step.

        Args:
            step (int): The step this is; default one more than the last step recorded.
            **values: Arrays or numbers, e.g. poses=x, velocities=dxu, error=0.12.
        """
        step = self._next_step if step is None else int(step)
        arrays = {key: np.array(value, copy=True) for key, value in values.items()}
        if self.dtype is not None:
            arrays = {key: array.astype(self.dtype) if array.dtype.kind == "f" else array
                      for key, array in arrays.items()}
        # Every value is checked before any is kept, so a refused record leaves no series a
        # step longer than the others.
        for key, array in arrays.items():
            if key in _RESERVED or key.startswith("__"):
                raise ValueError("RunLogger: %r is a name the file format reserves; record it "
                                 "under another." % key)
            # Objects in any form -- object arrays, structured dtypes with an object field,
            # numpy 2's StringDType -- need pickling, and the file must load without it.
            if array.dtype.hasobject or array.dtype.kind == "T":
                raise ValueError("RunLogger: %r is not numbers or text. Record numbers, arrays "
                                 "or strings; put anything else in meta." % key)
            series = self._series.get(key)
            if series and series[0].shape != array.shape:
                raise ValueError("RunLogger: %r was recorded with shape %s and now %s. Record "
                                 "a quantity with the same shape at every step."
                                 % (key, series[0].shape, array.shape))
            if series:
                try:
                    np.result_type(series[0].dtype, array.dtype)
                except TypeError:
                    raise ValueError("RunLogger: %r was recorded as %s and now as %s, which "
                                     "cannot be kept as one array." % (
                                         key, series[0].dtype, array.dtype)) from None
        for key, array in arrays.items():
            self._series.setdefault(key, []).append(array)
            self._steps.setdefault(key, []).append(step)
        self._next_step = step + 1
        self._last_step = step
        self._records += 1
        self._dirty = True
        if self.autosave_every and self._records % self.autosave_every == 0:
            self.save()

    def note(self, message: str, step=None) -> None:
        """
        Keeps (and prints) a remark tied to a step: a phase change, a detected fault.

        Args:
            message (str): The remark.
            step (int): The step it is about; default the last one recorded (0 before any).

        Each note also keeps "wall", the seconds since robotarium_runtime was imported, which
        places it in the testbed's video and log.txt (Julia's startup comes first).
        """
        if step is None:
            step = 0 if self._last_step is None else self._last_step
        step = int(step)
        self.meta["notes"].append({"step": step, "seconds": round(step * STEP_SECONDS, 3),
                                   "message": str(message),
                                   "wall": round(time.monotonic() - _IMPORTED, 3)})
        self._dirty = True
        if self.echo:
            _emit("[step %d | %.2f s] %s\n" % (step, step * STEP_SECONDS, message))

    def save(self, final=False) -> Path:
        """
        Writes everything recorded so far, replacing the previous write, and returns where. With
        stdout_copy, prints the file first, so a write that fails still leaves the copy.

        Args:
            final (bool): Print the copy whatever copy_every says, as the write at exit does.
        """
        arrays = {}
        for key, series in self._series.items():
            arrays[key] = np.stack(series)
            arrays["__steps__" + key] = np.asarray(self._steps[key], dtype=np.int64)
        meta = dict(self.meta, saved_at=datetime.datetime.now().astimezone().isoformat(
            timespec="seconds"), records=self._records)
        try:
            timings = julia_timings()
        except Exception:  # noqa: BLE001 -- bookkeeping must not cost the run its log
            timings = None
        if timings and "julia" not in self.meta:
            meta["julia"] = timings
        arrays["__meta__"] = np.array(json.dumps(meta, default=_jsonable))
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **arrays)
        content = buffer.getvalue()
        # The first calls to the backend have happened by now, and log.txt should have them.
        _print_timing()
        self._saves += 1
        if self._copies_to_stdout():
            # The first write prints, so even a run killed early leaves one copy in log.txt.
            if final or (self._saves - 1) % self.copy_every == 0:
                self._print_copy(content)
                self._copy_pending = False
            else:
                self._copy_pending = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Written aside and moved into place, so a run killed mid-write leaves the last
        # complete file rather than a truncated one.
        partial = self.path.with_name(self.path.name + ".partial")
        try:
            with open(partial, "wb") as handle:
                handle.write(content)
            os.replace(partial, self.path)
        except BaseException:
            try:
                partial.unlink()
            except OSError:
                pass
            raise
        self._dirty = False
        return self.path

    def _copies_to_stdout(self) -> bool:
        if self.stdout_copy is not None:
            return bool(self.stdout_copy)
        try:
            return not sys.stdout.isatty()
        except (AttributeError, ValueError):  # no stdout (pythonw), or a closed one
            return False

    def _print_copy(self, content: bytes) -> None:
        """The file as text, in one write so that nothing else lands inside it (see _emit)."""
        _emit("%s\n%s%s\n" % (
            COPY_BEGIN % (self.path.name, len(content), hashlib.sha256(content).hexdigest()),
            base64.encodebytes(content).decode("ascii"), COPY_END % self.path.name))

    def _save_if_dirty(self) -> None:
        # A write whose copy copy_every held back is written again, to print it.
        if self._dirty or self._copy_pending:
            try:
                _emit("Run log: %s\n" % self.save(final=True))
            except Exception as error:  # noqa: BLE001 -- at exit, report rather than raise
                print("robotarium_runtime: the run log could not be written: %s" % error)


class RunRecord:
    """A run RunLogger wrote, read back: .meta, .data[name] and .steps[name]."""

    def __init__(self, meta: dict, data: dict, steps: dict):
        self.meta = meta
        self.data = data
        self.steps = steps

    def __repr__(self) -> str:
        return "RunRecord(%r: %s)" % (self.meta.get("name"), ", ".join(
            "%s%s" % (key, list(value.shape)) for key, value in self.data.items()))


def load_run(path) -> RunRecord:
    """
    Reads a RunLogger file back.

    Args:
        path: The .npz file.
    """
    with np.load(path, allow_pickle=False) as archive:
        meta = json.loads(str(archive["__meta__"]))
        data, steps = {}, {}
        for key in archive.files:
            if key.startswith("__steps__"):
                steps[key[len("__steps__"):]] = archive[key]
            elif key != "__meta__":
                data[key] = archive[key]
    return RunRecord(meta, data, steps)


def _jsonable(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError("%r is not JSON-serialisable" % (value,))


# ---------------------------------------------------------------------------
# drawing on the arena
# ---------------------------------------------------------------------------

def _guarded(method):
    """
    A FloorOverlay method that does nothing when the overlay is off, and that turns the overlay
    off, with one printed line, rather than fail the run when drawing does. On the testbed the
    figure may be projected onto the floor; a drawing error must not stop the robots.
    """
    def wrapped(self, *args, **kwargs):
        if not self.enabled:
            return None
        try:
            return method(self, *args, **kwargs)
        except Exception as error:  # noqa: BLE001 -- see above
            self.enabled = False
            self.error = error
            # What was drawn would otherwise stay on the floor, frozen at its last update.
            for artist in list(self._artists.values()):
                try:
                    artist.set_visible(False)
                except Exception:  # noqa: BLE001
                    pass
            print("robotarium_runtime: FloorOverlay is off, and what it drew is hidden, after "
                  "an error in %s(): %s: %s" % (method.__name__, type(error).__name__, error),
                  flush=True)
            return None
    wrapped.__name__ = method.__name__
    wrapped.__doc__ = method.__doc__
    return wrapped


def _point(value) -> tuple:
    """A point as (x, y): a length-2 sequence, or the first two entries of a pose."""
    array = np.asarray(value, dtype=float).reshape(-1)
    if array.size < 2:
        raise ValueError("a point needs x and y, not %r" % (value,))
    return float(array[0]), float(array[1])


def _points(value) -> tuple:
    """A 2 x N (or 3 x N poses) array as (xs, ys)."""
    array = np.asarray(value, dtype=float)
    if array.ndim != 2 or array.shape[0] < 2:
        raise ValueError("points must be a 2 x N array, not shape %s" % (array.shape,))
    return array[0].tolist(), array[1].tolist()


#: What a FloorOverlay line, ring, dot or arrow is drawn with until a call says otherwise.
_LINE_STYLE = {"color": "#1f3b73", "width": 2.0, "style": "-", "alpha": 1.0}

#: And a label.
_TEXT_STYLE = {"color": "k", "size": 12, "ha": "left", "va": "center"}


class FloorOverlay:
    """
    Draws on the simulator's arena figure: links between robots, rails, rings, arrows, labels
    and a run clock, in metres in the arena frame, updated in place from step to step.

        overlay = FloorOverlay(r)
        for t in range(iterations):
            x = r.get_poses()
            overlay.segment("link12", x[:, 0], x[:, 1], color="#c03030")
            overlay.ring("slot1", goal[:, 0], 0.08, style="--")
            overlay.clock(t * STEP_SECONDS, label="assembling")
            ...
            r.step()           # rps redraws the figure; the overlay never does

    Each drawing method takes a key. Its first call creates the artist; later calls with the
    same key move or restyle that artist, never a new one, so a loop of 4,000 steps keeps a
    handful of artists. hide(), show(), remove() and clear() act on keys.

    A style argument (color, width, style, alpha, and a dot's radius and fill, an arrow's head,
    a label's size and alignment) that a call leaves out keeps the key's current value; the
    first call takes the default in the signature's docstring. So a loop may give only the
    geometry once the style is set:

        overlay.segment("rail", p, q, color="#c03030", width=3, style=":")   # first call
        overlay.segment("rail", p2, q2)                                     # still red, dotted

    The figure is rps's: the axes r._axes_handle (or r.axes), the figure r._fig (or r.figure).
    Without them (show_figure=False, and the testbed unless it draws one) `enabled` is False,
    every method does nothing, and Matplotlib is never imported. An error while drawing turns
    the overlay off with one printed line instead of failing the run.

    On a projected floor, rings and dots read at their size in metres, so give them real
    sizes (a robot is about 0.11 m across); an unfilled ring hides less of the robot under it
    than a filled disc.

    Args:
        r: The Robotarium.
        zorder (float): Where the overlay sits: rps draws its robots at 2, so 4 draws on top.
    """

    def __init__(self, r, zorder=4):
        axes = getattr(r, "_axes_handle", None)
        if axes is None:
            axes = getattr(r, "axes", None)
        if axes is not None and not hasattr(axes, "add_patch"):
            axes = None
        figure = getattr(r, "_fig", None)
        if figure is None:
            figure = getattr(r, "figure", None)
        if figure is None and axes is not None:
            figure = getattr(axes, "figure", None)
        self.axes = axes
        self.figure = figure
        self.zorder = zorder
        self.enabled = axes is not None
        self.error = None
        self._artists = {}
        self._styles = {}

    # -- styles -----------------------------------------------------------------------------

    def _style(self, key, defaults: dict, **given) -> dict:
        """The key's style: its current one (else `defaults`), with what this call gives."""
        style = dict(defaults)
        style.update(self._styles.get(key) or {})
        style.update((name, value) for name, value in given.items() if value is not None)
        self._styles[key] = style
        return style

    # -- lines ------------------------------------------------------------------------------

    @_guarded
    def segment(self, key, p, q, color=None, width=None, style=None, alpha=None):
        """
        A straight line from the point p to the point q. Defaults: color "#1f3b73", width
        2.0 points, style "-", alpha 1.0.
        """
        (x0, y0), (x1, y1) = _point(p), _point(q)
        self._line(key, [x0, x1], [y0, y1], self._style(
            key, _LINE_STYLE, color=color, width=width, style=style, alpha=alpha))

    @_guarded
    def polyline(self, key, points, color=None, width=None, style=None, alpha=None,
                 closed=False):
        """
        A path through the columns of a 2 x N array; closed joins the last to the first.
        Defaults: color "#1f3b73", width 1.5 points, style "-", alpha 1.0.
        """
        xs, ys = _points(points)
        if closed and xs:
            xs, ys = xs + xs[:1], ys + ys[:1]
        self._line(key, xs, ys, self._style(key, dict(_LINE_STYLE, width=1.5), color=color,
                                            width=width, style=style, alpha=alpha))

    def _line(self, key, xs, ys, style):
        line = self._artists.get(key)
        if line is None:
            line, = self.axes.plot(xs, ys, color=style["color"], linewidth=style["width"],
                                   linestyle=style["style"], alpha=style["alpha"],
                                   solid_capstyle="round", zorder=self.zorder)
            self._artists[key] = line
            return
        line.set_data(xs, ys)
        line.set_color(style["color"])
        line.set_linewidth(style["width"])
        line.set_linestyle(style["style"])
        line.set_alpha(style["alpha"])

    # -- circles ----------------------------------------------------------------------------

    @_guarded
    def ring(self, key, center, radius, color=None, width=None, style=None, alpha=None):
        """
        An unfilled circle of `radius` metres. Defaults: color "#1f3b73", width 2.0 points,
        style "-", alpha 1.0.
        """
        self._circle(key, center, radius, self._style(
            key, dict(_LINE_STYLE, filled=False), color=color, width=width, style=style,
            alpha=alpha))

    @_guarded
    def dot(self, key, point, radius=None, color=None, alpha=None, filled=None):
        """
        A small disc (or, with filled=False, a thin circle) of `radius` metres. Defaults:
        radius 0.03, color "#1f3b73", alpha 1.0, filled.
        """
        style = self._style(key, dict(_LINE_STYLE, width=1.0, radius=0.03, filled=True),
                            radius=radius, color=color, alpha=alpha, filled=filled)
        self._circle(key, point, style["radius"], style)

    def _circle(self, key, center, radius, style):
        center = _point(center)
        circle = self._artists.get(key)
        if circle is None:
            from matplotlib.patches import Circle
            circle = Circle(center, float(radius), fill=style["filled"], zorder=self.zorder)
            self.axes.add_patch(circle)
            self._artists[key] = circle
        else:
            circle.set_center(center)
            circle.set_radius(float(radius))
        circle.set_fill(style["filled"])
        circle.set_edgecolor(style["color"])
        circle.set_facecolor(style["color"] if style["filled"] else "none")
        circle.set_linewidth(style["width"])
        circle.set_linestyle(style["style"])
        circle.set_alpha(style["alpha"])

    # -- arrows -----------------------------------------------------------------------------

    @_guarded
    def arrow(self, key, origin, vector, color=None, width=None, head=None, alpha=None):
        """
        An arrow from `origin` along `vector`, both in metres, with a head `head` metres long;
        `width` is the shaft's, in points as for lines. A zero vector hides it. Defaults:
        color "#1f3b73", width 2.0 points, head 0.04 m, alpha 1.0.
        """
        style = self._style(key, dict(_LINE_STYLE, head=0.04), color=color, width=width,
                            head=head, alpha=alpha)
        (x, y), (dx, dy) = _point(origin), _point(vector)
        length = float(np.hypot(dx, dy))
        arrow = self._artists.get(key)
        if length < 1e-9:
            if arrow is not None:
                arrow.set_visible(False)
            return
        head = float(style["head"])
        shaft = max(float(style["width"]), 0.1) * self._metres_per_point()
        geometry = dict(x=x, y=y, dx=dx, dy=dy, width=shaft, head_width=0.8 * head,
                        head_length=min(head, length))
        if arrow is not None and hasattr(arrow, "set_data"):
            arrow.set_data(**geometry)
        else:
            if arrow is not None:  # Matplotlib before 3.5 cannot move one: replace it
                arrow.remove()
            from matplotlib.patches import FancyArrow
            arrow = FancyArrow(length_includes_head=True, zorder=self.zorder, linewidth=0,
                               **geometry)
            self.axes.add_patch(arrow)
            self._artists[key] = arrow
        arrow.set_color(style["color"])
        arrow.set_alpha(style["alpha"])
        arrow.set_visible(True)

    def _metres_per_point(self) -> float:
        """How many metres of the arena one typographic point spans in the figure."""
        try:
            x0, x1 = self.axes.get_xlim()
            inches = self.figure.get_size_inches()[0] * self.axes.get_position().width
            return abs(x1 - x0) / max(inches, 1e-9) / 72.0
        except Exception:  # noqa: BLE001 -- the rps figure's own scale, near enough
            return 0.0057

    # -- text -------------------------------------------------------------------------------

    @_guarded
    def text(self, key, point, string, color=None, size=None, ha=None, va=None):
        """A label at `point`. Defaults: color "k", size 12, ha "left", va "center"."""
        style = self._style(key, _TEXT_STYLE, color=color, size=size, ha=ha, va=va)
        x, y = _point(point)
        label = self._artists.get(key)
        if label is None:
            label = self.axes.text(x, y, str(string), color=style["color"],
                                   fontsize=style["size"], horizontalalignment=style["ha"],
                                   verticalalignment=style["va"], zorder=self.zorder + 1)
            self._artists[key] = label
            return
        label.set_position((x, y))
        label.set_text(str(string))
        label.set_color(style["color"])
        label.set_fontsize(style["size"])
        label.set_horizontalalignment(style["ha"])
        label.set_verticalalignment(style["va"])

    def clock(self, t, label=None, point=(-1.55, 0.93), size=14, color="k"):
        """
        "t = 12.3 s" (and the label after it) in the arena's top-left corner: the experiment's
        own clock, so a still from the testbed's video can be matched to the run's time.
        """
        text = "t = %.1f s" % float(t)
        if label:
            text += "  %s" % label
        self.text("__clock__", point, text, color=color, size=size, ha="left", va="center")

    # -- by key -----------------------------------------------------------------------------

    @_guarded
    def hide(self, key):
        """Stops drawing the key's artist, keeping it for show()."""
        if key in self._artists:
            self._artists[key].set_visible(False)

    @_guarded
    def show(self, key):
        """Draws a hidden key's artist again."""
        if key in self._artists:
            self._artists[key].set_visible(True)

    @_guarded
    def remove(self, key):
        """Takes the key's artist off the figure; the key's next call creates a new one."""
        artist = self._artists.pop(key, None)
        self._styles.pop(key, None)
        if artist is not None:
            artist.remove()

    def clear(self, prefix=""):
        """Removes every key that starts with prefix: every key, by default."""
        for key in [each for each in self._artists if str(each).startswith(prefix)]:
            self.remove(key)

    def keys(self) -> list:
        """The keys drawn so far, in the order they were first drawn."""
        return list(self._artists)


# ---------------------------------------------------------------------------
# a local video
# ---------------------------------------------------------------------------

class VideoRecorder:
    """
    Records a Matplotlib figure -- the simulator's, r._fig -- to an MP4 by piping frames to
    ffmpeg. H.264/yuv420p with +faststart, so the file plays inline in a browser.

        video = VideoRecorder(r._fig, "run.mp4")
        ... video.frame() after each r.step() ...
        video.close()

    For local runs: the testbed records its own video, and need not have ffmpeg. Without ffmpeg
    on PATH, or without a figure, every call is a no-op and `enabled` is False.

    Args:
        figure: The Matplotlib figure; None disables the recorder.
        filename: The .mp4 to write.
        fps (int): Frames per second; 30 is the testbed's real time at one frame per step.
        every (int): Keep one frame in this many calls to frame().
    """

    def __init__(self, figure, filename, fps=30, every=1):
        self.figure = figure
        self.filename = str(filename)
        self.fps = int(fps)
        self.every = max(1, int(every))
        self.enabled = figure is not None and shutil.which("ffmpeg") is not None
        self._process = None
        self._calls = 0

    def frame(self) -> None:
        """Captures the figure as it is now, as the next frame."""
        if not self.enabled:
            return
        self._calls += 1
        if (self._calls - 1) % self.every:
            return
        self.figure.canvas.draw()
        frame = np.ascontiguousarray(np.asarray(self.figure.canvas.buffer_rgba())[:, :, :3])
        if self._process is None:
            height, width = frame.shape[:2]
            Path(self.filename).parent.mkdir(parents=True, exist_ok=True)
            # libx264 needs even dimensions under yuv420p; the scale filter rounds down.
            self._process = subprocess.Popen(
                [shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-f", "rawvideo",
                 "-pix_fmt", "rgb24", "-s", "%dx%d" % (width, height), "-r", str(self.fps),
                 "-i", "-", "-an", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                 self.filename], stdin=subprocess.PIPE)
        self._process.stdin.write(frame.tobytes())

    def close(self):
        """Finishes the file and returns its path, or None if nothing was recorded."""
        if self._process is None:
            return None
        self._process.stdin.close()
        code = self._process.wait()
        self._process = None
        if code != 0:
            raise RuntimeError("ffmpeg exited with code %d writing %s" % (code, self.filename))
        return self.filename
