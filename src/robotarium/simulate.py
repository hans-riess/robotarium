"""
Rehearsing an experiment in the Robotarium Python simulator before it is sent.

    report = simulate.run(bundle, simulator_path="../robotarium_python_simulator")
    for finding in report.findings(experiment):
        print(finding)

The main file runs from the bundle directory -- the flat layout the testbed gives it -- in a
separate Python, under a thin wrapper around rps.robotarium.Robotarium (see _simshim.py). What
comes back is what the simulator's own end-of-run verdict is made of, plus what the form needs to
agree with:

    robots          how many robots each Robotarium(...) was made with: must equal the form's
    init_steps      steps spent driving to the initial poses
    steps           steps of the experiment proper
    errors          the simulator's counts: robots_too_close and robots_outside_boundaries
                    (the testbed rejects an experiment with any), exceeded_actuator_limits
    ended           whether debug() / call_at_scripts_end() was called

(init_steps + steps) x 0.033 s is the run's wall clock on the testbed, which is what "Estimated
Duration" asks for; limits.suggested_duration adds headroom. A duration of "measured" in
robotarium.toml is filled in from exactly that, plus the Julia startup estimate (budget.py)
when the bundle has a Julia backend.

    loop_ms         the main file's own time between step() calls: median, p99 and max
    startup_seconds wall clock until the first Robotarium(...), Julia's loading included
    julia           robotarium_runtime.julia_timings() of the run, when it loaded a backend

A run can also be recorded (see Recording): a video of the figure, and still frames at chosen
experiment times, with t = 0 the first step after the drive to the initial poses.

The simulator is the `rps` package from robotarium_python_simulator. It is found the way Python
finds any package, or under [simulator] path / --simulator / $ROBOTARIUM_SIMULATOR, which is put
on the run's PYTHONPATH: point it at a clone to run against that clone without installing it.
"""

import collections
import datetime
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import limits
from .credentials import PASSWORD_VARIABLE, USERNAME_VARIABLE
from .findings import ERROR, INFO, WARNING, Finding
from .results import BASE64_LINE, COPY_BEGIN, COPY_END, TIMING

SIMULATOR_VARIABLE = "ROBOTARIUM_SIMULATOR"

#: The formats a still frame can be saved in.
FRAME_FORMATS = ("png", "pdf")

#: Where the last simulation's step count is kept, under the output directory, for `check` and
#: `julia check --cold` to show the budget without simulating again.
LAST_SIMULATION = Path("runs") / "last-simulation.json"

log = logging.getLogger(__name__)


def parse_frames(value) -> list:
    """
    The experiment times to save stills at, as seconds (floats) and "end", in order and without
    repeats:

        parse_frames("0,14.5,end") == [0.0, 14.5, "end"]
        parse_frames([0, 30, "end"]) == [0.0, 30.0, "end"]

    Args:
        value: A comma-separated string, or a list of numbers and "end".
    """
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    frames = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if text.lower() == "end":
            parsed = "end"
        else:
            try:
                parsed = float(text.rstrip("s"))
            except ValueError:
                raise ValueError("--frames takes times in seconds and \"end\", such as "
                                 "0,14.5,end; not %r." % text) from None
            if not math.isfinite(parsed) or parsed < 0:
                raise ValueError("--frames: %r is not a time at or after 0 s." % text)
        if parsed not in frames:
            frames.append(parsed)
    return frames


def still_step(seconds: float) -> int:
    """The experiment step a still at `seconds` is taken at: the first at or after it."""
    return int(math.ceil(float(seconds) / limits.STEP_SECONDS - 1e-9))


def still_name(seconds, file_format: str = "png") -> str:
    """
    A still's file name: t0s.png, t14s.png, t14p5s.png, end.png. No second dot, which LaTeX's
    \\includegraphics would read as the extension.
    """
    if seconds == "end":
        return "end.%s" % file_format
    text = ("%.3f" % float(seconds)).rstrip("0").rstrip(".")
    return "t%ss.%s" % (text.replace(".", "p"), file_format)


def recording_stem(output_dir, title: str, when: datetime.datetime = None) -> Path:
    """
    Where a recording goes by default: .robotarium/runs/<title as a slug>_<YYYYmmdd_HHMMSS>,
    to which ".mp4" and "_frames/" are added.
    """
    from .bundle import slug
    when = when or datetime.datetime.now()
    return Path(output_dir) / "runs" / ("%s_%s" % (slug(title), when.strftime("%Y%m%d_%H%M%S")))


def _setting(name: str) -> str:
    """A recording setting as both places name it: --frames-dir, [simulator] frames_dir."""
    return "--%s ([simulator] %s)" % (name.replace("_", "-"), name)


@dataclass
class Recording:
    """
    What to record of a simulator run: a video of the figure, still frames, or both.

    Args:
        video (Path): The .mp4 to write; None for no video. Needs ffmpeg.
        frames (list): Experiment times in seconds, and "end", to save a still at; or a string
            such as "0,14.5,end". t = 0 is the first step after the drive to the initial poses,
            and a still at T s is taken at step ceil(T / 0.033).
        frames_dir (Path): Where the stills go; needed when there are frames.
        frames_format (str): "png" or "pdf".
        fps (int): The video's frames per second; default 30 / every, which plays it in real
            time.
        every (int): Keep one frame of video every this many steps.
        dpi (int): The figure's resolution, which sets the video's size (the figure is 8.25 x
            5.25 inches, so 200 dpi is 1650 x 1050 pixels) and the stills'.
    """

    video: object = None
    frames: list = field(default_factory=list)
    frames_dir: object = None
    frames_format: str = "png"
    fps: int = None
    every: int = 1
    dpi: int = 200

    def __post_init__(self):
        self.frames = parse_frames(self.frames)
        self.video = Path(self.video) if self.video else None
        self.frames_dir = Path(self.frames_dir) if self.frames_dir else None
        self.frames_format = str(self.frames_format or "png").lower().lstrip(".")
        if self.video is not None and self.video.suffix.lower() != ".mp4":
            raise ValueError("The video is written as an .mp4, so name one, not %s. (Give the "
                             "project before --video, not after it.)" % self.video)
        if self.frames_format not in FRAME_FORMATS:
            raise ValueError("Stills (--frames-format, [simulator] frames_format) are %s, not "
                             "%s." % (" or ".join(FRAME_FORMATS), self.frames_format))
        for name, value in (("every", self.every), ("dpi", self.dpi)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("%s must be a whole number of at least 1, not %r."
                                 % (_setting(name), value))
        if self.fps is None:
            self.fps = max(1, int(round(1 / (limits.STEP_SECONDS * self.every))))
        if isinstance(self.fps, bool) or not isinstance(self.fps, int) or self.fps < 1:
            raise ValueError("%s must be a whole number of at least 1, not %r."
                             % (_setting("fps"), self.fps))
        if self.frames and self.frames_dir is None:
            raise ValueError("Stills need a directory to go to (--frames-dir).")

    @property
    def wanted(self) -> bool:
        return bool(self.video or self.frames)

    def check(self) -> None:
        """
        Refuses, before anything runs, a recording that cannot be made here, and makes the
        directories it writes to: a recording that fails mid-run would otherwise cost the run.
        """
        if self.video and shutil.which("ffmpeg") is None:
            raise ValueError("--video needs ffmpeg, and it is not on PATH. Install it (apt "
                             "install ffmpeg, brew install ffmpeg), or leave out --video: "
                             "stills (--frames) need no ffmpeg.")
        if self.video and self.video.is_dir():
            raise ValueError("The video %s is a directory; name an .mp4 file." % self.video)
        for what, directory in (("the video", self.video.parent if self.video else None),
                                ("the stills", self.frames_dir if self.frames else None)):
            if directory is None:
                continue
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise ValueError("Cannot make %s, the directory for %s: %s."
                                 % (directory, what, error.strerror or error)) from None

    def environment(self) -> dict:
        """The variables that tell the shim what to record."""
        return {"ROBOTARIUM_SIM_VIDEO": str(self.video.resolve()) if self.video else "",
                "ROBOTARIUM_SIM_FPS": str(self.fps), "ROBOTARIUM_SIM_EVERY": str(self.every),
                "ROBOTARIUM_SIM_DPI": str(self.dpi),
                "ROBOTARIUM_SIM_FRAMES": ",".join("end" if each == "end" else repr(each)
                                                  for each in self.frames),
                "ROBOTARIUM_SIM_FRAMES_DIR": str(self.frames_dir.resolve())
                if self.frames_dir else "",
                "ROBOTARIUM_SIM_FORMAT": self.frames_format}


@dataclass
class SimulationReport:
    """
    What a simulator run found. See the module docstring for the fields.

    Args:
        returncode (int): The main file's exit status; 3 when rps could not be imported.
        timed_out (bool): Whether it was stopped for running past the timeout.
        output (str): The last lines it printed.
    """

    returncode: int
    timed_out: bool = False
    rps: str = None
    robots: list = field(default_factory=list)
    init_steps: int = 0
    steps: int = 0
    errors: dict = field(default_factory=dict)
    ended: bool = False
    exception: str = None
    wall_seconds: float = None
    skipped_initialization: bool = False
    too_long: bool = False
    output: str = ""
    startup_seconds: float = None
    loop_ms: dict = None
    julia: dict = None
    seed: int = None
    figure_forced: bool = False
    video: str = None
    stills: dict = field(default_factory=dict)
    frames_missing: list = field(default_factory=list)
    recording_error: str = None

    @property
    def total_steps(self) -> int:
        return self.init_steps + self.steps

    @property
    def testbed_seconds(self) -> float:
        """The run's wall clock on the testbed, at its fixed step."""
        return self.total_steps * limits.STEP_SECONDS

    @property
    def suggested_duration(self) -> int:
        """An "Estimated Duration" with headroom; see limits.suggested_duration."""
        return limits.suggested_duration(self.total_steps)

    def suggested(self, startup: float = None) -> int:
        """suggested_duration, with `startup` seconds of Julia before the first step."""
        return limits.suggested_duration(self.total_steps, extra_seconds=startup or 0)

    def findings(self, experiment=None, startup: float = None, upper_bound: bool = False) -> list:
        """
        The run's verdict as Findings, against the experiment's form fields when given.

        Args:
            experiment (Experiment): What the form will say; None to judge the run alone.
            startup (float): Seconds the testbed takes to start Julia before the first step
                (budget.startup_estimate); None or 0 for a run without Julia.
            upper_bound (bool): The startup is at most that (StartupEstimate.upper_bound), so
                what it alone pushes past a limit is a WARNING, not an ERROR.
        """
        findings = []
        add = findings.append

        if self.timed_out:
            add(Finding(ERROR, "The run was stopped at the timeout, after %s."
                               % ("%d steps" % self.total_steps if self.rps
                                  else "an unknown number of steps (it was stopped before it "
                                       "could report)")))
            if self.rps is None:
                return findings
        elif self.too_long:
            add(Finding(ERROR, "The run was stopped after %d steps, about %.0f s on the testbed: "
                               "longer than the form's %d s maximum, so it cannot be submitted "
                               "as it is. (A drive to initial poses the robots cannot reach "
                               "never ends either.)"
                               % (self.total_steps, self.testbed_seconds,
                                  limits.MAX_DURATION_SECONDS)))
        if self.rps is None:
            if self.returncode == 3:
                add(Finding(ERROR, "The simulator (rps) is not importable. Install it (pip "
                                   "install git+https://github.com/robotarium/"
                                   "robotarium_python_simulator) or point [simulator] path at "
                                   "a clone of it."))
            else:
                last = self.output.strip().splitlines()[-1:] or ["no output"]
                add(Finding(ERROR, "The run ended before the simulator could report (exit %d): "
                                   "%s" % (self.returncode, last[0])))
            return findings
        if self.returncode != 0 and not (self.timed_out or self.too_long):
            last = (self.exception or self.output).strip().splitlines()[-1:] or ["no output"]
            add(Finding(ERROR, "The main file failed in the simulator (exit %d): %s"
                               % (self.returncode, last[0])))

        if not self.robots:
            add(Finding(ERROR, "The main file never created a Robotarium."))
        elif experiment is not None and isinstance(experiment.robots, int):
            for count in sorted(set(self.robots)):
                if count != experiment.robots:
                    add(Finding(ERROR, "The simulation ran %d robots, and the form would say %d."
                                       % (count, experiment.robots)))

        too_close = self.errors.get("robots_too_close", 0)
        outside = self.errors.get("robots_outside_boundaries", 0)
        actuator = self.errors.get("exceeded_actuator_limits", 0)
        if too_close:
            add(Finding(ERROR, "%d step(s) had robots too close together (potential "
                               "collision); the testbed rejects such an experiment." % too_close))
        if outside:
            add(Finding(ERROR, "%d step(s) had robots outside the arena; the testbed rejects "
                               "such an experiment." % outside))
        if actuator:
            add(Finding(WARNING, "%d of %d step(s) (%.0f%%) commanded more than the wheels can "
                                 "do; the testbed will not follow those commands exactly."
                                 % (actuator, self.steps, 100.0 * actuator / max(self.steps, 1))))

        if self.robots and self.returncode == 0 and not self.ended:
            add(Finding(WARNING, "The run never called r.debug() (or r.call_at_scripts_end()), "
                                 "which the Robotarium requires at the end."))

        if self.skipped_initialization:
            add(Finding(INFO, "skip_initialization=True was overridden: the robots were driven "
                              "to their initial poses first, as on the testbed."))
        findings.extend(self._loop_findings())
        findings.extend(self._recording_findings())
        if self.julia:
            julia = self.julia
            add(Finding(INFO, "Julia here, already installed: packages %.1f s, start %.1f s, "
                              "include %.1f s, first calls %.1f s; the first Robotarium came "
                              "%.0f s into the run. The testbed installs it all first."
                              % (julia.get("resolve_seconds") or 0, julia.get("start_seconds")
                                 or 0, julia.get("include_seconds") or 0,
                                 sum((julia.get("first_calls") or {}).values()),
                                 self.startup_seconds or 0)))
        # A run cut short says nothing about how long the whole one takes.
        if self.total_steps and not (self.timed_out or self.too_long):
            findings.extend(self.duration_findings(experiment, float(startup or 0),
                                                   upper_bound=upper_bound))
        return findings

    def duration_findings(self, experiment, startup: float, upper_bound: bool = False) -> list:
        """
        Whether startup + drive + steps fit the declared duration, as Findings: the part of
        findings() that `robotarium check` also makes from the last simulation.

        Args:
            experiment (Experiment): Its duration; None to judge against the form's maximum.
            startup (float): Seconds of Julia startup before the first step; 0 for none.
            upper_bound (bool): The startup is at most that; see findings().
        """
        findings = []
        add = findings.append
        total = self.testbed_seconds + startup
        suggested = self.suggested(startup)
        about = "at most about" if upper_bound else "about"
        if startup:
            add(Finding(INFO, "%d steps (%d driving to the initial poses): about %.0f s on the "
                              "testbed, after %s %.0f s of Julia startup: %.0f s in all. "
                              "Suggested estimated duration: %d s."
                              % (self.total_steps, self.init_steps, self.testbed_seconds,
                                 about, startup, total, suggested)))
        else:
            add(Finding(INFO, "%d steps (%d driving to the initial poses): about %.0f s on the "
                              "testbed. Suggested estimated duration: %d s."
                              % (self.total_steps, self.init_steps, self.testbed_seconds,
                                 suggested)))
        duration = getattr(experiment, "duration", None)
        fixed = isinstance(duration, int) and not isinstance(duration, bool)
        # What the startup alone pushes over a limit is only as sure as the startup is.
        by_startup = WARNING if upper_bound else ERROR
        if fixed and total > duration:
            if total > limits.MAX_DURATION_SECONDS:
                advice = ("No declared duration fits it: the form's maximum is %d s, so the "
                          "run%s has to be shorter." % (limits.MAX_DURATION_SECONDS,
                                                        " or the Julia startup" if startup
                                                        else ""))
            else:
                advice = "Declare %d, or \"auto\" (the form's maximum)." % suggested
            add(Finding(ERROR if self.testbed_seconds > duration else by_startup,
                        "The run takes %s %.0f s on the testbed%s, and the form would declare "
                        "%d s; the testbed stops a run that outlasts its declared duration. %s"
                        % (about if startup else "about", total,
                           " (%.0f s of Julia startup, then %.0f s of steps)"
                           % (startup, self.testbed_seconds) if startup else "",
                           duration, advice)))
        if self.testbed_seconds > limits.MAX_DURATION_SECONDS:
            add(Finding(ERROR, "About %.0f s is over the form's %d s maximum."
                               % (self.testbed_seconds, limits.MAX_DURATION_SECONDS)))
        elif startup and total > limits.MAX_DURATION_SECONDS and not fixed:
            add(Finding(by_startup, "With %s %.0f s of Julia startup first, the run takes %s "
                                    "%.0f s: over the form's %d s maximum, so the testbed would "
                                    "stop it before the end. No declared duration fits it; the "
                                    "run or the startup has to be shorter."
                                    % (about, startup, about, total,
                                       limits.MAX_DURATION_SECONDS)))
        elif startup and duration == "auto" and total > limits.AUTO_WARNING_SECONDS:
            add(Finding(WARNING, "With %s %.0f s of Julia startup first, the run takes %s "
                                 "%.0f s of the %d s that \"auto\" declares; a slower start on "
                                 "the testbed would cut it off." % (
                                     about, startup, about, total,
                                     limits.MAX_DURATION_SECONDS)))
        return findings

    def _loop_findings(self) -> list:
        loop = self.loop_ms or {}
        if not loop.get("count"):
            return []
        step_ms = 1000 * limits.STEP_SECONDS
        text = ("median %.1f ms, 99th percentile %.1f ms, at most %.1f ms over %d steps"
                % (loop["median"], loop["p99"], loop["max"], loop["count"]))
        if loop["p99"] > step_ms:
            return [Finding(WARNING, "The main file's own time between steps (%s) exceeds the "
                                     "testbed's %.0f ms step, so on the robots it may not keep "
                                     "up in real time. This machine's speed is not the "
                                     "testbed's." % (text, step_ms))]
        return [Finding(INFO, "The main file's own time between steps: %s, within the testbed's "
                              "%.0f ms step." % (text, step_ms))]

    def _recording_findings(self) -> list:
        findings = []
        if self.figure_forced:
            findings.append(Finding(INFO, "show_figure=False was overridden to record the run: "
                                          "the video and stills are pictures of the figure. "
                                          "The testbed runs the main file as written."))
        if self.recording_error:
            findings.append(Finding(WARNING, "Recording: %s." % self.recording_error))
        end = self.steps * limits.STEP_SECONDS
        for seconds in self.frames_missing or []:
            findings.append(Finding(WARNING, "No still at %g s: the experiment ended at %.1f s "
                                             "(%d steps after the drive to the initial poses)."
                                    % (seconds, end, self.steps)))
        return findings


def simulator_path(configured=None):
    """
    The simulator checkout to run against: `configured` if given, else $ROBOTARIUM_SIMULATOR;
    None to use whatever `rps` this Python imports.
    """
    path = configured or os.environ.get(SIMULATOR_VARIABLE)
    if not path:
        return None
    path = Path(path).expanduser().resolve()
    if not (path / "rps" / "__init__.py").is_file():
        raise FileNotFoundError("%s is not a robotarium_python_simulator checkout (it has no "
                                "rps/__init__.py)." % path)
    return path


def run(bundle, simulator=None, fast: bool = True, headless: bool = True, timeout: float = None,
        log_dir=None, echo: bool = True, python: str = None, recording: Recording = None,
        seed: int = None) -> SimulationReport:
    """
    Runs the bundle's main file in the simulator and reports what happened.

    Args:
        bundle (Bundle): What bundle.build returned.
        simulator: A simulator checkout to put on the run's PYTHONPATH; see simulator_path.
        fast (bool): Turn off the simulator's real-time pacing.
        headless (bool): Draw the figure off screen (matplotlib's Agg backend), for a machine
            with no display or a run nobody is watching.
        timeout (float): Stop the run after this many seconds of wall clock; None for never.
        log_dir: Where robotarium_runtime.RunLogger writes during the run; default the bundle's.
        echo (bool): Print the run's output as it comes, as well as keeping its tail.
        python (str): The interpreter to run it with; default this one.
        recording (Recording): A video and stills to make of the run; None for neither.
        seed (int): Seeds Python's random and numpy.random before the main file runs, so the
            simulator's random initial poses (and the drive to them) repeat; None leaves them
            random.
    """
    if bundle.main.suffix != ".py":
        raise ValueError("Only a Python main file can be simulated, not %s." % bundle.main.name)
    if recording is not None and recording.wanted:
        recording.check()
    # The experiment is the user's code, but it has no business with the Robotarium login.
    environment = {key: value for key, value in os.environ.items()
                   if key not in (USERNAME_VARIABLE, PASSWORD_VARIABLE)}
    checkout = simulator_path(simulator)
    if checkout:
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(checkout)] + [each for each in [environment.get("PYTHONPATH")] if each])
    if headless:
        environment["MPLBACKEND"] = "Agg"
    environment["ROBOTARIUM_SIM_FAST"] = "1" if fast else "0"
    # The reader decodes UTF-8, so the run writes it, whatever the console's own encoding
    # (cp1252 on Windows).
    environment["PYTHONIOENCODING"] = "utf-8"
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        environment["ROBOTARIUM_LOG_DIR"] = str(Path(log_dir).resolve())

    # The run stops itself past the form's maximum: no run that long can be submitted, and a
    # drive to initial poses the robots cannot reach would otherwise never end.
    environment["ROBOTARIUM_SIM_MAX_STEPS"] = str(
        int(limits.MAX_DURATION_SECONDS / limits.STEP_SECONDS) + 1)
    for key in [each for each in environment if each.startswith("ROBOTARIUM_SIM_")
                and each not in ("ROBOTARIUM_SIM_FAST", "ROBOTARIUM_SIM_MAX_STEPS")]:
        del environment[key]  # only what this run asks for
    if recording is not None and recording.wanted:
        environment.update(recording.environment())
    if seed is not None:
        environment["ROBOTARIUM_SIM_SEED"] = str(int(seed))

    with tempfile.TemporaryDirectory() as scratch:
        report_path = Path(scratch) / "report.json"
        environment["ROBOTARIUM_SIM_REPORT"] = str(report_path)
        command = [python or sys.executable, "-m", "robotarium._simshim", str(bundle.main)]
        log.debug("running %s in %s", " ".join(command), bundle.directory)

        # Output a console cannot show is replaced, never fatal: a reader that died would leave
        # the run blocked on a full pipe.
        # On Windows the run gets its own process group, which is what lets a timeout send it
        # Ctrl-Break (the shim takes that as Ctrl-C) rather than terminate it outright.
        process = subprocess.Popen(command, cwd=bundle.directory, env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   encoding="utf-8", errors="replace", bufsize=1,
                                   creationflags=getattr(subprocess,
                                                         "CREATE_NEW_PROCESS_GROUP", 0)
                                   if os.name == "nt" else 0)
        tail = collections.deque(maxlen=60)
        reader = threading.Thread(target=_relay, args=(process.stdout, tail, echo), daemon=True)
        reader.start()

        stopped = threading.Event()
        timer = None
        if timeout:
            def stop():
                if process.poll() is not None:
                    return  # it finished in time, whatever is still being printed
                stopped.set()
                _interrupt(process)  # so the shim still writes its report
                killer = threading.Timer(10.0, _kill, (process,))
                killer.daemon = True
                killer.start()
            timer = threading.Timer(float(timeout), stop)
            timer.start()
        restore = _stop_with_this_process()
        try:
            returncode = process.wait()
        finally:
            restore()
            if timer is not None:
                timer.cancel()
            if process.poll() is None:
                _kill(process)
                process.wait()
        # The rest of the output, however slow the console; but a process the main file
        # started and left holding the pipe does not keep this waiting past the 30 s (5 s once
        # the run has already been stopped for its timeout).
        reader.join(timeout=5 if stopped.is_set() else 30)

        data = {}
        if report_path.is_file():
            data = json.loads(report_path.read_text(encoding="utf-8"))

    known = {key: data[key] for key in ("rps", "robots", "init_steps", "steps", "errors",
                                        "ended", "exception", "wall_seconds",
                                        "skipped_initialization", "too_long",
                                        "startup_seconds", "loop_ms", "julia", "seed",
                                        "figure_forced", "video", "stills", "frames_missing",
                                        "recording_error") if key in data}
    return SimulationReport(returncode=returncode, timed_out=stopped.is_set(),
                            output="".join(tail), **known)


def save_last(report: SimulationReport, bundle, output_dir, config=None) -> Path:
    """
    Keeps a finished run's step count, with the hashes of the files it ran and of its config,
    in <output_dir>/runs/last-simulation.json; None for a run cut short, which says nothing
    about how long the whole one takes. See last_simulation.

    Args:
        report (SimulationReport): The run.
        bundle (Bundle): What it ran.
        output_dir: The project's .robotarium directory.
        config (dict): The config it ran with (a --set variant's, say).
    """
    if report.rps is None or report.timed_out or report.too_long or not report.total_steps:
        return None
    path = Path(output_dir) / LAST_SIMULATION
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
              "title": bundle.title, "steps": report.steps, "init_steps": report.init_steps,
              "testbed_seconds": round(report.testbed_seconds, 3),
              "files": _user_hashes(bundle), "config": _digest(config),
              "loop_ms": report.loop_ms, "julia": report.julia,
              "startup_seconds": report.startup_seconds}
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def last_simulation(bundle, output_dir, config=None):
    """
    What save_last kept, when it was a run of the bundle's files as they are now (the generated
    files aside, which change with each build) and of the same config; otherwise None.
    """
    path = Path(output_dir) / LAST_SIMULATION
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if record.get("files") != _user_hashes(bundle) or record.get("config") != _digest(config):
        return None
    return record


def _user_hashes(bundle) -> dict:
    return {name: digest for name, digest in bundle.hashes().items()
            if name not in bundle.generated}


def _digest(config) -> str:
    import hashlib
    text = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _relay(stream, tail, echo: bool) -> None:
    """
    Copies the run's output to the console as it comes, keeping the last lines. It reads to
    the end whatever the console does, since the run blocks once the pipe is full. The copy of
    its file a RunLogger prints for the testbed's log.txt is shown as one line, not as base64,
    and the Julia timing marker (for log.txt too) not at all: the line before it says the same.

    A copy whose base64 stops before its end line was cut short on its way out (a
    robotarium_runtime that predates _emit, under a Julia that made stdout non-blocking, lost
    the rest of it): that is said, and the line that interrupted it is shown as it came.
    """
    copying = None

    def show(text):
        nonlocal echo
        tail.append(text)
        if echo:
            try:
                sys.stdout.write(text)
                sys.stdout.flush()
            except (OSError, ValueError, UnicodeError):
                echo = False

    for line in stream:
        text = line.strip()
        if TIMING.match(text):
            continue
        if copying:
            if text == COPY_END % copying:
                copying = None
                continue
            if BASE64_LINE.match(text):
                continue
            show("(... the copy of %s was cut short: its end never arrived, so `robotarium "
                 "fetch` cannot rebuild the file from this copy)\n" % copying)
            copying = None
        begun = COPY_BEGIN.match(text)
        if begun:
            copying = begun.group(1)
            line = "(a copy of %s, %.1f KB, printed for the testbed's log.txt)\n" % (
                copying, int(begun.group(2)) / 1e3)
        show(line if line.endswith("\n") else line + "\n")


def _interrupt(process) -> None:
    """
    Ctrl-C on POSIX; Ctrl-Break on Windows, which the shim takes as Ctrl-C. Either way the run
    writes its report on the way out. Where neither can be sent, terminate.
    """
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            process.send_signal(signal.SIGINT)
        else:
            process.send_signal(signal.CTRL_BREAK_EVENT)
    except (OSError, ValueError, AttributeError):
        try:
            process.terminate()
        except OSError:
            pass


def _kill(process) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _stop_with_this_process():
    """
    Makes a SIGTERM or SIGHUP to this process -- a closed terminal, `timeout`, a CI runner
    cancelling the job -- end the run too, instead of leaving it running unattended; returns a
    function that puts the previous handlers back. Only the main thread can install handlers;
    elsewhere this does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = {}

    def leave(number, frame):
        raise SystemExit(128 + number)

    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        # Only the default (die) is replaced: `nohup` sets SIGHUP to ignored, and a program
        # embedding this package may have its own handler, and both are kept as they are.
        if number is None or signal.getsignal(number) is not signal.SIG_DFL:
            continue
        try:
            previous[number] = signal.signal(number, leave)
        except (OSError, ValueError):
            pass

    def restore():
        for number, handler in previous.items():
            signal.signal(number, handler)
    return restore
