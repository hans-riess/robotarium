"""
Tooling for the backend.jl method on this machine: finding a package's UUID, loading a bundle's
Julia backend locally exactly as the testbed will, and timing that load from nothing -- a cold
start, as the testbed makes at the beginning of every run.

The method itself -- Julia code in a .jl file uploaded beside the main file, its packages
declared to juliapkg before Julia starts, the file include()d through juliacall -- lives in
robotarium_runtime.load_julia_backend, which is what runs on the testbed. docs/julia.md walks
through it.
"""

import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from .credentials import PASSWORD_VARIABLE, USERNAME_VARIABLE
from .experiment import JuliaPackage

#: The General registry, raw. ROBOTARIUM_JULIA_REGISTRY overrides it (a mirror, or a stand-in
#: under test).
REGISTRY_URL = "https://raw.githubusercontent.com/JuliaRegistries/General/master/"


class JuliaToolError(RuntimeError):
    """A lookup or local load failed; the message says why."""


def lookup(name: str) -> JuliaPackage:
    """
    A package's UUID and latest unyanked version, from Julia's General registry -- what a
    [julia] packages entry needs.

    Args:
        name (str): The registered name, e.g. "JSON3". Case matters, as it does to Julia.
    """
    base = os.environ.get("ROBOTARIUM_JULIA_REGISTRY") or REGISTRY_URL
    base = base if base.endswith("/") else base + "/"
    directory = "%s%s/%s/" % (base, name[:1].upper(), name)
    package = tomllib.loads(_fetch(directory + "Package.toml", name))
    versions = tomllib.loads(_fetch(directory + "Versions.toml", name))
    available = [version for version, entry in versions.items() if not entry.get("yanked")]
    if not available:
        raise JuliaToolError("%s has no unyanked versions in the registry." % name)
    latest = max(available, key=_version_key)
    return JuliaPackage(name=package.get("name", name), uuid=package["uuid"], version=latest)


def toml_entry(package: JuliaPackage) -> str:
    """The package as a line for robotarium.toml's [julia] packages list."""
    return '{ name = "%s", uuid = "%s", version = "%s" },' % (package.name, package.uuid,
                                                             package.version)


def _fetch(url: str, name: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise JuliaToolError("%s is not in the General registry (names are "
                                 "case-sensitive)." % name) from None
        raise JuliaToolError("The registry answered HTTP %d for %s." % (error.code, url)) \
            from None
    except urllib.error.URLError as error:
        raise JuliaToolError("Could not reach the registry: %s" % error.reason) from None


def _version_key(version: str):
    core, _, prerelease = version.partition("-")
    numbers = tuple(int(part) if part.isdigit() else 0 for part in core.split("."))
    return numbers + ((1,) if not prerelease else (0,))


#: Run in the bundle directory by check(): the testbed's own load path, then each --call twice
#: (the first compiles it), then a report.
_CHECK_SCRIPT = r"""
import json, sys, time
started = time.monotonic()
import robotarium_runtime as runtime
backend = runtime.load_julia_backend(sys.argv[1])
from juliacall import Main as jl
names = [str(each) for each in jl.names(backend)] if backend is not jl else []
calls = json.loads(sys.argv[2]) if len(sys.argv) > 2 else []
first, warm = {}, {}
for name, payload in calls:
    function = getattr(backend, name)
    clock = time.monotonic()
    runtime.call_json(function, payload)
    first[name] = round(time.monotonic() - clock, 3)
ready = round(time.monotonic() - started, 3)
for name, payload in calls:
    function = getattr(backend, name)
    clock = time.monotonic()
    runtime.call_json(function, payload)
    warm[name] = round(time.monotonic() - clock, 3)
print("ROBOTARIUM_JULIA_CHECK " + json.dumps({
    "module": str(backend), "names": names, "timings": runtime.julia_timings(),
    "first_calls": first, "warm_calls": warm, "ready_seconds": ready}), flush=True)
"""

_RESULT_PREFIX = "ROBOTARIUM_JULIA_CHECK "

#: Variables a cold start must not inherit: each points Julia or juliapkg at what is already
#: installed here. Proxies, CA bundles, JULIA_PKG_SERVER and the like pass through, since a
#: machine that needs them to reach the package servers needs them for a cold start too.
_WARM_VARIABLES = ("PYTHON_JULIAPKG_EXE", "PYTHON_JULIAPKG_PROJECT", "PYTHON_JULIAPKG_OFFLINE",
                   "PYTHON_JULIACALL_SYSIMAGE", "JULIA_DEPOT_PATH", "JULIA_LOAD_PATH",
                   "JULIA_PROJECT", "JULIAUP_DEPOT_PATH")

#: Where `julia check --cold` keeps its measurement, under the output directory.
COLD_START = Path("julia") / "cold-start.json"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PRECOMPILED = re.compile(r"(\d+) dependenc(?:y|ies) successfully precompiled in "
                          r"(\d+(?:\.\d+)?) seconds")


def parse_call(text: str) -> tuple:
    """
    A --call argument as (function name, payload): "build" calls build with call_json(f, {}),
    'build={"robots": 6}' with that payload.
    """
    name, equals, payload = str(text).partition("=")
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_!]*", name):
        raise JuliaToolError("--call takes a function's name, with =JSON for its argument: "
                             "not %r." % text)
    if not equals:
        return name, {}
    try:
        return name, json.loads(payload)
    except ValueError as error:
        raise JuliaToolError("--call %s: the argument is not JSON (%s)." % (name, error)) \
            from None


def check(bundle, julia_file: str = None, python: str = None, timeout: float = 1800,
          calls=(), environment: dict = None, echo: bool = False) -> dict:
    """
    Loads the bundle's Julia backend here, through robotarium_runtime from the bundle directory,
    as the testbed will -- packages declared before Julia starts, the file include()d by base
    name -- and returns what it found: {"module", "names", "timings", "first_calls",
    "warm_calls", "ready_seconds", "transcript", "elapsed"}. timings is
    robotarium_runtime.julia_timings(); transcript is each line printed, with the seconds since
    the start it was printed at.

    The first run installs Julia and the packages, which takes minutes; later ones reuse them.
    cold_check() times one from nothing.

    Args:
        bundle (Bundle): A built bundle that carries robotarium_runtime.py.
        julia_file (str): Which .jl to load; default the bundle's only one.
        python (str): The interpreter, which needs juliacall; default this one.
        timeout (float): Seconds before giving up.
        calls: (name, payload) pairs (see parse_call) to call after loading, twice each.
        environment (dict): The run's environment; default this one's, without the login.
        echo (bool): Print the output as it comes, each line stamped with its time.
    """
    julia_file = _julia_file(bundle, julia_file)
    if environment is None:
        environment = {key: value for key, value in os.environ.items()
                       if key not in (USERNAME_VARIABLE, PASSWORD_VARIABLE)}
    environment = dict(environment, PYTHONUNBUFFERED="1")
    command = [python or sys.executable, "-c", _CHECK_SCRIPT, julia_file,
               json.dumps([list(each) for each in calls])]
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=bundle.directory, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               encoding="utf-8", errors="replace", bufsize=1)
    transcript = []
    reader = threading.Thread(target=_stamped, args=(process.stdout, transcript, started, echo),
                              daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        reader.join(timeout=5)
        raise JuliaToolError("Loading %s did not finish in %.0f s." % (julia_file, timeout)) \
            from None
    finally:
        if process.poll() is None:
            process.kill()
    reader.join(timeout=30)
    elapsed = time.monotonic() - started
    for _, line in transcript:
        if line.startswith(_RESULT_PREFIX):
            result = json.loads(line[len(_RESULT_PREFIX):])
            result.setdefault("timings", {})
            result.update(transcript=transcript, elapsed=round(elapsed, 3))
            return result
    output = [line for _, line in transcript]
    # Julia's own "ERROR:" lines say what went wrong; the Python traceback after them only
    # says where it was noticed.
    julia_errors = [line for line in output if line.startswith("ERROR:")]
    last = [line for line in output if line.strip()][-1:] or ["no output"]
    raise JuliaToolError("Loading %s failed:\n  %s" % (
        julia_file, "\n  ".join(julia_errors + last)))


def _julia_file(bundle, julia_file):
    julia_files = [path.name for path in bundle.files if path.suffix == ".jl"]
    if julia_file is None:
        if len(julia_files) != 1:
            raise JuliaToolError("The bundle has %s; name the one to load."
                                 % (", ".join(julia_files) or "no .jl file"))
        julia_file = julia_files[0]
    if not (bundle.directory / "robotarium_runtime.py").is_file():
        raise JuliaToolError("The bundle does not carry robotarium_runtime.py: import it from "
                             "the main file, or set [experiment] runtime = true.")
    return julia_file


def _stamped(stream, transcript: list, started: float, echo: bool) -> None:
    """Keeps each line with the seconds since `started`, printing it so stamped when echo."""
    for line in stream:
        text = _ANSI.sub("", line.rstrip("\r\n"))
        seconds = time.monotonic() - started
        transcript.append((round(seconds, 3), text))
        if echo and not text.startswith(_RESULT_PREFIX):
            try:
                sys.stdout.write("[%7.1f s] %s\n" % (seconds, text))
                sys.stdout.flush()
            except (OSError, ValueError, UnicodeError):
                echo = False


def cold_environment(scratch: Path, keep_julia: bool = False, cpus: int = None,
                     python: str = None, base: dict = None) -> dict:
    """
    The environment of a cold start in `scratch`: a Julia depot, juliapkg project and juliaup
    depot of its own, and no julia or juliaup on PATH, so nothing installed here is found.
    juliapkg reads these once, when it is imported, so they are set for the child process.

    Args:
        scratch (Path): An empty directory for all of it.
        keep_julia (bool): Use the Julia installed here (PYTHON_JULIAPKG_EXE) rather than
            download one: the packages are still installed from nothing.
        cpus (int): Precompile with this many tasks (JULIA_NUM_PRECOMPILE_TASKS), as a smaller
            machine would.
        python (str): The interpreter whose juliapkg finds the installed Julia, for keep_julia.
        base (dict): The environment to start from; default this one's, without the login.
    """
    environment = {key: value for key, value in (os.environ if base is None else base).items()
                   if key not in (USERNAME_VARIABLE, PASSWORD_VARIABLE)
                   and key not in _WARM_VARIABLES}
    scratch = Path(scratch).resolve()
    environment["JULIA_DEPOT_PATH"] = str(scratch / "depot")
    environment["PYTHON_JULIAPKG_PROJECT"] = str(scratch / "project")
    environment["JULIAUP_DEPOT_PATH"] = str(scratch / "juliaup")
    names = ("julia", "julia.exe", "juliaup", "juliaup.exe")
    environment["PATH"] = os.pathsep.join(
        directory for directory in environment.get("PATH", "").split(os.pathsep)
        if directory and not any((Path(directory) / name).exists() for name in names))
    if cpus:
        environment["JULIA_NUM_PRECOMPILE_TASKS"] = str(int(cpus))
    if keep_julia:
        environment["PYTHON_JULIAPKG_EXE"] = local_julia(python, base)
    return environment


def local_julia(python: str = None, base: dict = None) -> str:
    """
    The Julia executable juliapkg uses here: $PYTHON_JULIAPKG_EXE, else the one its project
    records (installed by an earlier `robotarium julia check`), else julia on PATH.
    """
    environment = dict(os.environ if base is None else base)
    configured = environment.get("PYTHON_JULIAPKG_EXE")
    if configured and Path(configured).is_file():
        return configured
    try:
        found = subprocess.run(
            [python or sys.executable, "-c", "import json, juliapkg.state as s; "
             "print(json.dumps([s.STATE['meta'], s.STATE['install']]))"],
            env=environment, capture_output=True, text=True, timeout=60)
        meta, install = json.loads(found.stdout.strip().splitlines()[-1])
        try:
            executable = json.loads(Path(meta).read_text(encoding="utf-8")).get("executable")
        except (OSError, ValueError):
            executable = None
        for candidate in (executable, str(Path(install) / "bin" / "julia")):
            if candidate and Path(candidate).is_file():
                return candidate
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    on_path = shutil.which("julia", path=environment.get("PATH"))
    if on_path:
        return on_path
    raise JuliaToolError("--keep-julia found no Julia here: run `robotarium julia check` once "
                         "to install one, or leave out --keep-julia.")


def phases(transcript: list) -> dict:
    """
    How the install split, from juliapkg's and Pkg's own lines in a transcript of
    (seconds, line) pairs: {"julia": installing Julia, "packages": the registry, downloads
    and resolve, "precompile": precompiling, "precompiled": how many packages}. A phase that
    did not show (Julia already there, nothing to precompile) is None.
    """
    def first(pattern, after=0.0):
        for seconds, line in transcript:
            if seconds >= after and re.search(pattern, line):
                return seconds
        return None

    found = {"julia": None, "packages": None, "precompile": None, "precompiled": None,
             "precompile_reported": None}
    locating = first(r"\[juliapkg\] Locating Julia")
    using = first(r"\[juliapkg\] Using Julia ", locating or 0.0)
    if locating is not None and using is not None:
        found["julia"] = round(using - locating, 3)
    installing = first(r"\[juliapkg\] Installing packages:")
    precompiling = first(r"Precompiling", installing or 0.0) if installing is not None else None
    if installing is not None and precompiling is not None:
        found["packages"] = round(precompiling - installing, 3)
    for seconds, line in transcript:
        match = _PRECOMPILED.search(line)
        if match and (precompiling is None or seconds >= precompiling):
            found["precompiled"] = int(match.group(1))
            found["precompile_reported"] = float(match.group(2))
            if precompiling is not None:
                found["precompile"] = round(seconds - precompiling, 3)
    return found


def cold_check(bundle, experiment, output_dir, julia_file: str = None, python: str = None,
               timeout: float = 3600, calls=(), keep_julia: bool = False, cpus: int = None,
               scratch=None, keep_scratch: bool = False, echo: bool = True) -> dict:
    """
    Times a cold start of the bundle's Julia backend: check() run with a depot and a juliapkg
    project of its own (see cold_environment), so that Julia, the registry and every package
    are downloaded, resolved and precompiled from nothing, as on the testbed. Saves the result
    to <output_dir>/julia/cold-start.json and returns it.

    Args:
        bundle (Bundle): A built bundle that carries robotarium_runtime.py.
        experiment (Experiment): Its [julia] table, for the key the result is filed under.
        output_dir (Path): The project's .robotarium directory.
        julia_file, python, calls: As check() takes them.
        timeout (float): Seconds before giving up.
        keep_julia (bool): Reuse the Julia installed here instead of downloading one.
        cpus (int): Precompile with this many tasks.
        scratch (Path): An empty directory to install into; default a temporary one.
        keep_scratch (bool): Leave scratch in place afterwards, rather than delete it.
        echo (bool): Print the output as it comes, stamped with its time.
    """
    from robotarium_runtime import julia_key

    if scratch is None:
        scratch = Path(tempfile.mkdtemp(prefix="robotarium-julia-cold-"))
    else:
        scratch = Path(scratch).resolve()
        if scratch.exists() and any(scratch.iterdir()):
            raise JuliaToolError("%s is not empty, so a start there would not be cold. Name an "
                                 "empty or new directory." % scratch)
        scratch.mkdir(parents=True, exist_ok=True)
    try:
        environment = cold_environment(scratch, keep_julia=keep_julia, cpus=cpus, python=python)
        result = check(bundle, julia_file, python=python, timeout=timeout, calls=calls,
                       environment=environment, echo=echo)
    finally:
        if not keep_scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    timings = result.get("timings") or {}
    found = phases(result["transcript"])
    packages = [package.as_dict() for package in experiment.julia_packages]
    juliacall_version, juliapkg_version = timings.get("juliacall"), timings.get("juliapkg")
    record = {
        "measured_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "key": julia_key(experiment.julia_version, packages, juliacall_version,
                         juliapkg_version),
        "file": _julia_file(bundle, julia_file),
        "julia": experiment.julia_version,
        "julia_version": timings.get("julia_version"),
        "packages": packages,
        "juliacall": juliacall_version,
        "juliapkg": juliapkg_version,
        "keep_julia": bool(keep_julia),
        "precompile_tasks": cpus,
        "cpus": os.cpu_count(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "phases": dict(found, resolve=timings.get("resolve_seconds"),
                       start=timings.get("start_seconds"),
                       include=timings.get("include_seconds"),
                       first_calls=result.get("first_calls") or {},
                       warm_calls=result.get("warm_calls") or {}),
        "startup_seconds": result.get("ready_seconds"),
        "total_seconds": result.get("elapsed"),
        "scratch": str(scratch) if keep_scratch else None,
    }
    path = Path(output_dir) / COLD_START
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record["path"] = str(path)
    record["module"], record["names"] = result.get("module"), result.get("names")
    return record


def describe_cold_start(record: dict) -> str:
    """The phases of a cold start as a small table, for `robotarium julia check --cold`."""
    phase = record.get("phases") or {}
    rows = [("installing Julia", phase.get("julia"), "reused, --keep-julia"
             if record.get("keep_julia") else ""),
            ("registry, downloads, resolve", phase.get("packages"), ""),
            ("precompiling", phase.get("precompile"),
             "%s packages; Pkg says %.0f s" % (phase.get("precompiled"),
                                                phase.get("precompile_reported"))
             if phase.get("precompiled") is not None else ""),
            ("juliapkg in all", phase.get("resolve"), ""),
            ("starting Julia (juliacall)", phase.get("start"), ""),
            ("include %s" % record.get("file"), phase.get("include"), "")]
    for name, seconds in (phase.get("first_calls") or {}).items():
        warm = (phase.get("warm_calls") or {}).get(name)
        rows.append(("first %s()" % name, seconds,
                     "then %.3f s warm" % warm if warm is not None else ""))
    rows.append(("ready, after", record.get("startup_seconds"), ""))
    lines = ["Cold start of %s (Julia %s, %s CPUs%s): %.0f s in all" % (
        record.get("file"), record.get("julia_version") or "?", record.get("cpus"),
        ", %s precompile tasks" % record["precompile_tasks"]
        if record.get("precompile_tasks") else "", record.get("total_seconds") or 0)]
    for name, seconds, note in rows:
        shown = "%8.1f s" % seconds if seconds is not None else "       - "
        lines.append("  %-30s %s%s" % (name, shown, "   (%s)" % note if note else ""))
    return "\n".join(lines)
