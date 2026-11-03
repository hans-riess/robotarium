"""
The bundle: the experiment's files laid out flat in one directory, exactly as the testbed will
see them, plus what the form cannot carry any other way.

    .robotarium/bundle/
        main.py                 the main file
        helpers.py, backend.jl  the supporting files, by base name
        robotarium_bundle.py    generated: the title, robots, duration, commit and file hashes
                                this build was made with, the [experiment] config as CONFIG,
                                and the [julia] packages
        robotarium_runtime.py   vendored: this package's testbed-side helpers, when a .py
                                file imports them

Two things about the form shape all of it. It takes no .toml, .json or .yaml, so a config and the
Julia package list travel as Python literals in robotarium_bundle.py. And it uploads every file
under its base name into one directory, so the bundle is that directory: `robotarium simulate`
runs the main file from here, and a run that works here has the layout the testbed will give it.

A build is staged and then moved into place, so one that fails leaves the last good bundle.
There is one bundle directory per project, so a command that builds it and then runs or uploads
what it built holds the project's lock (locked) from the build to the end: a second command on
the same project waits, and cannot rebuild the bundle under the first one's run.
"""

import ast
import datetime
import hashlib
import importlib.util
import json
import logging
import math
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import limits
from ._version import __version__
from .experiment import BUNDLE_MODULE, RUNTIME_MODULE, git_revision
from .findings import errors
from .preflight import imports

log = logging.getLogger(__name__)


class BundleError(RuntimeError):
    """The experiment cannot be bundled; .findings says why."""

    def __init__(self, findings: list):
        self.findings = findings
        super().__init__("The experiment cannot be bundled:\n  %s"
                         % "\n  ".join(str(each) for each in findings))


class BundleLocked(RuntimeError):
    """Another command held the project's lock past the timeout."""


#: Where the lock is kept, in the output directory.
LOCK_NAME = ".lock"

#: The locks this process holds, by path: [open file, depth], so a holder may lock again.
_HELD = {}


class locked:
    """
    Holds the project's lock while the block runs: one command at a time builds, runs and
    uploads a project's bundle. A command that finds it held says by whom, and waits.

        with locked(project.output_dir):
            bundle = build(experiment, project.output_dir)
            ...run or upload it...

    The same process may take it again inside the block. The lock is the operating system's
    (flock on POSIX, a locked byte on Windows), so a command that dies lets it go.

    Args:
        output_dir (Path): The project's .robotarium directory.
        timeout (float): Seconds to wait before giving up (BundleLocked); None to wait on.
        out: Where to say that it is waiting; default stderr.
    """

    def __init__(self, output_dir, timeout: float = None, out=None):
        self.path = (Path(output_dir) / LOCK_NAME).resolve()
        self.timeout = timeout
        self.out = out

    def __enter__(self):
        held = _HELD.get(self.path)
        if held is not None:
            held[1] += 1
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            if not _try_lock(handle):
                handle.seek(0)
                holder = handle.read().strip() or "another robotarium command"
                print("Waiting for %s to finish with %s (it holds %s)..."
                      % (holder, self.path.parent, self.path.name),
                      file=self.out or sys.stderr, flush=True)
                waited = time.monotonic()
                while not _try_lock(handle):
                    if self.timeout is not None and time.monotonic() - waited > self.timeout:
                        raise BundleLocked("%s is still held by %s." % (self.path, holder))
                    time.sleep(0.2)
            handle.seek(0)
            handle.truncate()
            handle.write("pid %d (%s)\n" % (os.getpid(), " ".join(
                [Path(sys.argv[0]).name] + sys.argv[1:3]) if sys.argv else "python"))
            handle.flush()
        except BaseException:
            handle.close()
            raise
        _HELD[self.path] = [handle, 1]
        return self

    def __exit__(self, *exc):
        held = _HELD.get(self.path)
        if held is None:
            return False
        held[1] -= 1
        if held[1] == 0:
            del _HELD[self.path]
            _unlock(held[0])
            held[0].close()
        return False


def _try_lock(handle) -> bool:
    try:
        import fcntl
    except ImportError:
        fcntl = None
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except ImportError:
        return True  # neither: no lock to take here
    except OSError:
        return False


def _unlock(handle) -> None:
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except ImportError:
        try:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            pass
    except OSError:
        pass


@dataclass
class Bundle:
    """
    A built bundle: what to upload and what to type into the form.

    Args:
        directory (Path): Where the files are.
        files (list): Their paths, in upload order: main first, generated modules last.
        title (str): The rendered title.
        description (str): The description.
        duration: The estimated duration in seconds ("auto" declares the form's maximum), or
            "measured" until a simulation settles it.
        robots (int): The number of robots.
        built_at (str): When, as an ISO timestamp.
        git (str): The commit the files come from, or None outside git.
        generated (list): The base names of the files this package wrote rather than copied.
    """

    directory: Path
    files: list
    title: str
    description: str
    duration: object
    robots: int
    built_at: str
    git: str = None
    generated: list = field(default_factory=list)

    @property
    def main(self) -> Path:
        return self.files[0]

    def hashes(self) -> dict:
        """Each file's base name to its sha256."""
        return {path.name: sha256(path) for path in self.files}


def build(experiment, output_dir: Path, when: datetime.datetime = None) -> Bundle:
    """
    Builds the bundle into output_dir/bundle, replacing the last one, and returns it. Refuses
    (BundleError) when the experiment's own fields or files have errors; the static preflight
    over the built files is preflight.check, run separately.

    Args:
        experiment (Experiment): What to bundle.
        output_dir (Path): The project's .robotarium directory, or wherever it is configured.
        when (datetime.datetime): The build's moment, which the title's placeholders show;
            default now. Rebuilding with the same `when` keeps the same title.
    """
    problems = errors(experiment.check())
    if problems:
        raise BundleError(problems)

    when = when or datetime.datetime.now().astimezone()
    built_at = when.isoformat(timespec="seconds")
    git = git_revision(experiment.root)
    title = experiment.render_title(when=when, git=git or "nogit")

    output_dir = Path(output_dir)
    staging = output_dir / "_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    sources = experiment.upload_files()
    files = []
    for source in sources:
        target = staging / source.name
        shutil.copy2(source, target)
        files.append(target)
    log.debug("copied %d file(s) into %s", len(files), staging)

    generated = []
    if wants_runtime(experiment, files):
        target = staging / RUNTIME_MODULE
        shutil.copy2(runtime_source(), target)
        generated.append(target)

    config = experiment.load_config()
    if generated or config is not None or experiment.julia_packages:
        record = {
            "title": title,
            "description": experiment.description,
            "duration": experiment.duration,
            "robots": experiment.robots,
            "main": sources[0].name,
            "built_at": built_at,
            "git": git,
            "builder": "robotarium %s" % __version__,
            "files": {path.name: sha256(path) for path in files},
        }
        julia = {"version": experiment.julia_version,
                 "packages": [package.as_dict() for package in experiment.julia_packages]}
        target = staging / BUNDLE_MODULE
        target.write_text(bundle_module_source(record, config, julia), encoding="utf-8")
        generated.append(target)

    directory = output_dir / "bundle"
    if directory.exists():
        shutil.rmtree(directory)
    staging.rename(directory)

    duration = limits.MAX_DURATION_SECONDS if experiment.duration == "auto" \
        else experiment.duration
    return Bundle(directory=directory,
                  files=[directory / path.name for path in files + generated],
                  title=title, description=experiment.description,
                  duration=duration, robots=experiment.robots, built_at=built_at,
                  git=git, generated=[path.name for path in generated])


def wants_runtime(experiment, files: list) -> bool:
    """
    Whether to vendor robotarium_runtime.py: as configured, or under "auto" whenever an
    uploaded .py file imports it.

    Args:
        experiment (Experiment): Its `runtime` setting.
        files (list): The copied files.
    """
    if experiment.runtime is True or experiment.runtime is False:
        return experiment.runtime
    if str(experiment.runtime).lower() != "auto":
        raise ValueError("[experiment] runtime must be true, false or \"auto\", not %r"
                         % experiment.runtime)
    for path in files:
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        if any(module == RUNTIME_MODULE[:-3] for module, _, _, _ in imports(tree)):
            return True
    return False


def runtime_source() -> Path:
    """robotarium_runtime.py as installed beside this package."""
    spec = importlib.util.find_spec("robotarium_runtime")
    if spec is None or not spec.origin:
        raise FileNotFoundError("robotarium_runtime.py is not installed with this package; "
                                "reinstall it: pip install -e .")
    return Path(spec.origin)


def bundle_module_source(record: dict, config, julia: dict) -> str:
    """
    The source of robotarium_bundle.py. Checked to be literals only before it is returned, so a
    value that would not survive the trip fails here rather than on the testbed.

    Args:
        record (dict): EXPERIMENT: what was built and from what.
        config: CONFIG: the run's parameters, or None.
        julia (dict): JULIA: {"version", "packages"}.
    """
    parts = [
        '"""\nGenerated by `robotarium bundle` at %s. Do not edit; rebuild instead.\n\n'
        "Read it through robotarium_runtime (bundle(), config()), or import it directly:\n"
        "    from robotarium_bundle import CONFIG\n\"\"\"\n" % record["built_at"],
    ]
    for name, value in (("EXPERIMENT", record), ("CONFIG", config), ("JULIA", julia)):
        rendered = python_literal(value, key_path=name)
        ast.literal_eval(rendered)
        parts.append("%s = %s\n" % (name, rendered))
    source = "\n".join(parts)
    ast.parse(source)
    return source


def python_literal(value, indent_level: int = 0, key_path: str = "config") -> str:
    """
    One value as readable Python literal source. Not pprint, which happily writes values that
    are not literals (datetime.date(2026, 8, 5), nan) and which then fail on import on the
    testbed. Ported from robosheaf's codegen.

    Args:
        value: The value to render.
        indent_level (int): How deep this value sits, in four-space steps.
        key_path (str): Where this value came from, named in any error raised.
    """
    pad = "    " * indent_level
    inner_pad = "    " * (indent_level + 1)

    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("%s: dictionary keys must be strings, but %r is %s."
                                % (key_path, key, type(key).__name__))
            rendered = python_literal(item, indent_level + 1, "%s.%s" % (key_path, key))
            lines.append("%s%s: %s," % (inner_pad, json.dumps(key, ensure_ascii=False),
                                        rendered))
        lines.append("%s}" % pad)
        return "\n".join(lines)

    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        items = [python_literal(item, indent_level + 1, "%s[%d]" % (key_path, index))
                 for index, item in enumerate(value)]
        one_line = "[" + ", ".join(items) + "]"
        if all("\n" not in item for item in items) and len(pad) + len(one_line) <= 79:
            return one_line
        return "[\n%s\n%s]" % ("\n".join("%s%s," % (inner_pad, item) for item in items), pad)

    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("%s: %r is not a Python literal. Use a large finite number or None."
                        % (key_path, value))
    if isinstance(value, (int, float)):
        return repr(value)

    raise ValueError("%s: cannot carry a value of type %s (%r). Quote it in the config if it "
                    "should be a string; an unquoted date, for one, is not a literal."
                    % (key_path, type(value).__name__, value))


def sha256(path: Path) -> str:
    """The file's sha256, in hex."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def slug(text: str) -> str:
    """A safe file-name fragment of `text`."""
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()[:60] or "experiment"

