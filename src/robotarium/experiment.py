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

#: Placeholders a title may carry, filled in when the bundle is built. {config.KEY} is a value of
#: the [config] table; a dotted KEY reaches into a nested table.
TITLE_FIELDS = ("date", "time", "timestamp", "git", "robots", "main", "config.KEY")


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
        julia_version (str): A Julia compat specifier for juliapkg, e.g. "=1.11.9"; None for
            any.
        julia_startup: How long the testbed takes to install and start Julia before the main
            file's first step: "measured" (the default: estimated, see budget.py), or seconds
            (an int, or a string such as "4m").
        config_set (dict): Values given with --set, by dotted key, applied over the config; see
            set_config. Kept to report them, and recorded in the ledger.
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
    julia_startup: object = "measured"
    config_set: dict = field(default_factory=dict)

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
        if self.julia_startup is None:
            self.julia_startup = "measured"
        if not (isinstance(self.julia_startup, str)
                and self.julia_startup.strip().lower() == "measured"):
            try:
                self.julia_startup = parse_duration(self.julia_startup)
            except ValueError:
                pass  # left as given; check() says what is wrong with it
        else:
            self.julia_startup = "measured"

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
        try:
            loaded = self.load_config()
        except (OSError, ValueError, ImportError):
            loaded = None  # check() reports it
        values = {"date": when.strftime("%Y-%m-%d"), "time": when.strftime("%H:%M"),
                  "timestamp": when.strftime("%Y%m%dT%H%M"), "git": git or "",
                  "robots": self.robots, "main": Path(self.main).stem,
                  "config": _ConfigView(loaded if isinstance(loaded, dict) else {}, "config")}
        try:
            return self.title.format_map(values).strip()
        except (KeyError, IndexError, ValueError, AttributeError) as error:
            raise ValueError("The title %r has a placeholder this package does not fill (%s). "
                             "It may use %s; write {{ and }} for a literal brace."
                             % (self.title, error, ", ".join("{%s}" % each
                                                             for each in TITLE_FIELDS))) from None

    def load_config(self):
        """The run's parameters as a dict, read from the file named if config is a path."""
        if self.config is None or isinstance(self.config, dict):
            return self.config
        return read_config_file(self.config)

    def set_config(self, settings) -> None:
        """
        Applies --set values over the config, as `robotarium check --set controller=baseline`
        does: one manifest then serves several variants of an experiment, and the bundle (its
        CONFIG, its title's {config.KEY}) is the variant's. A config read from a file becomes a
        dict here.

        Args:
            settings: "KEY=VALUE" strings, or a {dotted key: value} dict. A string's VALUE is
                read as a TOML value (3, 0.5, true, "text", [1, 2], { a = 1 }), and is taken as
                a string when it is not one; a dotted KEY sets a value in a nested table. A
                value the config already has keeps its type where the text allows it: a
                string stays the text as typed (label=12 is "12"), a float stays a float
                (alpha=2 is 2.0), and a bool also takes Python's True and False.
        """
        if isinstance(settings, dict):
            pairs = [(key, value, None) for key, value in settings.items()]
        else:
            pairs = [parse_setting(each) + (_raw_value(each),) for each in settings or ()]
        if not pairs:
            return
        loaded = self.load_config()
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError("--set needs the config to be a table, and it is %s."
                             % type(loaded).__name__)
        # A copy, so the manifest's own table (or a caller's dict) is left as it was.
        config = _copy_plain(loaded) if loaded else {}
        for key, value, raw in pairs:
            parts = _key_parts(key)
            table = config
            for depth, part in enumerate(parts[:-1]):
                inner = table.setdefault(part, {})
                if not isinstance(inner, dict):
                    raise ValueError("--set %s: %s is %r, not a table, so it has no %s."
                                     % (key, ".".join(parts[:depth + 1]), inner,
                                        parts[depth + 1]))
                table = inner
            was = table.get(parts[-1], _ABSENT)
            if raw is not None:
                value = _conform(raw, value, was)
            self.config_set[".".join(parts)] = {"value": value, "was": was}
            table[parts[-1]] = value
        self.config = config

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
            resolved = path.resolve()
            if path is not self.main and not resolved.is_relative_to(self.root):
                add(Finding(INFO, "From outside the project directory (%s), uploaded flat "
                                  "beside the main file, as every file is. Import it or open it "
                                  "there by its base name, %r. `robotarium simulate` runs from "
                                  "that flat bundle; `python %s` run here does not see it."
                                  % (os.path.relpath(resolved, self.root), path.name,
                                     self.main.name), file=path.name))
            elif path is not self.main and resolved.parent != self.main.parent.resolve():
                add(Finding(INFO, "Uploaded flat, beside the main file: open it on the testbed "
                                  "as Path(__file__).parent / %r, not by its path here."
                                  % path.name, file=path.name))
            work_tree = git_work_tree(self.root)
            if work_tree is not None and not resolved.is_relative_to(work_tree):
                add(Finding(WARNING, "%s is outside the git work tree the manifest is in (%s), "
                                     "so the bundle's {git} commit and its -dirty flag say "
                                     "nothing about it; only its sha256, in "
                                     "robotarium_bundle.py, identifies it."
                                     % (resolved, work_tree), file=path.name))

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
        findings = []
        for key, change in self.config_set.items():
            value, was = change["value"], change["was"]
            if was is _ABSENT:
                findings.append(Finding(WARNING, "--set %s=%r adds %s, which the config does not "
                                                 "have. Check its spelling." % (
                                                     key, value, key)))
            elif _kind(value) != _kind(was):
                findings.append(Finding(WARNING, "--set %s=%r replaces the config's %s %r with a "
                                                 "%s. Check that this is meant, and that the "
                                                 "main file takes it." % (
                                                     key, value, _kind(was), was,
                                                     _kind(value))))
            else:
                findings.append(Finding(INFO, "--set %s=%r (the config says %r)." % (
                    key, value, was)))
        return findings

    def _check_julia_packages(self) -> list:
        findings = []
        names = [package.name for package in self.julia_packages]
        for name in sorted({each for each in names if names.count(each) > 1}):
            findings.append(Finding(ERROR, "Julia package %s is listed %d times in [julia] "
                                           "packages; list it once, with one version."
                                    % (name, names.count(name))))
        for package in self.julia_packages:
            try:
                uuid.UUID(str(package.uuid))
            except ValueError:
                findings.append(Finding(ERROR, "Julia package %s: %r is not a UUID. `robotarium "
                                               "julia lookup %s` prints the right one." % (
                                                   package.name, package.uuid, package.name)))
        if self.julia_version is not None:
            finding = _julia_version_finding(str(self.julia_version))
            if finding:
                findings.append(finding)
        startup = self.julia_startup
        if startup != "measured" and (not isinstance(startup, int) or isinstance(startup, bool)
                                      or not 0 <= startup <= limits.MAX_DURATION_SECONDS):
            findings.append(Finding(ERROR, "[julia] startup %r is not \"measured\" or a number of "
                                           "seconds from 0 to %d (240, \"4m\")."
                                    % (startup, limits.MAX_DURATION_SECONDS)))
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
        simulator_seed (int): [simulator] seed: seeds random and numpy.random, so the
            simulator's random initial poses repeat from run to run.
        recording (dict): [simulator] video, fps, every, dpi, frames, frames_dir and
            frames_format: what `robotarium simulate` records (see simulate.Recording).
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
    simulator_seed: int = None
    recording: dict = field(default_factory=dict)
    draft: bool = False
    simulate_first: bool = False
    output_dir: Path = None

    def __post_init__(self):
        if self.output_dir is None:
            self.output_dir = self.experiment.root / ".robotarium"


def _julia_version_finding(spec: str):
    """
    A WARNING when [julia] version does not pin one Julia release. Pkg reads a bare version as a
    caret specifier: "1.11.9" means at least 1.11.9 and below 2, so the testbed may install a
    newer Julia than the one tested here. "=1.11.9" pins it.
    """
    text = spec.strip()
    if re.fullmatch(r"=\s*\d+\.\d+\.\d+", text):
        return None
    # A hyphen range from one full release to itself is a pin too ("1.11.9 - 1.11.9").
    hyphen = re.fullmatch(r"(\d+\.\d+\.\d+)\s+-\s+(\d+\.\d+\.\d+)", text)
    if hyphen and hyphen.group(1) == hyphen.group(2):
        return None
    # "=1.11" pins one release, but Pkg fills in the missing parts with 0: Julia 1.11.0.
    partial = re.fullmatch(r"=\s*(\d+)(?:\.(\d+))?", text)
    if partial:
        major, minor = partial.groups()
        release = "%s.%s.0" % (major, minor or "0")
        return Finding(WARNING, "[julia] version %r pins Julia %s exactly: Pkg reads the "
                                "missing parts of an = version as 0. Write \"=%s\" if that is "
                                "the release meant, or the one used here, such as "
                                "\"=1.11.9\"." % (spec, release, release))
    bare = re.fullmatch(r"\^?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
    if bare:
        major, minor, patch = bare.groups()
        lowest = ".".join(part or "0" for part in (major, minor, patch))
        if int(major) > 0:
            below = "%d" % (int(major) + 1)
        elif minor and int(minor) > 0:
            below = "0.%d" % (int(minor) + 1)
        else:
            below = "0.0.%d" % (int(patch or 0) + 1)
        pin = "\"=%s.%s.%s\"" % (major, minor, patch) if patch \
            else "an exact release, such as \"=1.11.9\","
        return Finding(WARNING, "[julia] version %r is a caret specifier: it accepts Julia "
                                "%s and anything newer below %s, so the testbed may install a "
                                "newer Julia than the one used here. Write %s to pin one "
                                "release." % (spec, lowest, below, pin))
    return Finding(WARNING, "[julia] version %r accepts more than one Julia release, so the "
                            "testbed may install another one than the one used here. Write an "
                            "exact pin such as \"=1.11.9\"." % spec)


class _Absent:
    """What a --set key had before, when the config did not have it."""

    def __repr__(self):
        return "nothing"


_ABSENT = _Absent()


def parse_setting(text: str) -> tuple:
    """
    One --set argument as (dotted key, value). The value is read as a TOML value, and kept as
    the string it is when it is not one, so `controller=baseline` needs no quotes:

        parse_setting("gain=0.5") == ("gain", 0.5)
        parse_setting("controller=baseline") == ("controller", "baseline")
        parse_setting("gains.alpha=[1, 2]") == ("gains.alpha", [1, 2])
        parse_setting('label="12"') == ("label", "12")

    Args:
        text (str): KEY=VALUE.
    """
    key, equals, value = str(text).partition("=")
    if not equals or not key.strip():
        raise ValueError("--set takes KEY=VALUE, not %r." % text)
    _key_parts(key)
    value = value.strip()
    try:
        parsed = tomllib.loads("value = %s" % value)["value"]
    except tomllib.TOMLDecodeError:
        parsed = value
    return key.strip(), parsed


def _raw_value(text: str) -> str:
    """What a --set argument gives after the first =, as typed (stripped)."""
    return str(text).partition("=")[2].strip()


def _conform(raw: str, parsed, was):
    """
    A --set value in the type of the value it replaces, where its text allows: see
    Experiment.set_config.

        _conform("12", 12, "label") == "12"
        _conform("2", 2, 1.5) == 2.0
        _conform("False", "False", True) is False
    """
    if was is _ABSENT or was is None:
        return raw == "True" if raw in ("True", "False") else parsed
    if isinstance(was, bool):
        if not isinstance(parsed, bool) and raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        return parsed
    if isinstance(was, str):
        # A quoted TOML string, or text TOML does not read, is a string already; anything
        # else (12, true, nan, a date) is kept as the text that was typed.
        return parsed if isinstance(parsed, str) else raw
    if isinstance(was, float) and isinstance(parsed, int) and not isinstance(parsed, bool):
        return float(parsed)
    return parsed


def _kind(value) -> str:
    """A config value's type, for --set's findings: ints and floats are both numbers."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "table"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def _key_parts(key: str) -> list:
    parts = [part.strip() for part in str(key).strip().split(".")]
    if not all(parts) or not all(re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
        raise ValueError("--set %s: a key is letters, digits, _ and -, dotted for a nested "
                         "table (gains.alpha)." % key)
    return parts


def _copy_plain(value):
    if isinstance(value, dict):
        return {key: _copy_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_plain(item) for item in value]
    return value


class _ConfigView:
    """The config as a title sees it: {config.controller}, {config.gains.alpha}."""

    def __init__(self, table: dict, path: str):
        self._table = table
        self._path = path

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name not in self._table:
            raise KeyError("%s.%s" % (self._path, name))
        value = self._table[name]
        return _ConfigView(value, "%s.%s" % (self._path, name)) if isinstance(value, dict) \
            else value

    def __getitem__(self, name):
        return self.__getattr__(name)

    def __format__(self, spec):
        return format(str(self._table), spec)


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
    "julia": {"version", "packages", "startup"},
    "simulator": {"path", "fast", "headless", "timeout", "seed", "video", "fps", "every", "dpi",
                  "frames", "frames_dir", "frames_format"},
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
        julia_startup=julia.get("startup", "measured"),
    )

    simulator = data.get("simulator") or {}
    submit = data.get("submit") or {}
    output = data.get("output") or {}
    simulator_path = simulator.get("path")
    recording = {key: simulator[key] for key in ("video", "fps", "every", "dpi", "frames",
                                                 "frames_dir", "frames_format")
                 if key in simulator}
    # Paths in the manifest are the manifest's, wherever the command runs from.
    for key in ("video", "frames_dir"):
        if isinstance(recording.get(key), str):
            recording[key] = (root / recording[key]).resolve()
    seed = simulator.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ValueError("%s: [simulator] seed must be a whole number, not %r." % (manifest, seed))
    return Project(
        experiment=experiment,
        manifest=manifest,
        simulator_path=(root / simulator_path).resolve() if simulator_path else None,
        simulator_fast=bool(simulator.get("fast", True)),
        simulator_headless=bool(simulator.get("headless", True)),
        simulator_timeout=simulator.get("timeout"),
        simulator_seed=seed,
        recording=recording,
        draft=bool(submit.get("draft", False)),
        simulate_first=bool(submit.get("simulate", False)),
        output_dir=(root / output["dir"]).resolve() if output.get("dir") else None,
    )


_WORK_TREES = {}


def git_work_tree(root: Path):
    """The top of the git work tree `root` is in, resolved; None outside git."""
    root = Path(root).resolve()
    if root not in _WORK_TREES:
        try:
            found = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                                   capture_output=True, text=True, timeout=10)
            _WORK_TREES[root] = Path(found.stdout.strip()).resolve() \
                if found.returncode == 0 and found.stdout.strip() else None
        except (OSError, subprocess.SubprocessError):
            _WORK_TREES[root] = None
    return _WORK_TREES[root]


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
