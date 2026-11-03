"""
How long the testbed spends on Julia before an experiment's first step, and so whether the run
fits its declared duration.

The testbed installs Julia and the [julia] packages at the start of every run that loads a
backend.jl (it keeps no depot between runs, as far as can be told), then starts Julia and loads
the file, and only then does the main file create its Robotarium. That startup counts against
the declared duration as much as the steps do. robosheaf's run with CellularSheaves.jl began to
move about 171 s into the testbed's video.

startup_estimate() takes the best figure there is, in this order:

    1. a timing fetched from the testbed         julia_timing.json under .robotarium/results/,
                                                 for the same Julia and packages, x 1.1; or
                                                 [julia] startup, when that states more
    2. [julia] startup = 240 (or "4m")           as stated
    3. a cold start measured here                .robotarium/julia/cold-start.json, written by
                                                 `robotarium julia check --cold`, x 1.5; or as
                                                 measured, as an upper bound, when it was
                                                 measured without Julia's package servers
    4. nothing measured                          an assumed 180 s, with a warning

and applies only to a bundle with a .jl file or [julia] packages. budget_findings() turns the
estimate, and the last simulation's steps, into the duration checks `check` and `submit` make
without simulating.
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from . import limits
from .findings import ERROR, INFO, WARNING, Finding

#: Where `robotarium julia check --cold` keeps its measurement, under the output directory.
COLD_START = Path("julia") / "cold-start.json"

#: What results.fetch writes beside each experiment's results, from its log.txt.
TESTBED_TIMING = "julia_timing.json"


@dataclass
class StartupEstimate:
    """
    The Julia startup the duration checks count.

    Args:
        seconds (float): The estimate, margin included.
        source (str): "stated", "testbed", "cold-start" or "assumed".
        detail (str): Where it comes from, as a phrase for the budget line.
        findings (list): What to tell about it: an INFO, or a WARNING when it is assumed.
        upper_bound (bool): The startup takes at most this: a cold start measured here
            without Julia's package servers, every package a git clone. What it alone pushes
            past a limit is then a WARNING, not an ERROR.
    """

    seconds: float
    source: str
    detail: str
    findings: list = field(default_factory=list)
    upper_bound: bool = False


def uses_julia(experiment, bundle=None) -> bool:
    """Whether the bundle (or the experiment's files) has a .jl file or [julia] packages."""
    if experiment.julia_packages:
        return True
    paths = bundle.files if bundle is not None else experiment.upload_files()
    return any(Path(path).suffix.lower() == ".jl" for path in paths)


def testbed_versions() -> tuple:
    """The juliacall and juliapkg versions the testbed has, from its libraries.txt."""
    from . import libraries
    found = libraries.load()
    return found.version("juliacall"), found.version("juliapkg")


def local_versions() -> tuple:
    """The juliacall and juliapkg versions installed here (None for one that is not)."""
    from importlib import metadata
    versions = []
    for name in ("juliacall", "juliapkg"):
        try:
            versions.append(metadata.version(name))
        except metadata.PackageNotFoundError:
            versions.append(None)
    return tuple(versions)


def project_key(experiment, juliacall_version, juliapkg_version) -> str:
    """robotarium_runtime.julia_key for the experiment's [julia] table."""
    from robotarium_runtime import julia_key
    return julia_key(experiment.julia_version,
                     [package.as_dict() for package in experiment.julia_packages],
                     juliacall_version, juliapkg_version)


def testbed_timing(project):
    """
    The newest julia_timing.json fetched for an experiment with the project's Julia and
    packages, as (path, timing); None when there is none.
    """
    key = project_key(project.experiment, *testbed_versions())
    results = Path(project.output_dir) / "results"
    found = []
    for path in results.glob("*/%s" % TESTBED_TIMING):
        try:
            timing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(timing, dict) and timing.get("key") == key \
                and timing.get("total_seconds") is not None:
            found.append((path.stat().st_mtime, path, timing))
    if not found:
        return None
    _, path, timing = max(found, key=lambda each: each[0])
    return path, timing


def cold_start(project):
    """
    The cold start `julia check --cold` measured for the project, as (path, record, matches):
    matches is False when it was measured with other packages or Julia. None without one.
    """
    path = Path(project.output_dir) / COLD_START
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    versions = (record.get("juliacall"), record.get("juliapkg"))
    matches = record.get("key") == project_key(project.experiment, *versions)
    return path, record, matches


def startup_estimate(project, bundle=None):
    """
    The testbed's Julia startup for the project, as a StartupEstimate; None when the bundle
    loads no Julia. See the module docstring for where the figure comes from.

    Args:
        project (Project): The experiment and its output directory.
        bundle (Bundle): The built bundle, whose files say whether there is a .jl; default
            the experiment's files.
    """
    experiment = project.experiment
    if not uses_julia(experiment, bundle):
        return None

    stated = experiment.julia_startup
    if not isinstance(stated, int) or isinstance(stated, bool):
        stated = None
    margin = limits.TESTBED_TIMING_MARGIN

    fetched = testbed_timing(project)
    if fetched:
        path, timing = fetched
        measured = float(timing["total_seconds"])
        seconds = measured * margin
        timed = "%.0f s as timed there (%s) x %.1f" % (measured, path.parent.name, margin)
        if stated is not None and stated >= seconds:
            return StartupEstimate(float(stated), "stated", "[julia] startup = %d" % stated, [
                Finding(INFO, "Julia startup on the testbed: %d s, as [julia] startup states; "
                              "about %.0f s, %s, is less." % (stated, seconds, timed))])
        findings = [Finding(INFO, "Julia startup on the testbed: about %.0f s, %s."
                                  % (seconds, timed))]
        if stated is not None:
            findings.append(Finding(WARNING, "[julia] startup = %d is less than the testbed's "
                                             "own timing, about %.0f s (%s), so the duration "
                                             "checks count the timing. Raise [julia] startup "
                                             "to at least %d, or leave it out to follow the "
                                             "testbed's timings."
                                    % (stated, seconds, timed, math.ceil(seconds))))
        return StartupEstimate(seconds, "testbed", "%.0f s timed on the testbed x %.1f"
                               % (measured, margin), findings)

    if stated is not None:
        return StartupEstimate(float(stated), "stated", "[julia] startup = %d" % stated, [
            Finding(INFO, "Julia startup on the testbed: %d s, as [julia] startup states."
                          % stated)])

    findings = []
    local = cold_start(project)
    if local:
        path, record, matches = local
        if matches and record.get("startup_seconds") is not None:
            return _cold_estimate(path, record)
        findings.append(Finding(INFO, "%s was measured with other Julia packages or versions "
                                      "than [julia] now declares, so it is not used; run "
                                      "`robotarium julia check --cold` again." % path))

    seconds = float(limits.ASSUMED_JULIA_STARTUP)
    # Under "auto" the form's maximum is declared, and only a run that comes close to it
    # depends on the figure (the duration checks warn about that one); otherwise the declared
    # duration rests on it.
    level = INFO if experiment.duration == "auto" else WARNING
    findings.append(Finding(level, "Julia startup on the testbed is assumed to take %d s "
                                   "(installing Julia and the packages, then loading the "
                                   "backend), since nothing has measured it. Measure a cold "
                                   "start here with `robotarium julia check --cold`, or state "
                                   "it as [julia] startup." % limits.ASSUMED_JULIA_STARTUP))
    return StartupEstimate(seconds, "assumed", "%d s assumed" % limits.ASSUMED_JULIA_STARTUP,
                           findings)


def _cold_estimate(path, record) -> StartupEstimate:
    """A cold start measured here, as the testbed's startup."""
    measured = float(record["startup_seconds"])
    when = record.get("measured_at", path.name)
    findings = []
    juliacall, juliapkg = testbed_versions()
    if (juliacall and record.get("juliacall") != juliacall) \
            or (juliapkg and record.get("juliapkg") != juliapkg):
        findings.append(Finding(WARNING, "The cold start in %s was measured with juliacall %s "
                                         "and juliapkg %s, and the testbed has %s and %s "
                                         "(libraries.txt), which may install and start Julia "
                                         "in another time. pip install those, and measure "
                                         "again." % (path, record.get("juliacall"),
                                                     record.get("juliapkg"), juliacall,
                                                     juliapkg)))
    if record.get("pkg_server") == "":
        # Every package a git clone, and the registry too: far slower than the package
        # servers the testbed's Pkg uses, so the measurement bounds the testbed's from above.
        findings.insert(0, Finding(INFO, "Julia startup on the testbed: at most about %.0f s, "
                                         "the cold start here (%s), which installed every "
                                         "package by git clone with Julia's package servers "
                                         "off (JULIA_PKG_SERVER is empty); the testbed's Pkg "
                                         "uses them and is faster. Fetching a testbed run's "
                                         "results replaces it with the testbed's own figure."
                                   % (measured, when)))
        return StartupEstimate(measured, "cold-start", "%.0f s cold here, by git clone, at "
                               "most" % measured, findings, upper_bound=True)
    seconds = measured * limits.JULIA_COLD_MARGIN
    findings.insert(0, Finding(INFO, "Julia startup on the testbed: about %.0f s, the %.0f s a "
                                     "cold start took here (%s) x %.1f. Fetching a testbed "
                                     "run's results replaces it with the testbed's own figure."
                               % (seconds, measured, when, limits.JULIA_COLD_MARGIN)))
    return StartupEstimate(seconds, "cold-start", "%.0f s cold here x %.1f"
                           % (measured, limits.JULIA_COLD_MARGIN), findings)


def budget_findings(project, bundle, estimate=None) -> list:
    """
    The duration checks without simulating, as `check` and a `submit` that does not simulate
    make them: the startup estimate's own findings, then the last simulation of these same
    files and config judged with it (simulate.last_simulation), or, without one, whether the
    startup alone already takes what the duration allows. The steps can only add to it.

    Args:
        project (Project): The experiment and its output directory.
        bundle (Bundle): The built bundle.
        estimate (StartupEstimate): The startup; default startup_estimate(project, bundle).
    """
    from . import simulate

    if estimate is None:
        estimate = startup_estimate(project, bundle)
    found = list(estimate.findings) if estimate else []
    experiment = project.experiment
    last = simulate.last_simulation(bundle, project.output_dir, experiment.load_config())
    if last is not None:
        report = simulate.SimulationReport(returncode=0, rps="(last simulation)",
                                           init_steps=last["init_steps"], steps=last["steps"])
        return found + [each for each in report.duration_findings(
            experiment, estimate.seconds if estimate else 0.0,
            upper_bound=bool(estimate and estimate.upper_bound)) if each.level != INFO]
    if not estimate:
        return found

    level = WARNING if estimate.upper_bound else ERROR
    seconds, duration = estimate.seconds, experiment.duration
    about = "at most about" if estimate.upper_bound else "about"
    maximum = limits.MAX_DURATION_SECONDS
    if isinstance(duration, int) and not isinstance(duration, bool) and seconds >= duration:
        found.append(Finding(level, "Julia's startup alone, %s %.0f s (%s), takes all of the "
                                    "%d s declared, before the robots' first step; the testbed "
                                    "stops a run that outlasts its declared duration. %s"
                             % (about, seconds, estimate.detail, duration,
                                "Declare more, or \"auto\" (the form's maximum); `robotarium "
                                "simulate` counts the steps." if seconds < maximum else
                                "No declared duration fits it: the form's maximum is %d s."
                                % maximum)))
    elif duration in ("auto", "measured") and seconds >= maximum:
        found.append(Finding(level, "Julia's startup alone, %s %.0f s (%s), is over the "
                                    "form's %d s maximum, before the robots' first step, so "
                                    "no declared duration fits the run."
                             % (about, seconds, estimate.detail, maximum)))
    return found


def budget_line(estimate, run_seconds: float, duration, steps: int = None) -> str:
    """
    The startup and the steps against the declared duration, as one line:

        Budget: 270 s Julia startup (180 s cold here x 1.5) + 95 s robots (2879 steps) =
        365 s, against 600 s declared ("auto").
    """
    startup = estimate.seconds if estimate else 0.0
    declared = limits.MAX_DURATION_SECONDS if duration == "auto" else duration
    parts = []
    if estimate:
        parts.append("%.0f s Julia startup (%s)" % (startup, estimate.detail))
    parts.append("%.0f s robots%s" % (run_seconds, " (%d steps)" % steps if steps else ""))
    total = startup + run_seconds
    against = ("against %s s declared%s" % (declared, " (\"auto\")" if duration == "auto"
                                            else "") if isinstance(declared, int)
               else "with the duration \"measured\": %d s would be declared"
               % limits.suggested_duration(steps or 0, extra_seconds=startup))
    if total > limits.MAX_DURATION_SECONDS:
        against += ", and the form's maximum is %d s" % limits.MAX_DURATION_SECONDS
    return "Budget: %s = %.0f s, %s." % (" + ".join(parts), total, against)
