"""
How long the testbed spends on Julia before an experiment's first step, and so whether the run
fits its declared duration.

The testbed installs Julia and the [julia] packages at the start of every run that loads a
backend.jl (it keeps no depot between runs, as far as can be told), then starts Julia and loads
the file, and only then does the main file create its Robotarium. That startup counts against
the declared duration as much as the steps do. robosheaf's run with CellularSheaves.jl began to
move about 171 s into the testbed's video.

startup_estimate() takes the best figure there is, in this order:

    1. [julia] startup = 240 (or "4m")          as stated
    2. a timing fetched from the testbed         julia_timing.json under .robotarium/results/,
                                                 for the same Julia and packages, x 1.1
    3. a cold start measured here                .robotarium/julia/cold-start.json, written by
                                                 `robotarium julia check --cold`, x 1.5
    4. nothing measured                          an assumed 180 s, with a warning

and applies only to a bundle with a .jl file or [julia] packages.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import limits
from .findings import INFO, WARNING, Finding

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
    """

    seconds: float
    source: str
    detail: str
    findings: list = field(default_factory=list)


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
    if isinstance(stated, int) and not isinstance(stated, bool):
        detail = "[julia] startup = %d" % stated
        return StartupEstimate(float(stated), "stated", detail, [Finding(
            INFO, "Julia startup on the testbed: %d s, as [julia] startup states." % stated)])

    fetched = testbed_timing(project)
    if fetched:
        path, timing = fetched
        measured = float(timing["total_seconds"])
        seconds = measured * limits.TESTBED_TIMING_MARGIN
        detail = "%.0f s timed on the testbed x %.1f" % (measured, limits.TESTBED_TIMING_MARGIN)
        return StartupEstimate(seconds, "testbed", detail, [Finding(
            INFO, "Julia startup on the testbed: about %.0f s, %.0f s as timed there (%s) x "
                  "%.1f." % (seconds, measured, path.parent.name,
                             limits.TESTBED_TIMING_MARGIN))])

    findings = []
    local = cold_start(project)
    if local:
        path, record, matches = local
        if matches and record.get("startup_seconds") is not None:
            measured = float(record["startup_seconds"])
            seconds = measured * limits.JULIA_COLD_MARGIN
            detail = "%.0f s cold here x %.1f" % (measured, limits.JULIA_COLD_MARGIN)
            return StartupEstimate(seconds, "cold-start", detail, [Finding(
                INFO, "Julia startup on the testbed: about %.0f s, the %.0f s a cold start "
                      "took here (%s) x %.1f. Fetching a testbed run's results replaces it "
                      "with the testbed's own figure." % (
                          seconds, measured, record.get("measured_at", path.name),
                          limits.JULIA_COLD_MARGIN))])
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
