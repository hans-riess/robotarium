"""
An experiment as the submission form sees it -- title, estimated duration, description, number of
robots, the files and which one is main -- plus what this package needs to build, rehearse and
send it, read from a robotarium.toml or given in Python.

    from robotarium import Experiment

    experiment = Experiment(title="Consensus ({date})", description="Eight robots agree.",
                            duration="2m", robots=8, main="main.py", files=["helpers.py"])
    for finding in experiment.check():
        print(finding)

docs/manifest.md is the reference for every key a robotarium.toml may hold.
"""

import datetime
import glob
import json
import os
import re
import subprocess
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import limits
from .findings import ERROR, INFO, WARNING, Finding

MANIFEST_NAME = "robotarium.toml"

#: The modules a bundle may carry beside the user's files: the generated record of the build,
#: and this package's runtime helpers, vendored. See bundle.py.
BUNDLE_MODULE = "robotarium_bundle.py"
RUNTIME_MODULE = "robotarium_runtime.py"
GENERATED_NAMES = (BUNDLE_MODULE, RUNTIME_MODULE)

#: The durations given as words: "auto" declares the form's maximum, and "measured" takes the
#: duration from a simulator run. "auto" is the default.
DURATION_WORDS = ("auto", "measured")

#: Placeholders a title may carry, filled in when the bundle is built.
TITLE_FIELDS = ("date", "time", "timestamp", "git", "robots", "main")


@dataclass
class JuliaPackage:
    """
    A Julia package a .jl file in the bundle needs, as juliapkg wants it declared.

    Args:
        name (str): The package's registered name, e.g. "CellularSheaves".
        uuid (str): Its UUID, from its Project.toml or the General registry (`robotarium julia
            lookup NAME` prints it).
        version (str): A Pkg compat specifier, e.g. "0.0.1", "1", "=1.14.3"; None for any.
    """

    name: str
    uuid: str
    version: str = None

    def as_dict(self) -> dict:
        return {"name": self.name, "uuid": self.uuid, "version": self.version}


@dataclass
class Experiment:
    """
    One experiment: the form's fields and the files to upload.

    Args:
        title (str): "Title". May carry {date}, {time}, {timestamp}, {git}, {robots} and {main},
            filled in when the bundle is built; see render_title.
        description (str): "Experiment Description".
        duration: "Estimated Duration": seconds as an int, a string such as "90", "90s", "2m" or
            "1m30s"; "auto" for the form's maximum, 600 s (the testbed stops a run that outlasts
            its declared duration, and declaring more costs nothing); or "measured" to take it
            from a simulator run, with headroom (see simulate.py).
        robots (int): "Number of Robots", 1 to 18.
        main: The main file, a .py (or a MATLAB .m).
        files (list): The supporting files. Globs are expanded; the main file may be listed or
            not. Every file is uploaded under its base name, flat.
        root: What relative paths are relative to; the manifest's directory when loaded from one.
        config: Parameters for the run -- a dict, or the path of a .toml, .json or .yaml file. The
            form takes none of those extensions, so the bundle carries it as CONFIG in the
            generated robotarium_bundle.py.
        runtime: Whether to vendor robotarium_runtime.py into the bundle: True, False, or "auto"
            (whenever a .py file imports it).
        allow_imports (list): Top-level modules to accept even though the Robotarium's
            libraries.txt does not list them.
        julia_packages (list): JuliaPackage entries (or dicts) the .jl files need.
        julia_version (str): A Julia compat specifier for juliapkg, e.g. "1.11"; None for any.
    """

    title: str
    description: str
    duration: object
    robots: int
    main: object
    files: list = field(default_factory=list)
    root: object = "."
    config: object = None
    runtime: object = "auto"
    allow_imports: list = field(default_factory=list)
    julia_packages: list = field(default_factory=list)
    julia_version: str = None

    def __post_init__(self):
        self.root = Path(self.root).expanduser().resolve()
        self.main = self._resolve(self.main)
        self.julia_packages = [each if isinstance(each, JuliaPackage) else JuliaPackage(**each)
                               for each in self.julia_packages]
        if isinstance(self.config, (str, Path)):
            self.config = self._resolve(self.config)
        if isinstance(self.duration, str) or isinstance(self.duration, (int, float)):
            try:
                self.duration = parse_duration(self.duration)
            except ValueError:
                pass  # left as given; check() says what is wrong with it

    def _resolve(self, path) -> Path:
        path = Path(path).expanduser()
        return path if path.is_absolute() else self.root / path

    # -- the files ------------------------------------------------------------------------------

    def upload_files(self) -> list:
        """
        The user's files to upload, main first, globs expanded and duplicates dropped. What
        bundle.py adds (the generated and vendored modules) is not included.
        """
        paths = [self.main]
        for entry in self.files:
            if self._is_pattern(entry):
                paths.extend(self._expand(entry))
            else:
                paths.append(self._resolve(entry))
        unique = []
        seen = set()
        for path in paths:
            key = path.resolve() if path.exists() else path
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return unique

    def _is_pattern(self, entry) -> bool:
        """
        Whether a files entry is a glob. One that names an existing file is that file, even
        with [ ] * or ? in its path -- `submit --file exp[1]/data.npy` must not go looking for
        exp1/data.npy.
        """
        return glob.has_magic(str(entry)) and not self._resolve(entry).is_file()

    def _expand(self, entry) -> list:
        text = str(entry)
        if not glob.has_magic(text):
            return []
        # Only what the entry spells is a pattern: the project's own directory (exp[1]/), or
        # the home directory a leading ~ stands for, must not be read as one.
        if text.startswith("~"):
            head, rest = re.match(r"(~[^/\\]*)[/\\]?(.*)", text, re.DOTALL).groups()
            pattern = os.path.join(glob.escape(os.path.expanduser(head)), rest)
        elif Path(text).is_absolute():
            pattern = text
        else:
            pattern = os.path.join(glob.escape(str(self.root)), text)
        return sorted(Path(each) for each in glob.glob(pattern, recursive=True)
                      if Path(each).is_file())

    # -- the form ------------------------------------------------------------------------------

    def render_title(self, when: datetime.datetime = None, git: str = None) -> str:
        """
        The title with its placeholders filled in.

        Args:
            when (datetime.datetime): The moment the title describes; default now.
            git (str): The short commit the files come from; default looked up under root.
        """
        when = when or datetime.datetime.now().astimezone()
        if git is None and "{git}" in self.title:
            git = git_revision(self.root) or "nogit"
        values = {"date": when.strftime("%Y-%m-%d"), "time": when.strftime("%H:%M"),
                  "timestamp": when.strftime("%Y%m%dT%H%M"), "git": git or "",
                  "robots": self.robots, "main": Path(self.main).stem}
        try:
            return self.title.format_map(values).strip()
        except (KeyError, IndexError, ValueError) as error:
            raise ValueError("The title %r has a placeholder this package does not fill (%s). "
                             "It may use %s; write {{ and }} for a literal brace."
                             % (self.title, error, ", ".join("{%s}" % each
                                                             for each in TITLE_FIELDS))) from None

    def load_config(self):
        """The run's parameters as a dict, read from the file named if config is a path."""
        if self.config is None or isinstance(self.config, dict):
            return self.config
        return read_config_file(self.config)

    # -- checks -------------------------------------------------------------------------------

    def check(self) -> list:
        """
        What is wrong with the experiment's fields and files, as Findings; empty when nothing.
        This is the form's own validation plus what the form cannot know (that a file exists,
        that two files would land on the same name). preflight.py looks inside the files.
        """
        findings = []
        add = findings.append

        if not str(self.title or "").strip():
            add(Finding(ERROR, "The experiment needs a title."))
        else:
            try:
                self.render_title(git="")
            except ValueError as error:
                add(Finding(ERROR, str(error)))

        if not str(self.description or "").strip():
            add(Finding(ERROR, "The experiment needs a description."))

        if self.duration not in DURATION_WORDS:
            if not isinstance(self.duration, int) or isinstance(self.duration, bool):
                add(Finding(ERROR, "Estimated duration %r is not a number of seconds; give "
                                   "e.g. 120, \"90s\", \"2m\", \"auto\" or \"measured\"."
                                   % (self.duration,)))
            elif not 1 <= self.duration <= limits.MAX_DURATION_SECONDS:
                add(Finding(ERROR, "Estimated duration %d s is outside the form's 1-%d s."
                                   % (self.duration, limits.MAX_DURATION_SECONDS)))

        if not isinstance(self.robots, int) or isinstance(self.robots, bool):
            add(Finding(ERROR, "Number of robots %r is not a whole number." % (self.robots,)))
        elif not limits.MIN_ROBOTS <= self.robots <= limits.MAX_ROBOTS:
            add(Finding(ERROR, "%d robots: the testbed has %d-%d." % (
                self.robots, limits.MIN_ROBOTS, limits.MAX_ROBOTS)))

        findings.extend(self._check_files())
        findings.extend(self._check_config())
        findings.extend(self._check_julia_packages())
        return findings

    def _check_files(self) -> list:
        findings = []
        add = findings.append

        if not self.main.is_file():
            add(Finding(ERROR, "The main file %s does not exist." % self.main))
        elif self.main.suffix.lower() not in limits.MAIN_FILE_EXTENSIONS:
            add(Finding(ERROR, "The main file must be %s, not %s." % (
                " or ".join(limits.MAIN_FILE_EXTENSIONS), self.main.name)))

        for entry in self.files:
            if self._is_pattern(entry) and not self._expand(entry):
                add(Finding(WARNING, "%r matches no files." % str(entry)))

        by_name = {}
        for path in self.upload_files():
            if path is self.main:
                pass
            elif path.is_dir():
                add(Finding(ERROR, "%s is a directory, and the form takes files only. List the "
                                   "files in it, or a glob such as \"%s/*.py\"."
                                   % (path, path.name)))
                continue
            elif not path.is_file():
                add(Finding(ERROR, "%s does not exist." % path))
                continue
            if path.suffix.lower() not in limits.ALLOWED_EXTENSIONS:
                add(Finding(ERROR, "The form does not take %s files. It takes %s."
                                   % (path.suffix or "extensionless",
                                      " ".join(limits.ALLOWED_EXTENSIONS)), file=path.name))
            if path.name in GENERATED_NAMES:
                add(Finding(ERROR, "%s is a name this package generates into the bundle; "
                                   "rename your file." % path.name))
            by_name.setdefault(path.name, []).append(path)
            if path is not self.main and path.parent.resolve() != self.main.parent.resolve():
                add(Finding(INFO, "Uploaded flat, beside the main file: open it on the testbed "
                                  "as Path(__file__).parent / %r, not by its path here."
                                  % path.name, file=path.name))

        for name, paths in by_name.items():
            if len(paths) > 1:
                add(Finding(ERROR, "%d files would be uploaded as %s: %s. The form flattens "
                                   "every file to its base name." % (
                                       len(paths), name, ", ".join(str(each) for each in paths))))
        return findings

    def _check_config(self) -> list:
        from .bundle import python_literal

        if self.config is None:
            return []
        try:
            loaded = self.load_config()
        except (OSError, ValueError, ImportError) as error:
            return [Finding(ERROR, "The config %s could not be read: %s" % (self.config, error))]
        if not isinstance(loaded, dict):
            return [Finding(ERROR, "The config must be a mapping at the top level.")]
        try:
            python_literal(loaded)
        except ValueError as error:
            return [Finding(ERROR, "The config cannot travel to the testbed: %s" % error)]
        return []

    def _check_julia_packages(self) -> list:
        findings = []
        for package in self.julia_packages:
            try:
                uuid.UUID(str(package.uuid))
            except ValueError:
                findings.append(Finding(ERROR, "Julia package %s: %r is not a UUID. `robotarium "
                                               "julia lookup %s` prints the right one." % (
                                                   package.name, package.uuid, package.name)))
        return findings


@dataclass
class Project:
    """
    Everything a robotarium.toml holds: the experiment, and how to rehearse and send it.

    Args:
        experiment (Experiment): The [experiment] (and [julia]) tables.
        manifest (Path): The file it was read from; None when built in Python.
        simulator_path (Path): [simulator] path: a robotarium_python_simulator checkout to run
            against when `rps` is not installed.
        simulator_fast (bool): [simulator] fast: skip the simulator's real-time pacing.
        simulator_headless (bool): [simulator] headless: draw the figure off screen.
        simulator_timeout (float): [simulator] timeout, in seconds of wall clock.
        draft (bool): [submit] draft: save the experiment rather than queue it.
        simulate_first (bool): [submit] simulate: refuse to submit what fails the simulator.
        output_dir (Path): [output] dir: where bundles, snapshots and the submission ledger go.
    """

    experiment: Experiment
    manifest: Path = None
    simulator_path: Path = None
    simulator_fast: bool = True
    simulator_headless: bool = True
    simulator_timeout: float = None
    draft: bool = False
    simulate_first: bool = False
    output_dir: Path = None

    def __post_init__(self):
        if self.output_dir is None:
            self.output_dir = self.experiment.root / ".robotarium"


def parse_duration(value):
    """
    A duration in whole seconds, rounded up; or "auto" (the form's maximum) or "measured" (from a
    simulator run), kept as words until the bundle is built.

        parse_duration(90) == parse_duration("90") == parse_duration("90s") == 90
        parse_duration("2m") == 120
        parse_duration("1m30s") == parse_duration("1.5 min") == 90

    Args:
        value: An int or float of seconds, or a string as above.
    """
    if isinstance(value, bool):
        raise ValueError("A duration is a number of seconds, not %r." % value)
    if isinstance(value, (int, float)):
        return int(-(-value // 1))
    text = str(value).strip().lower()
    if text in DURATION_WORDS:
        return text
    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*m(?:in(?:utes?)?)?)?\s*"
                         r"(?:(\d+(?:\.\d+)?)\s*(?:s(?:ec(?:onds?)?)?)?)?", text)
    if not text or not match or not any(match.groups()):
        raise ValueError("%r is not a duration; give e.g. 120, \"90s\", \"2m\" or \"1m30s\"."
                         % value)
    minutes, seconds = match.groups()
    return int(-(-(float(minutes or 0) * 60 + float(seconds or 0)) // 1))


def read_config_file(path: Path) -> dict:
    """
    A run's parameters from a .toml, .json or .yaml/.yml file. YAML needs PyYAML, which this
    package does not otherwise depend on.

    Args:
        path (Path): The file.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".toml":
        return tomllib.loads(path.read_text(encoding="utf-8"))
    if suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise ImportError("reading YAML needs PyYAML: pip install pyyaml") from None
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    raise ValueError("a config must be .toml, .json or .yaml, not %s" % (suffix or "no suffix"))


def find_manifest(start=".") -> Path:
    """
    The robotarium.toml at `start`, or in the nearest directory above it; or `start` itself
    when it names a file. None when there is none.

    Args:
        start: A directory to search from, or a manifest's path.
    """
    start = Path(start).expanduser().resolve()
    if start.is_file():
        return start
    for directory in (start, *start.parents):
        candidate = directory / MANIFEST_NAME
        if candidate.is_file():
            return candidate
    return None


#: The keys each table of a robotarium.toml may hold, so a misspelt one fails loudly rather than
#: quietly building the experiment you did not ask for.
_KNOWN_KEYS = {
    "experiment": {"title", "description", "description_file", "duration", "robots", "main",
                   "files", "config", "runtime", "allow_imports"},
    "julia": {"version", "packages"},
    "simulator": {"path", "fast", "headless", "timeout"},
    "submit": {"draft", "simulate"},
    "output": {"dir"},
    "config": None,
}


def load_project(path=".") -> Project:
    """
    Reads a robotarium.toml into a Project.

    Args:
        path: The manifest, or a directory at or under the one holding it.
    """
    manifest = find_manifest(path)
    if manifest is None:
        raise FileNotFoundError("No %s in %s or above it. `robotarium init` writes one."
                                % (MANIFEST_NAME, Path(path).resolve()))
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ValueError("%s: %s" % (manifest, error)) from None

    unknown = [table for table in data if table not in _KNOWN_KEYS]
    for table, keys in _KNOWN_KEYS.items():
        if keys is not None and isinstance(data.get(table), dict):
            unknown += ["%s.%s" % (table, key) for key in data[table] if key not in keys]
    if unknown:
        raise ValueError("%s: unknown key(s) %s. docs/manifest.md lists what each table takes."
                         % (manifest, ", ".join(unknown)))

    root = manifest.parent
    table = data.get("experiment") or {}
    missing = [key for key in ("title", "robots", "main") if key not in table]
    if "description" not in table and "description_file" not in table:
        missing.append("description (or description_file)")
    if missing:
        raise ValueError("%s: [experiment] needs %s." % (manifest, ", ".join(missing)))

    description = table.get("description")
    if "description_file" in table:
        if description is not None:
            raise ValueError("%s: give description or description_file, not both." % manifest)
        description = (root / table["description_file"]).read_text(encoding="utf-8")

    config = table.get("config")
    if "config" in data:
        if config is not None:
            raise ValueError("%s: give [experiment] config or a [config] table, not both."
                             % manifest)
        config = data["config"]

    julia = data.get("julia") or {}
    experiment = Experiment(
        title=table["title"],
        description=str(description).strip(),
        duration=table.get("duration", "auto"),
        robots=table["robots"],
        main=table["main"],
        files=list(table.get("files") or []),
        root=root,
        config=config,
        runtime=table.get("runtime", "auto"),
        allow_imports=list(table.get("allow_imports") or []),
        julia_packages=list(julia.get("packages") or []),
        julia_version=julia.get("version"),
    )

    simulator = data.get("simulator") or {}
    submit = data.get("submit") or {}
    output = data.get("output") or {}
    simulator_path = simulator.get("path")
    return Project(
        experiment=experiment,
        manifest=manifest,
        simulator_path=(root / simulator_path).resolve() if simulator_path else None,
        simulator_fast=bool(simulator.get("fast", True)),
        simulator_headless=bool(simulator.get("headless", True)),
        simulator_timeout=simulator.get("timeout"),
        draft=bool(submit.get("draft", False)),
        simulate_first=bool(submit.get("simulate", False)),
        output_dir=(root / output["dir"]).resolve() if output.get("dir") else None,
    )


def git_revision(root: Path):
    """The short commit `root` is checked out at, with "-dirty" if it has changes; or None."""
    try:
        revision = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                                  capture_output=True, text=True, timeout=10)
        if revision.returncode != 0:
            return None
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return revision.stdout.strip() + ("-dirty" if status.stdout.strip() else "")
