"""
Helpers that travel WITH an experiment to the Robotarium: loading a Julia backend, logging a run,
recording a local video, and reading what the bundle was built with.

    from robotarium_runtime import RunLogger, load_julia_backend, config

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
import shutil
import subprocess
import sys
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

    try:
        juliapkg.resolve()
    except Exception as error:
        raise JuliaBackendError(
            "Could not install the Julia packages %s. Check each name, UUID and version; on "
            "the testbed, the cause can also be that it could not reach the package servers."
            % ", ".join(name for name, _, _ in entries)) from error
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
    declare_julia_packages(packages, julia_version)

    from juliacall import Main as jl

    try:
        jl.include(str(path))
    except Exception as error:
        raise JuliaBackendError(
            "Julia could not load %s: %s. If it says a package is missing, declare it under "
            "[julia] packages in robotarium.toml (or pass packages=)." % (path.name, error)
        ) from error

    if module is None:
        module = julia_module_name(path.read_text(encoding="utf-8"))
    backend = getattr(jl, module) if module else jl
    _BACKENDS[path] = backend
    return backend


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
    return function(json.dumps(payload, default=_jsonable))


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
    interpreter exits.

    Args:
        name (str): The file name's stem; the start time is appended.
        directory: Where to write; default $ROBOTARIUM_LOG_DIR, else the main file's directory.
        meta (dict): Anything else to keep in the record; must be JSON-serialisable.
        autosave_every (int): Rewrite the file every this many record() calls; 0 for never.
        save_at_exit (bool): Write the file when the interpreter exits, if it has changed.
        echo (bool): Print notes as well as keeping them.
        stdout_copy (bool): Print a copy of the file each time it is written; default when
            stdout is not a terminal, as on the testbed and in `robotarium simulate`.
    """

    def __init__(self, name="run", directory=None, meta=None, autosave_every=0,
                 save_at_exit=True, echo=True, stdout_copy=None):
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
        """
        if step is None:
            step = 0 if self._last_step is None else self._last_step
        step = int(step)
        self.meta["notes"].append({"step": step, "seconds": round(step * STEP_SECONDS, 3),
                                   "message": str(message)})
        self._dirty = True
        if self.echo:
            print("[step %d | %.2f s] %s" % (step, step * STEP_SECONDS, message), flush=True)

    def save(self) -> Path:
        """
        Writes everything recorded so far, replacing the previous write, and returns where. With
        stdout_copy, prints the file first, so a write that fails still leaves the copy.
        """
        arrays = {}
        for key, series in self._series.items():
            arrays[key] = np.stack(series)
            arrays["__steps__" + key] = np.asarray(self._steps[key], dtype=np.int64)
        meta = dict(self.meta, saved_at=datetime.datetime.now().astimezone().isoformat(
            timespec="seconds"), records=self._records)
        arrays["__meta__"] = np.array(json.dumps(meta, default=_jsonable))
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **arrays)
        content = buffer.getvalue()
        if self._copies_to_stdout():
            self._print_copy(content)
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
        """The file as text, in one write so that nothing else lands inside it."""
        sys.stdout.write("%s\n%s%s\n" % (
            COPY_BEGIN % (self.path.name, len(content), hashlib.sha256(content).hexdigest()),
            base64.encodebytes(content).decode("ascii"), COPY_END % self.path.name))
        sys.stdout.flush()

    def _save_if_dirty(self) -> None:
        if self._dirty:
            try:
                print("Run log: %s" % self.save(), flush=True)
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
