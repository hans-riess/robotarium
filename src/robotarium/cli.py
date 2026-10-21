"""
The `robotarium` command line.

    robotarium init my_experiment [--julia]     a new experiment that runs as it stands
    robotarium check                            every offline check, nothing sent
    robotarium bundle                           lay the files out as the testbed will see them
    robotarium simulate [--show]                rehearse it in the Robotarium simulator
    robotarium submit [--dry-run] [--draft]     check, show, confirm, send, record
    robotarium history                          what has been sent, and how it went
    robotarium login | logout | credentials     where the Robotarium login is kept
    robotarium limits                           the site's live limits
    robotarium julia lookup NAME                a [julia] packages entry, from the registry
    robotarium julia check                      load the backend.jl here, as the testbed will
    robotarium inspect RUN.npz                  what a RunLogger file holds

Every command that works on an experiment takes the robotarium.toml (or a directory at or under
it) as an optional positional, default the current directory. `submit` can also run without a
manifest, from flags alone: --title, --description, --duration, --robots, --main, --file.
"""

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path

from . import credentials as credentials_module
from . import julia as julia_tools
from . import ledger, libraries, limits, preflight, scaffold, simulate, submission
from ._version import __version__
from .bundle import BundleError, build
from .client import RobotariumClient, RobotariumError
from .experiment import Experiment, Project, find_manifest, load_project
from .findings import ERROR, WARNING, errors, ordered

DESCRIPTION = "Bundle, check, simulate and submit experiments to the Georgia Tech Robotarium."


def main(argv=None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    configure_logging(arguments.verbose, arguments.log_file)
    if not getattr(arguments, "handler", None):
        parser.print_help()
        return 2
    try:
        return arguments.handler(arguments)
    except (BundleError, submission.SubmissionRefused) as error:
        print_findings(error.findings, file=sys.stderr)
        if arguments.handler is run_submit:
            print("Nothing sent.", file=sys.stderr)
        return 1
    except (credentials_module.CredentialsError, RobotariumError,
            julia_tools.JuliaToolError, FileNotFoundError, ValueError) as error:
        print("robotarium: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robotarium", description=DESCRIPTION,
                                     epilog="`robotarium COMMAND --help` for each command. The "
                                            "docs/ directory of the package is the guide.")
    parser.add_argument("--version", action="version", version="robotarium %s" % __version__)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log what each step does (never a password or a request body)")
    parser.add_argument("--log-file", type=Path, metavar="FILE",
                        help="also append that log, timestamped, to FILE")
    commands = parser.add_subparsers(title="commands", metavar="COMMAND")

    command = commands.add_parser("init", help="write a new experiment that runs as it stands")
    command.add_argument("directory", type=Path, nargs="?", default=Path("."))
    command.add_argument("--julia", action="store_true", help="with a backend.jl, loaded the "
                                                              "robosheaf way")
    command.add_argument("--robots", type=int, default=4, help="number of robots (default 4)")
    command.add_argument("--title", help="the experiment's title (default from the directory)")
    command.add_argument("--force", action="store_true", help="overwrite files that exist")
    command.set_defaults(handler=run_init)

    command = commands.add_parser("check", help="every offline check; nothing is sent")
    add_project_argument(command)
    command.add_argument("--libraries", type=Path, metavar="FILE",
                         help="a libraries.txt to check imports against")
    command.add_argument("--strict", action="store_true", help="fail on warnings too")
    command.set_defaults(handler=run_check)

    command = commands.add_parser("bundle", help="lay the files out as the testbed will see "
                                                 "them, and show the form")
    add_project_argument(command)
    command.set_defaults(handler=run_bundle)

    command = commands.add_parser("simulate", help="rehearse the experiment in the simulator")
    add_project_argument(command)
    add_simulator_arguments(command)
    command.add_argument("--no-check", action="store_true", help="skip the static checks")
    command.set_defaults(handler=run_simulate)

    command = commands.add_parser("submit", help="check, show, confirm, send, record")
    add_project_argument(command)
    group = command.add_argument_group("the form (each overrides robotarium.toml)")
    group.add_argument("--title")
    group.add_argument("--description")
    group.add_argument("--description-file", type=Path, metavar="FILE")
    group.add_argument("--duration", help="seconds, \"90s\", \"2m\", or \"auto\"")
    group.add_argument("--robots", type=int)
    group.add_argument("--main", type=Path, metavar="FILE")
    group.add_argument("--file", type=Path, action="append", default=[], dest="files",
                       metavar="FILE", help="a supporting file; repeatable")
    group.add_argument("--config", type=Path, metavar="FILE",
                       help="a .toml/.json/.yaml of parameters, carried as CONFIG")
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--draft", action="store_true", default=None,
                      help="save the experiment on the site rather than queue it")
    mode.add_argument("--queue", action="store_false", dest="draft",
                      help="queue it, whatever [submit] draft says")
    simulate_group = command.add_mutually_exclusive_group()
    simulate_group.add_argument("--simulate", action="store_true", default=None,
                                dest="simulate_first", help="run the simulator first")
    simulate_group.add_argument("--no-simulate", action="store_false", dest="simulate_first",
                                help="do not, whatever [submit] simulate says")
    add_simulator_arguments(command)
    command.add_argument("--dry-run", action="store_true",
                         help="show what would be sent, and send nothing")
    command.add_argument("-y", "--yes", action="store_true",
                         help="send without asking (for CI); the plan is still printed")
    command.add_argument("--strict", action="store_true", help="refuse on warnings too")
    command.add_argument("--env-file", type=Path, metavar="FILE",
                         help="read ROBOTARIUM_USERNAME/PASSWORD from this dotenv file")
    command.add_argument("--libraries", type=Path, metavar="FILE",
                         help="a libraries.txt to check imports against")
    command.set_defaults(handler=run_submit)

    command = commands.add_parser("history", help="what has been submitted, and how it went")
    add_project_argument(command)
    command.add_argument("-n", type=int, default=0, metavar="N", help="only the last N")
    command.add_argument("--json", action="store_true", help="the raw ledger entries")
    command.set_defaults(handler=run_history)

    command = commands.add_parser("login", help="keep the Robotarium login for later runs")
    command.add_argument("--store", choices=("keyring", "file"), default=None,
                         help="the system keyring (default when available) or a file only "
                              "you can read")
    command.add_argument("--verify", action="store_true", help="log in once to check them")
    command.set_defaults(handler=run_login)

    command = commands.add_parser("logout", help="forget what `login` kept")
    command.set_defaults(handler=run_logout)

    command = commands.add_parser("credentials", help="where the login would come from")
    add_project_argument(command)
    command.add_argument("--env-file", type=Path, metavar="FILE")
    command.add_argument("--verify", action="store_true", help="log in once to check them")
    command.set_defaults(handler=run_credentials)

    command = commands.add_parser("limits", help="the site's live limits (needs a login)")
    add_project_argument(command)
    command.add_argument("--env-file", type=Path, metavar="FILE")
    command.set_defaults(handler=run_limits)

    command = commands.add_parser("julia", help="tooling for a backend.jl")
    julia_commands = command.add_subparsers(title="julia commands", metavar="COMMAND")
    lookup = julia_commands.add_parser("lookup", help="a package's [julia] packages entry")
    lookup.add_argument("names", nargs="+", metavar="NAME")
    lookup.set_defaults(handler=run_julia_lookup)
    check = julia_commands.add_parser("check", help="load the backend here, as the testbed "
                                                    "will")
    add_project_argument(check)
    check.add_argument("--file", dest="julia_file", metavar="FILE.jl",
                       help="which .jl, when the bundle has several")
    check.set_defaults(handler=run_julia_check)

    command = commands.add_parser("inspect", help="what a RunLogger .npz holds")
    command.add_argument("run", type=Path, metavar="RUN.npz")
    command.set_defaults(handler=run_inspect)

    return parser


def add_project_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument("project", type=Path, nargs="?", default=Path("."),
                         help="robotarium.toml, or a directory at or under it (default .)")


def add_simulator_arguments(command: argparse.ArgumentParser) -> None:
    group = command.add_argument_group("the simulator")
    group.add_argument("--simulator", type=Path, metavar="PATH",
                       help="a robotarium_python_simulator clone to run against")
    group.add_argument("--show", action="store_true",
                       help="open the figure in a window, in real time")
    group.add_argument("--timeout", type=float, metavar="SECONDS",
                       help="stop the run after this long")


def configure_logging(verbose: bool, log_file: Path) -> None:
    logger = logging.getLogger("robotarium")
    logger.setLevel(logging.DEBUG if verbose or log_file else logging.WARNING)
    if verbose:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("robotarium: %(message)s"))
        logger.addHandler(handler)
    if log_file:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        logger.addHandler(handler)


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------

def run_init(arguments) -> int:
    written = scaffold.create(arguments.directory, julia=arguments.julia,
                              robots=arguments.robots, title=arguments.title,
                              force=arguments.force)
    for path, fresh in written:
        print("  %s %s" % ("wrote  " if fresh else "kept   ", path))
    directory = arguments.directory
    print("\nNext:\n  cd %s\n  robotarium simulate --show    # needs the simulator: see "
          "docs/simulator.md\n  robotarium submit --dry-run" % directory)
    if arguments.julia:
        print("  robotarium julia check        # load backend.jl here, as the testbed will")
    return 0


def run_check(arguments) -> int:
    project = load_project(arguments.project)
    bundle = build(project.experiment, project.output_dir)
    findings = project.experiment.check() + preflight.check(
        bundle.files, project.experiment,
        libraries.load(arguments.libraries, simulator_path=project.simulator_path))
    findings = ordered(list(dict.fromkeys(findings)))
    print_findings(findings)
    print_form(bundle)
    blocking = [each for each in findings
                if each.level == ERROR or (arguments.strict and each.level == WARNING)]
    return 1 if blocking else 0


def run_bundle(arguments) -> int:
    project = load_project(arguments.project)
    bundle = build(project.experiment, project.output_dir)
    print_form(bundle)
    return 0


def run_simulate(arguments) -> int:
    project = apply_simulator_arguments(load_project(arguments.project), arguments)
    bundle = build(project.experiment, project.output_dir)
    if not arguments.no_check:
        findings = preflight.check(bundle.files, project.experiment,
                                   libraries.load(simulator_path=project.simulator_path))
        if findings:
            print_findings(ordered(findings))
            print()
    print("Running %s in the simulator, from %s\n" % (bundle.main.name, bundle.directory))
    report = simulate.run(bundle, simulator=project.simulator_path, fast=project.simulator_fast,
                          headless=project.simulator_headless,
                          timeout=project.simulator_timeout,
                          log_dir=Path(project.output_dir) / "runs")
    findings = ordered(report.findings(project.experiment))
    print()
    print_findings(findings)
    if report.robots:
        print("Run logs: %s" % (Path(project.output_dir) / "runs"))
    return 1 if errors(findings) else 0


def run_submit(arguments) -> int:
    project = apply_simulator_arguments(project_for_submit(arguments), arguments)
    result = submission.submit(project, draft=arguments.draft,
                               simulate_first=arguments.simulate_first,
                               confirm=not arguments.yes, dry_run=arguments.dry_run,
                               strict=arguments.strict, env_file=arguments.env_file,
                               libraries_file=arguments.libraries)
    return 0 if result.sent or arguments.dry_run else 1


def project_for_submit(arguments) -> Project:
    """The manifest's project with the command line's overrides; or one from flags alone."""
    overrides = {}
    if arguments.title is not None:
        overrides["title"] = arguments.title
    if arguments.description_file is not None:
        overrides["description"] = arguments.description_file.read_text(encoding="utf-8").strip()
    if arguments.description is not None:
        overrides["description"] = arguments.description
    if arguments.duration is not None:
        overrides["duration"] = arguments.duration
    if arguments.robots is not None:
        overrides["robots"] = arguments.robots
    if arguments.main is not None:
        overrides["main"] = arguments.main.resolve()
    if arguments.files:
        overrides["files"] = [path.resolve() for path in arguments.files]
    if arguments.config is not None:
        overrides["config"] = arguments.config.resolve()

    if find_manifest(arguments.project) is None and "main" in overrides:
        missing = [flag for flag, key in (("--title", "title"), ("--description", "description"),
                                          ("--duration", "duration"), ("--robots", "robots"))
                   if key not in overrides]
        if missing:
            raise ValueError("Without a robotarium.toml, submit needs %s." % ", ".join(missing))
        return Project(experiment=Experiment(root=Path.cwd(), **overrides))

    project = load_project(arguments.project)
    experiment = project.experiment
    for key, value in overrides.items():
        setattr(experiment, key, value)
    experiment.__post_init__()
    return project


def apply_simulator_arguments(project: Project, arguments) -> Project:
    if getattr(arguments, "simulator", None):
        project.simulator_path = arguments.simulator.resolve()
    if getattr(arguments, "show", False):
        project.simulator_headless = False
        project.simulator_fast = False
    if getattr(arguments, "timeout", None):
        project.simulator_timeout = arguments.timeout
    return project


def run_history(arguments) -> int:
    manifest = find_manifest(arguments.project)
    project = load_project(manifest) if manifest else None
    output_dir = project.output_dir if project else Path(arguments.project) / ".robotarium"
    entries = ledger.read(output_dir)
    if arguments.n:
        entries = entries[-arguments.n:]
    if arguments.json:
        for entry in entries:
            print(json.dumps(entry, sort_keys=True))
        return 0
    if not entries:
        print("Nothing submitted yet from here (%s)." % (Path(output_dir) / ledger.LEDGER_NAME))
        return 0
    print("\n\n".join(ledger.describe(entry) for entry in entries))
    return 0


def run_login(arguments) -> int:
    where = arguments.store or ("keyring" if credentials_module.keyring_available() else None)
    if where is None:
        print("No usable system keyring (pip install keyring). Store in %s instead, readable "
              "by you only? Or use --store file to skip this question."
              % credentials_module.user_credentials_file())
        if input("[y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing stored.")
            return 1
        where = "file"
    username = input("Robotarium email: ").strip()
    password = getpass.getpass("Robotarium password: ")
    if not username or not password:
        print("Nothing stored.")
        return 1
    if arguments.verify:
        RobotariumClient().login(username, password)
        print("Logged in: the credentials work.")
    print("Stored in %s." % credentials_module.store(username, password, where))
    return 0


def run_logout(arguments) -> int:
    removed = credentials_module.forget()
    print("Removed %s." % " and ".join(removed) if removed else "Nothing was stored.")
    return 0


def run_credentials(arguments) -> int:
    manifest = find_manifest(arguments.project)
    found = credentials_module.resolve(project_root=manifest.parent if manifest else None,
                                       env_file=arguments.env_file, interactive=False)
    print("Account:  %s" % credentials_module.mask(found.username))
    print("Username: from %s" % found.username_source)
    print("Password: from %s" % found.password_source)
    for warning in found.warnings:
        print("WARNING: %s" % warning)
    if arguments.verify:
        RobotariumClient().login(found.username, found.password)
        print("Logged in: the credentials work.")
    return 0


def run_limits(arguments) -> int:
    manifest = find_manifest(arguments.project)
    found = credentials_module.resolve(project_root=manifest.parent if manifest else None,
                                       env_file=arguments.env_file)
    client = RobotariumClient()
    client.login(found.username, found.password)
    settings = client.settings()
    print("The site's live limits (%s):" % client.url)
    print("  Robots               %s-%s" % (settings.get("minRbt"), settings.get("maxRbt")))
    print("  Estimated duration   up to %s s" % settings.get("maxDuration"))
    print("  File types           %s" % " ".join(settings.get("allowedFileExt") or []))
    print("\nChecked offline against: robots %d-%d, duration up to %d s, %s."
          % (limits.MIN_ROBOTS, limits.MAX_ROBOTS, limits.MAX_DURATION_SECONDS,
             " ".join(limits.ALLOWED_EXTENSIONS)))
    return 0


def run_julia_lookup(arguments) -> int:
    print("[julia]\npackages = [")
    for name in arguments.names:
        print("    %s" % julia_tools.toml_entry(julia_tools.lookup(name)))
    print("]")
    return 0


def run_julia_check(arguments) -> int:
    project = load_project(arguments.project)
    bundle = build(project.experiment, project.output_dir)
    print("Loading the Julia backend from %s, as the testbed will. The first run installs "
          "Julia and the packages, which takes a while..." % bundle.directory)
    loaded = julia_tools.check(bundle, arguments.julia_file)
    print("Loaded %s, which exports: %s" % (loaded["module"], ", ".join(loaded["names"])))
    return 0


def run_inspect(arguments) -> int:
    from robotarium_runtime import load_run

    run = load_run(arguments.run)
    meta = run.meta
    built = meta.get("bundle") or {}
    print("%s, started %s, %s record(s)" % (meta.get("name"), meta.get("started_at"),
                                            meta.get("records")))
    if built:
        print("Bundle: %s (built %s%s)" % (built.get("title"), built.get("built_at"),
                                           ", commit %s" % built["git"] if built.get("git")
                                           else ""))
    for key, value in run.data.items():
        steps = run.steps.get(key)
        span = " steps %d-%d" % (steps[0], steps[-1]) if steps is not None and len(steps) else ""
        print("  %-20s %-16s %s%s" % (key, "x".join(str(each) for each in value.shape),
                                      value.dtype, span))
    for note in meta.get("notes") or []:
        print("  [step %s | %s s] %s" % (note.get("step"), note.get("seconds"),
                                         note.get("message")))
    return 0


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def print_findings(findings: list, file=None) -> None:
    file = file or sys.stdout
    for finding in findings:
        print(finding, file=file)
    counts = [sum(1 for each in findings if each.level == level) for level in (ERROR, WARNING)]
    print("%d error(s), %d warning(s)." % tuple(counts), file=file)


def print_form(bundle) -> None:
    print("\nBundle in %s:" % bundle.directory)
    for path in bundle.files:
        tag = "  <-- MAIN FILE" if path == bundle.main else \
            "  (generated)" if path.name in bundle.generated else ""
        print("  %8.1f KB  %s%s" % (path.stat().st_size / 1e3, path.name, tag))
    print("\n" + "-" * 78)
    print("SUBMISSION FORM")
    print("-" * 78)
    print("Title                        %s" % bundle.title)
    print("Estimated Duration (max %d) %s" % (limits.MAX_DURATION_SECONDS, bundle.duration))
    print("Number of Robots (1-%d)      %d" % (limits.MAX_ROBOTS, bundle.robots))
    print("Experiment Description       %s" % bundle.description)
    print("-" * 78)


if __name__ == "__main__":
    sys.exit(main())
