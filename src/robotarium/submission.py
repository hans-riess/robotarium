"""
Submitting an experiment, start to finish, with every check in front of the network:

    1. bundle      the files laid out flat, as the testbed will see them (bundle.py)
    2. preflight   the fields, the files, and what is inside them (experiment.py, preflight.py)
    3. simulate    optionally, and always for a duration of "measured" (simulate.py)
    4. the plan    exactly what is about to be sent, printed, and a yes waited for
    5. send        log in, check the site's live limits, upload each file, create the
                   experiment, mark the main file
    6. ledger      the attempt recorded and the files snapshotted, whatever the outcome (ledger.py)

Nothing goes over the network before step 5, and nothing at all with an error from steps 2-3.

    from robotarium import load_project, submit
    result = submit(load_project("."), draft=True)
    print(result.experiment_id)
"""

import dataclasses
import datetime
from dataclasses import dataclass, field
from pathlib import Path

from . import credentials as credentials_module
from . import ledger, libraries, preflight, simulate
from .bundle import Bundle, build
from .client import (DASHBOARD_URL, RobotariumClient, RobotariumError, check_against_settings,
                     experiment_form)
from .experiment import Experiment, Project
from .findings import ERROR, WARNING, Finding, errors, ordered
from .limits import EXPERIMENT_TYPE


class SubmissionRefused(RuntimeError):
    """Checks found errors (or, under strict, warnings), so nothing was sent."""

    def __init__(self, findings: list):
        self.findings = findings
        super().__init__("Not submitted:\n  %s" % "\n  ".join(str(each) for each in findings))


@dataclass
class Prepared:
    """A built, checked and possibly simulated experiment, ready to be shown and sent."""

    project: Project
    experiment: Experiment
    bundle: Bundle
    findings: list
    report: object = None


@dataclass
class SubmissionResult:
    """
    How a submission went.

    Args:
        sent (bool): Whether anything was sent; False after a "no" or a dry run.
        experiment_id (str): The site's id for the new experiment, when sent.
        draft (bool): Whether it was saved as a draft rather than queued.
        prepared (Prepared): The bundle, findings and simulation behind it.
        snapshot (Path): The zip of what was sent.
        ledger (Path): The ledger the attempt was recorded in.
    """

    sent: bool
    prepared: Prepared
    draft: bool = False
    experiment_id: str = None
    snapshot: Path = None
    ledger: Path = None
    uploaded: list = field(default_factory=list)


def as_project(target) -> Project:
    """A Project from a Project, an Experiment, or a path to (or under) a robotarium.toml."""
    if isinstance(target, Project):
        return target
    if isinstance(target, Experiment):
        return Project(experiment=target)
    from .experiment import load_project
    return load_project(target)


def prepare(target, simulate_first: bool = None, echo: bool = True,
            libraries_file=None) -> Prepared:
    """
    Builds the bundle and runs every check that needs no network. Raises BundleError when the
    experiment cannot even be bundled; otherwise the findings are in the result.

    Args:
        target: A Project, an Experiment, or a path to a robotarium.toml.
        simulate_first (bool): Run the simulator; default as [submit] simulate says, and always
            when the duration is "measured".
        echo (bool): Show the simulator's output as it runs.
        libraries_file: A libraries.txt to check imports against instead of the default.
    """
    project = as_project(target)
    experiment = project.experiment
    when = datetime.datetime.now().astimezone()
    bundle = build(experiment, project.output_dir, when=when)

    report = None
    findings = []
    wants_simulation = simulate_first or (simulate_first is None and project.simulate_first) \
        or experiment.duration == "measured"
    if experiment.duration == "measured" and simulate_first is False:
        findings.append(Finding(ERROR, "The duration is \"measured\", which only a simulation "
                                       "can settle; give a duration, or let it simulate."))
    elif wants_simulation and bundle.main.suffix != ".py":
        findings.append(Finding(ERROR, "Only a Python main file can be simulated here, and %s "
                                       "is not one; give a duration and --no-simulate."
                                % bundle.main.name))
    elif simulate_first or (simulate_first is None and project.simulate_first) \
            or experiment.duration == "measured":
        report = simulate.run(bundle, simulator=project.simulator_path,
                              fast=project.simulator_fast, headless=project.simulator_headless,
                              timeout=project.simulator_timeout,
                              log_dir=Path(project.output_dir) / "runs", echo=echo)
        if experiment.duration == "measured":
            if errors(report.findings()):
                findings.append(Finding(ERROR, "The duration is \"measured\", and the simulation "
                                               "that would settle it failed."))
            else:
                experiment = dataclasses.replace(experiment,
                                                 duration=report.suggested_duration)
                bundle = build(experiment, project.output_dir, when=when)
        findings.extend(report.findings(experiment))

    findings.extend(experiment.check())
    environment = libraries.load(libraries_file, simulator_path=project.simulator_path)
    findings.extend(preflight.check(bundle.files, experiment, environment))
    return Prepared(project=project, experiment=experiment, bundle=bundle,
                    findings=ordered(_unique(findings)), report=report)


def plan(prepared: Prepared, draft: bool, account: str = None) -> str:
    """
    Exactly what submit() is about to send, as text.

    Args:
        prepared (Prepared): What prepare returned.
        draft (bool): Whether it will be saved as a draft rather than queued.
        account (str): Who it will be sent as, already masked; None to leave it out.
    """
    bundle = prepared.bundle
    lines = ["About to log in to %s%s and send:" % (
        RobotariumClient().url, " as %s" % account if account else ""), "",
        "Files, from %s:" % bundle.directory]
    hashes = bundle.hashes()
    for path in bundle.files:
        tag = "MAIN FILE" if path == bundle.main else \
            "generated" if path.name in bundle.generated else ""
        lines.append("  %8.1f KB  %-30s %-9s  sha256 %s" % (
            path.stat().st_size / 1e3, path.name, tag, hashes[path.name][:16]))
    lines += ["", "Experiment, %s:" % ("SAVED AS A DRAFT, not queued" if draft
                                       else "SUBMITTED to the testbed queue"),
              "  Title                %s" % bundle.title,
              "  Estimated Duration   %s s" % bundle.duration,
              "  Number of Robots     %d" % bundle.robots,
              "  Description          %s" % _indent(bundle.description, 23),
              "  Type                 %s" % EXPERIMENT_TYPE]
    shown = [each for each in prepared.findings if each.level in (ERROR, WARNING)]
    if shown:
        lines += ["", "Findings:"] + ["  %s" % each for each in shown]
    return "\n".join(lines)


def submit(target, credentials=None, draft: bool = None, simulate_first: bool = None,
           confirm=True, dry_run: bool = False, strict: bool = False, out=print,
           env_file=None, libraries_file=None) -> SubmissionResult:
    """
    Bundles, checks, shows, confirms and sends an experiment, and records the attempt.

    Args:
        target: A Project, an Experiment, or a path to a robotarium.toml.
        credentials (Credentials): Who to log in as; default credentials.resolve().
        draft (bool): Save rather than queue; default as [submit] draft says.
        simulate_first (bool): Run the simulator first; see prepare.
        confirm: True to ask on the terminal, False to send without asking, or a function
            given the plan's text that returns whether to send.
        dry_run (bool): Do everything up to the plan and send nothing.
        strict (bool): Treat warnings as errors.
        out: Where to write progress; print by default.
        env_file: A dotenv file to read the credentials from; see credentials.resolve.
        libraries_file: A libraries.txt to check imports against instead of the default.
    """
    prepared = prepare(target, simulate_first=simulate_first, libraries_file=libraries_file)
    project, bundle = prepared.project, prepared.bundle
    draft = project.draft if draft is None else bool(draft)

    blocking = [each for each in prepared.findings
                if each.level == ERROR or (strict and each.level == WARNING)]
    if blocking:
        raise SubmissionRefused(blocking)

    if not dry_run and credentials is None:
        credentials = credentials_module.resolve(project_root=project.experiment.root,
                                                 env_file=env_file)
    if credentials is not None:
        for warning in credentials.warnings:
            out("WARNING: %s" % warning)

    text = plan(prepared, draft,
                account=credentials_module.mask(credentials.username) if credentials else None)
    out(text)
    if dry_run:
        out("\nDry run: nothing sent.")
        return SubmissionResult(sent=False, prepared=prepared, draft=draft)

    if confirm is True:
        try:
            answer = input("\nSend this to the Robotarium? [y/N] ")
        except EOFError:
            answer = ""
        go = answer.strip().lower() in ("y", "yes")
    elif callable(confirm):
        go = bool(confirm(text))
    else:
        go = True
    if not go:
        out("Nothing sent.")
        return SubmissionResult(sent=False, prepared=prepared, draft=draft)

    snapshot = ledger.snapshot(bundle, project.output_dir)
    entry = _ledger_entry(prepared, draft, snapshot)
    client = RobotariumClient()
    uploaded = []
    experiment_id = None
    stage = "login"
    try:
        client.login(credentials.username, credentials.password)
        out("Logged in.")

        stage = "settings"
        settings = client.settings()
        # "auto" declares the maximum: the site's, should it ever be lower than the form's.
        live_maximum = settings.get("maxDuration")
        if prepared.experiment.duration == "auto" and live_maximum \
                and int(live_maximum) < bundle.duration:
            bundle.duration = int(live_maximum)
            entry["duration"] = bundle.duration
            out("Estimated duration: %d s, the site's current maximum." % bundle.duration)
        problems = check_against_settings(settings, int(bundle.duration), bundle.robots,
                                          bundle.files)
        if problems:
            raise RobotariumError("the site's current limits refuse it: %s" % " ".join(problems))

        stage = "upload"
        for path in bundle.files:
            uploaded.append(client.upload_file(path))
            out("Uploaded %s." % path.name)

        stage = "create"
        experiment_id = client.create_experiment(experiment_form(
            bundle.title, bundle.description, int(bundle.duration), bundle.robots, uploaded,
            draft=draft))

        stage = "mark-main"
        client.mark_main_file(uploaded[0], uploaded, experiment_id)
        out("Marked %s as the main file." % bundle.main.name)
    except BaseException as error:
        # Whatever stopped it -- a refusal, a dropped connection, Ctrl-C -- the attempt is
        # recorded, with what is left on the site, before the error goes on up.
        left, consequence = _consequence(stage, error, uploaded, experiment_id)
        entry.update(outcome="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                     stage=stage, error=_ledger_error(error),
                     orphaned_uploads=len(uploaded) if left == "orphaned" else 0,
                     experiment_id=experiment_id, consequence=consequence)
        ledger.record(project.output_dir, entry)
        if consequence and isinstance(error, RobotariumError):
            error.args = ("%s. %s" % (error, consequence),)
        elif consequence:
            out(consequence)
        raise

    entry.update(outcome="submitted", experiment_id=experiment_id)
    path = ledger.record(project.output_dir, entry)
    out("\nExperiment %s %s: %s" % (experiment_id, "saved as a draft" if draft else "submitted",
                                    bundle.title))
    out("See it at %s" % DASHBOARD_URL)
    return SubmissionResult(sent=True, prepared=prepared, draft=draft,
                            experiment_id=experiment_id, snapshot=snapshot, ledger=path,
                            uploaded=uploaded)


def _consequence(stage: str, error: BaseException, uploaded: list, experiment_id) -> tuple:
    """
    What a failure at `stage` leaves behind on the site: a kind ("orphaned", "unknown",
    "unmarked" or None) and a sentence for the person ("" for nothing).

    An answer that never came (ConnectionLost), a gateway's 5xx, a success that does not name
    what it made, or anything else that stopped the request part-way means the site may have
    acted on it, so those are "unknown".
    """
    unanswered = not isinstance(error, RobotariumError) or error.uncertain
    if stage == "mark-main":
        if unanswered:
            return "unknown", ("Experiment %s was created; whether its main file got marked is "
                               "unknown. Check \"Main File\" on %s rather than submitting "
                               "again." % (experiment_id, DASHBOARD_URL))
        return "unmarked", ("Experiment %s was created, but its main file is not marked: tick "
                            "\"Main File\" on %s rather than submitting again."
                            % (experiment_id, DASHBOARD_URL))
    if stage == "create" and unanswered:
        return "unknown", ("The experiment may have been created before the answer was lost: "
                           "check %s before submitting again." % DASHBOARD_URL)
    if stage == "upload" and unanswered:
        return "orphaned", ("%d file(s) were uploaded, and one more may have been, before it "
                            "failed; none is attached to an experiment." % len(uploaded))
    if uploaded:
        return "orphaned", ("%d file(s) were uploaded before it failed and are attached to no "
                            "experiment." % len(uploaded))
    return None, ""


def _ledger_error(error: BaseException) -> str:
    """
    The error as the ledger keeps it. A RobotariumError's message is written to carry nothing
    of the request; any other exception's might (a UnicodeEncodeError quotes the character it
    choked on), so only its type is kept in a file meant to be safe to commit.
    """
    if isinstance(error, RobotariumError):
        return str(error)
    return type(error).__name__


def _ledger_entry(prepared: Prepared, draft: bool, snapshot: Path) -> dict:
    bundle, project = prepared.bundle, prepared.project
    output_dir = Path(project.output_dir)
    entry = {
        "submitted_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "built_at": bundle.built_at,
        "title": bundle.title,
        "description": bundle.description,
        "duration": bundle.duration,
        "robots": bundle.robots,
        "main": bundle.main.name,
        "draft": draft,
        "files": bundle.hashes(),
        "git": bundle.git,
        "manifest": str(project.manifest) if project.manifest else None,
        "snapshot": str(snapshot.relative_to(output_dir)) if snapshot.is_relative_to(output_dir)
        else str(snapshot),
        "api": RobotariumClient().url,
    }
    if prepared.report is not None:
        report = prepared.report
        entry["simulation"] = {"steps": report.steps, "init_steps": report.init_steps,
                               "errors": report.errors, "robots": report.robots}
    return entry


def _unique(findings: list) -> list:
    seen, unique = set(), []
    for each in findings:
        if each not in seen:
            seen.add(each)
            unique.append(each)
    return unique


def _indent(text: str, width: int) -> str:
    return ("\n" + " " * width).join(str(text).splitlines() or [""])
