"""
The `robotarium` command line.

    robotarium init my_experiment [--julia]     a new experiment that runs as it stands
    robotarium check                            every offline check, nothing sent
    robotarium bundle                           lay the files out as the testbed will see them
    robotarium simulate [--show]                rehearse it in the Robotarium simulator
    robotarium simulate --video --frames 0,end  ... and record a video and stills of it
    robotarium submit [--dry-run] [--draft]     check, show, confirm, send, record
    robotarium history                          what has been sent, and how it went
    robotarium fetch [EXPERIMENT]               what came back: video, logs, data, messages
    robotarium login | logout | credentials     where the Robotarium login is kept
    robotarium limits                           the site's live limits
    robotarium julia lookup NAME                a [julia] packages entry, from the registry
    robotarium julia check [--cold]             load the backend.jl here, as the testbed will
    robotarium inspect RUN.npz                  what a RunLogger file holds

Every command that works on an experiment takes the robotarium.toml (or a directory at or under
it) as an optional positional, default the current directory. `submit` can also run without a
manifest, from flags alone: --title, --description, --duration, --robots, --main, --file.
`check`, `bundle`, `simulate`, `submit` and `julia check` take --set KEY=VALUE, a [config]
value for this command only.
"""

import argparse
import getpass
import glob
import json
import logging
import os
import sys
from pathlib import Path

from . import credentials as credentials_module
from . import julia as julia_tools
from . import budget, ledger, libraries, limits, preflight, results, scaffold, simulate, submission
from ._version import __version__
from .bundle import BundleError, BundleLocked, build, locked
from .client import RobotariumClient, RobotariumError
from .experiment import Experiment, Project, find_manifest, load_project
from .findings import ERROR, WARNING, Finding, errors, ordered

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
    except (credentials_module.CredentialsError, RobotariumError, BundleLocked,
            julia_tools.JuliaToolError, FileNotFoundError, ValueError) as error:
        print("robotarium: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # Whatever was reading the output stopped (`robotarium check | head`): nothing more to
        # say, and no traceback. stdout is pointed at devnull so the flush at exit cannot fail
        # the same way.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


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
    kind = command.add_mutually_exclusive_group()
    kind.add_argument("--julia", action="store_true", help="with a backend.jl, loaded the "
                                                           "robosheaf way")
    kind.add_argument("--example", choices=scaffold.examples(), metavar="NAME",
                      help="a ready-made example to try the CLI on: %s"
                           % ", ".join(scaffold.examples()))
    command.add_argument("--robots", type=int, default=4, help="number of robots (default 4)")
    command.add_argument("--title", help="the experiment's title (default from the directory)")
    command.add_argument("--force", action="store_true", help="overwrite files that exist")
    command.set_defaults(handler=run_init)

    command = commands.add_parser("check", help="every offline check; nothing is sent")
    add_project_argument(command)
    add_set_argument(command)
    command.add_argument("--libraries", type=Path, metavar="FILE",
                         help="a libraries.txt to check imports against")
    command.add_argument("--strict", action="store_true", help="fail on warnings too")
    command.set_defaults(handler=run_check)

    command = commands.add_parser("bundle", help="lay the files out as the testbed will see "
                                                 "them, and show the form")
    add_project_argument(command)
    add_set_argument(command)
    command.set_defaults(handler=run_bundle)

    command = commands.add_parser("simulate", help="rehearse the experiment in the simulator")
    add_project_argument(command)
    add_set_argument(command)
    add_simulator_arguments(command)
    command.add_argument("--no-check", action="store_true", help="skip the static checks")
    group = command.add_argument_group("recording (t = 0 is the first step after the drive to "
                                       "the initial poses)")
    group.add_argument("--video", nargs="?", const="", default=None, metavar="PATH.mp4",
                       help="record the run's figure to an .mp4 (needs ffmpeg); default "
                            ".robotarium/runs/<title>_<time>.mp4")
    group.add_argument("--fps", type=int, metavar="N",
                       help="the video's frames per second (default 30 / --every: real time)")
    group.add_argument("--every", type=int, metavar="N",
                       help="one frame of video every N steps (default 1)")
    group.add_argument("--dpi", type=int, metavar="N",
                       help="the figure's resolution, for the video and stills (default 200: "
                            "1650 x 1050 pixels)")
    group.add_argument("--frames", metavar="T1,T2,...,end",
                       help="save stills at these experiment times, in seconds, and at the end")
    group.add_argument("--frames-dir", type=Path, metavar="DIR",
                       help="where the stills go (default beside the video, in <video>_frames/)")
    group.add_argument("--frames-format", choices=simulate.FRAME_FORMATS,
                       help="the stills' format (default png)")
    group.add_argument("--seed", type=int, metavar="N",
                       help="seed random and numpy.random, so the random initial poses repeat")
    command.set_defaults(handler=run_simulate)

    command = commands.add_parser("submit", help="check, show, confirm, send, record")
    add_project_argument(command)
    add_set_argument(command)
    group = command.add_argument_group("the form (each overrides robotarium.toml)")
    group.add_argument("--title")
    group.add_argument("--description")
    group.add_argument("--description-file", type=Path, metavar="FILE")
    group.add_argument("--duration", help="seconds, \"90s\", \"2m\"; \"auto\" (the form's "
                                          "maximum, 600 s; the default) or \"measured\" (from a "
                                          "simulator run)")
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

    command = commands.add_parser("fetch", help="download what came back from the testbed: "
                                                "video, log files, data files, return messages")
    command.add_argument("experiment", nargs="?", metavar="EXPERIMENT",
                         help="the experiment's id or the address of its page (default: the "
                              "last one submitted from here, from the ledger)")
    command.add_argument("--project", type=Path, default=Path("."), metavar="PATH",
                         help="robotarium.toml, or a directory at or under it, whose ledger and "
                              ".robotarium/ to use (default .)")
    command.add_argument("-o", "--out", type=Path, metavar="DIR",
                         help="put the files here (default .robotarium/results/<id>/)")
    command.add_argument("--no-video", action="store_false", dest="video",
                         help="leave out the video, which can be large")
    command.add_argument("--scripts", action="store_true",
                         help="also download the files as uploaded")
    command.add_argument("--force", action="store_true",
                         help="download again what is already there")
    command.add_argument("--env-file", type=Path, metavar="FILE",
                         help="read ROBOTARIUM_USERNAME/PASSWORD from this dotenv file")
    command.set_defaults(handler=run_fetch)

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
    add_set_argument(check)
    check.add_argument("--file", dest="julia_file", metavar="FILE.jl",
                       help="which .jl to load, when the bundle has several (default "
                            "backend.jl, as load_julia_backend() loads)")
    check.add_argument("--call", action="append", default=[], dest="calls",
                       metavar="FUNC[=JSON]",
                       help="after loading, call FUNC twice through call_json (with {} or the "
                            "JSON given), timing the first call, which compiles it; repeatable")
    cold = check.add_argument_group("a cold start, as on the testbed")
    cold.add_argument("--cold", action="store_true",
                      help="install Julia and every package from nothing, in a scratch depot, "
                           "and time each phase; saved to .robotarium/julia/cold-start.json")
    cold.add_argument("--keep-julia", action="store_true",
                      help="with --cold: reuse the Julia installed here, not download one")
    cold.add_argument("--cpus", type=int, metavar="N",
                      help="with --cold: precompile with N tasks, as a smaller machine would")
    cold.add_argument("--scratch", type=Path, metavar="DIR",
                      help="with --cold: install into DIR (empty or new) rather than a "
                           "temporary directory")
    cold.add_argument("--keep-scratch", action="store_true",
                      help="with --cold: leave the scratch directory in place afterwards")
    check.add_argument("--timeout", type=float, metavar="SECONDS",
                       help="give up after this long (default 1800, 3600 with --cold)")
    check.set_defaults(handler=run_julia_check)

    command = commands.add_parser("inspect", help="what a RunLogger .npz holds")
    command.add_argument("run", type=Path, metavar="RUN.npz")
    command.set_defaults(handler=run_inspect)

    return parser


def add_project_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument("project", type=Path, nargs="?", default=Path("."),
                         help="robotarium.toml, or a directory at or under it (default .)")


def add_set_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument("--set", action="append", default=[], dest="settings",
                         metavar="KEY=VALUE",
                         help="set a [config] value for this run, read as TOML (gain=0.5, "
                              "controller=baseline, gains.alpha=2); repeatable")


def load(arguments) -> Project:
    """The project the positional names, with --set applied to its config."""
    project = load_project(arguments.project)
    project.experiment.set_config(getattr(arguments, "settings", None))
    return project


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
    if arguments.example:
        written = scaffold.create_example(arguments.example, arguments.directory,
                                          force=arguments.force)
    else:
        written = scaffold.create(arguments.directory, julia=arguments.julia,
                                  robots=arguments.robots, title=arguments.title,
                                  force=arguments.force)
    for path, fresh in written:
        print("  %s %s" % ("wrote  " if fresh else "kept   ", path))
    directory = arguments.directory
    print("\nNext:\n  cd %s\n  robotarium check\n  robotarium simulate --show    # needs the "
          "simulator: see docs/simulator.md\n  robotarium submit --dry-run" % directory)
    if arguments.julia:
        print("  robotarium julia check        # load backend.jl here, as the testbed will")
    if arguments.example:
        print("  robotarium login              # once: keeps your Robotarium login safely\n"
              "  robotarium submit             # saves it on the Robotarium as a DRAFT\n"
              "  robotarium history")
    return 0


def run_check(arguments) -> int:
    project = load(arguments)
    with locked(project.output_dir):
        return check_project(project, arguments)


def check_project(project: Project, arguments) -> int:
    bundle = build(project.experiment, project.output_dir)
    findings = project.experiment.check() + preflight.check(
        bundle.files, project.experiment,
        libraries.load(arguments.libraries, simulator_path=project.simulator_path))
    findings += budget.budget_findings(project, bundle)
    findings += recording_findings(project, bundle)
    findings = ordered(list(dict.fromkeys(findings)))
    print_findings(findings)
    print_form(bundle)
    last = last_run(project, bundle)
    if last is not None or budget.uses_julia(project.experiment, bundle):
        print()
        print_budget(project, bundle)
    blocking = [each for each in findings
                if each.level == ERROR or (arguments.strict and each.level == WARNING)]
    return 1 if blocking else 0


def last_run(project: Project, bundle):
    """The last simulation of these files and this config, or None; see simulate.save_last."""
    return simulate.last_simulation(bundle, project.output_dir,
                                    project.experiment.load_config())


def recording_findings(project: Project, bundle) -> list:
    """What `simulate` would refuse in [simulator]'s recording keys, as an ERROR."""
    try:
        recording_for(project, bundle, None)
    except ValueError as error:
        return [Finding(ERROR, "[simulator]: %s" % error)]
    return []


def run_bundle(arguments) -> int:
    project = load(arguments)
    with locked(project.output_dir):
        bundle = build(project.experiment, project.output_dir)
        print_form(bundle)
    return 0


def run_simulate(arguments) -> int:
    project = apply_simulator_arguments(load(arguments), arguments)
    with locked(project.output_dir):
        return simulate_project(project, arguments)


def simulate_project(project: Project, arguments) -> int:
    bundle = build(project.experiment, project.output_dir)
    if not arguments.no_check:
        findings = preflight.check(bundle.files, project.experiment,
                                   libraries.load(simulator_path=project.simulator_path))
        if findings:
            print_findings(ordered(findings))
            print()
    recording = recording_for(project, bundle, arguments)
    if recording is not None:
        recording.check()  # before anything runs
    seed = arguments.seed if arguments.seed is not None else project.simulator_seed
    estimate = budget.startup_estimate(project, bundle)
    print("Running %s in the simulator, from %s\n" % (bundle.main.name, bundle.directory))
    report = simulate.run(bundle, simulator=project.simulator_path, fast=project.simulator_fast,
                          headless=project.simulator_headless,
                          timeout=project.simulator_timeout,
                          log_dir=Path(project.output_dir) / "runs", recording=recording,
                          seed=seed)
    simulate.save_last(report, bundle, project.output_dir, project.experiment.load_config())
    findings = report.findings(project.experiment,
                               startup=estimate.seconds if estimate else None,
                               upper_bound=bool(estimate and estimate.upper_bound))
    if estimate:
        findings += estimate.findings
    findings = ordered(findings)
    print()
    print_findings(findings)
    if report.robots:
        print("Run logs: %s" % (Path(project.output_dir) / "runs"))
    if report.video:
        print("Video:    %s" % report.video)
    for label, still in sorted((report.stills or {}).items(),
                               key=lambda item: item[1].get("seconds", 0)):
        print("Still:    %s  (t = %.2f s)" % (still["path"], still["seconds"]))
    return 1 if errors(findings) else 0


def recording_for(project: Project, bundle, arguments):
    """
    The Recording the command line and [simulator] ask for, the command line winning; None
    when neither asks for a video or stills.
    """
    settings = dict(project.recording or {})
    arguments = arguments if arguments is not None else argparse.Namespace()
    for key in ("fps", "every", "dpi", "frames_format"):
        if getattr(arguments, key, None) is not None:
            settings[key] = getattr(arguments, key)
    if getattr(arguments, "frames", None) is not None:
        settings["frames"] = arguments.frames
    if getattr(arguments, "frames_dir", None) is not None:
        settings["frames_dir"] = arguments.frames_dir.resolve()
    video = settings.pop("video", None)
    if getattr(arguments, "video", None) is not None:
        video = arguments.video or True
    if video is False:
        video = None
    frames = simulate.parse_frames(settings.get("frames"))
    if not video and not frames:
        return None
    stem = simulate.recording_stem(project.output_dir, bundle.title)
    if video is True:
        video = stem.with_suffix(".mp4")
    elif video:
        video = Path(video).expanduser().resolve()
    if frames and not settings.get("frames_dir"):
        settings["frames_dir"] = (video.with_name(video.stem + "_frames") if video
                                  else stem.with_name(stem.name + "_frames"))
    settings["frames"] = frames
    return simulate.Recording(video=video, **settings)


def run_submit(arguments) -> int:
    project = apply_simulator_arguments(project_for_submit(arguments), arguments)
    # submission.submit holds the project's lock itself, from the build to the upload.
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
        overrides["files"] = [from_here(path) for path in arguments.files]
    if arguments.config is not None:
        overrides["config"] = arguments.config.resolve()

    if find_manifest(arguments.project) is None and "main" in overrides:
        missing = [flag for flag, key in (("--title", "title"), ("--description", "description"),
                                          ("--robots", "robots"))
                   if key not in overrides]
        if missing:
            raise ValueError("Without a robotarium.toml, submit needs %s." % ", ".join(missing))
        overrides.setdefault("duration", "auto")
        project = Project(experiment=Experiment(root=Path.cwd(), **overrides))
        project.experiment.set_config(arguments.settings)
        return project

    project = load_project(arguments.project)
    experiment = project.experiment
    for key, value in overrides.items():
        setattr(experiment, key, value)
    experiment.__post_init__()
    experiment.set_config(arguments.settings)
    return project


def from_here(path: Path) -> Path:
    """
    A --file argument as an absolute path, read from the current directory as the shell would.
    A glob keeps its pattern but not the directory's: `--file "data/*.npy"` run from exp[1]/
    must not read exp[1] as a pattern.
    """
    if not path.is_absolute() and glob.has_magic(str(path)) and not path.is_file():
        return Path(os.path.join(glob.escape(os.getcwd()), str(path)))
    return path.resolve()


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


def run_fetch(arguments) -> int:
    manifest = find_manifest(arguments.project)
    project = load_project(manifest) if manifest else None
    output_dir = project.output_dir if project else Path(arguments.project) / ".robotarium"
    if arguments.experiment:
        experiment_id = results.experiment_id_from(arguments.experiment)
    else:
        entry = results.last_submitted(output_dir)
        if entry is None:
            raise ValueError("No experiment given, and none submitted from here (%s). Name one: "
                             "robotarium fetch <id or page address>."
                             % (Path(output_dir) / ledger.LEDGER_NAME))
        experiment_id = entry["experiment_id"]
        print("The last experiment submitted from here: %s (%s)"
              % (entry.get("title"), entry.get("submitted_at")))
    found = credentials_module.resolve(project_root=manifest.parent if manifest else None,
                                       env_file=arguments.env_file)
    client = RobotariumClient()
    client.login(found.username, found.password)
    directory = arguments.out or results.directory_for(output_dir, experiment_id)
    try:
        result = results.fetch(experiment_id, directory, client=client, video=arguments.video,
                               scripts=arguments.scripts, force=arguments.force)
    except OSError as error:
        print("robotarium: could not save the results: %s" % error, file=sys.stderr)
        return 1
    print()
    print(results.describe(result))
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
    credentials_module.check_text(email=username, password=password)
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
    calls = [julia_tools.parse_call(each) for each in arguments.calls]
    if not arguments.cold and (arguments.keep_julia or arguments.cpus or arguments.scratch
                               or arguments.keep_scratch):
        raise ValueError("--keep-julia, --cpus, --scratch and --keep-scratch go with --cold.")
    project = load(arguments)
    with locked(project.output_dir):
        return julia_check_project(project, arguments, calls)


def julia_check_project(project: Project, arguments, calls: list) -> int:
    bundle = build(project.experiment, project.output_dir)
    julia_file, note = julia_tools.julia_file_for(bundle, arguments.julia_file)
    if note:
        print("Loading %s: %s." % (julia_file, note))
    if not arguments.cold:
        print("Loading the Julia backend %s from %s, as the testbed will. The first run "
              "installs Julia and the packages, which takes a while..."
              % (julia_file, bundle.directory))
        loaded = julia_tools.check(bundle, julia_file, calls=calls,
                                   timeout=arguments.timeout or 1800)
        print("Loaded %s, which exports: %s" % (loaded["module"], ", ".join(loaded["names"])))
        timings = loaded.get("timings") or {}
        if timings:
            print("Here, already installed: packages %.1f s, start %.1f s, include %.1f s%s."
                  % (timings.get("resolve_seconds") or 0, timings.get("start_seconds") or 0,
                     timings.get("include_seconds") or 0,
                     "".join(", first %s() %.1f s (then %.3f s)" % (
                         name, seconds, (loaded.get("warm_calls") or {}).get(name, 0))
                             for name, seconds in (loaded.get("first_calls") or {}).items())))
        return 0

    testbed = budget.testbed_versions()
    here = budget.local_versions()
    for name, mine, theirs in zip(("juliacall", "juliapkg"), here, testbed):
        if theirs and mine != theirs:
            print("WARNING %s is %s here and %s on the testbed (libraries.txt), so this cold "
                  "start may not be the testbed's: pip install '%s==%s'."
                  % (name, mine or "not installed", theirs, name, theirs))
    print("A cold start of the Julia backend %s in %s: Julia%s, the registry and every "
          "package from nothing, as the testbed does at the start of each run. This takes "
          "minutes." % (julia_file, bundle.directory,
                        " (reused)" if arguments.keep_julia else ""))
    if os.environ.get("JULIA_PKG_SERVER") == "":
        print("JULIA_PKG_SERVER is empty: Julia's package servers are off, so every package is "
              "a git clone, far slower than on the testbed. The budget will count the result "
              "as an upper bound.")
    record = julia_tools.cold_check(
        bundle, project.experiment, project.output_dir, julia_file=julia_file,
        timeout=arguments.timeout or 3600, calls=calls, keep_julia=arguments.keep_julia,
        cpus=arguments.cpus, scratch=arguments.scratch, keep_scratch=arguments.keep_scratch)
    print()
    print("Loaded %s, which exports: %s" % (record["module"], ", ".join(record["names"])))
    print(julia_tools.describe_cold_start(record))
    print("Saved %s" % record["path"])
    if record.get("scratch"):
        print("Kept the scratch directory %s" % record["scratch"])
    print()
    print_budget(project, bundle)
    return 0


def print_budget(project: Project, bundle) -> None:
    """The Julia startup and the last simulation's steps against the declared duration."""
    estimate = budget.startup_estimate(project, bundle)
    last = last_run(project, bundle)
    if last is None:
        if estimate:
            print("Budget: %.0f s Julia startup (%s), plus the robots' steps: `robotarium "
                  "simulate` counts them." % (estimate.seconds, estimate.detail))
        return
    print(budget.budget_line(estimate, last["testbed_seconds"], project.experiment.duration,
                             steps=last["steps"] + last["init_steps"]))


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
