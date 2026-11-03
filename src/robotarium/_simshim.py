"""
Runs a main file under the Robotarium simulator and reports what the simulator saw.

    python -m robotarium._simshim MAIN_FILE

Run by simulate.py, with the bundle directory as the working directory, never by hand. It wraps
rps.robotarium.Robotarium before the main file runs -- the file itself is not touched -- to count
the steps of the drive to the initial poses and of the experiment, note how many robots each
Robotarium was made with, collect the simulator's own error counts, and see whether the
experiment ended with debug() or call_at_scripts_end(). It writes all of that as JSON to the path
in $ROBOTARIUM_SIM_REPORT when the main file finishes, however it finishes.

$ROBOTARIUM_SIM_FAST=1 turns off the simulator's real-time pacing, which changes how long the
run takes to watch and nothing about what it does. skip_initialization=True is overridden,
since the testbed always drives the robots to their initial poses first. Past
$ROBOTARIUM_SIM_MAX_STEPS steps the run is stopped: it could not be submitted anyway.

It also times the main file's own loop: the wall clock between one step() returning and the
next being called, which on the testbed has to fit in its 33 ms step.

Recording, set by simulate.py:

    $ROBOTARIUM_SIM_VIDEO        an .mp4 to write the run to, through ffmpeg
    $ROBOTARIUM_SIM_FPS          its frames per second
    $ROBOTARIUM_SIM_EVERY        one frame every this many steps
    $ROBOTARIUM_SIM_DPI          the figure's resolution, for the video and the stills
    $ROBOTARIUM_SIM_FRAMES       stills to save, at experiment times in seconds, and "end"
    $ROBOTARIUM_SIM_FRAMES_DIR   where they go
    $ROBOTARIUM_SIM_FORMAT       png or pdf
    $ROBOTARIUM_SIM_SEED         seeds random and numpy.random before the main file runs

Recording turns the figure on (show_figure=True) and sets matplotlib's figure.dpi before rps
creates it. Headless, the figure is drawn only for the frames kept: rps's own per-step redraw
(draw_idle under Agg renders at once, for nobody) is turned off. Time 0 is the experiment's
first step, after the drive to the initial poses: frame k is taken as step() is called for the
kth time after the drive, when the robots (drawn at the end of the step before) and whatever
the main file drew since both show the poses at k x 0.033 s; the still reports that step's
time. The "end" still is taken at r.debug() (or at exit): the robots as the last step left them,
and what the main file drew before that step, one step behind them. A recording that fails
stops recording and is reported; it never fails the run.
"""

import atexit
import importlib.util
import json
import math
import os
import random
import runpy
import signal
import sys
import time
import traceback
import warnings
from pathlib import Path

REPORT = {
    "rps": None,
    "robots": [],
    "init_steps": 0,
    "steps": 0,
    "errors": {},
    "ended": False,
    "exception": None,
    "wall_seconds": None,
    "skipped_initialization": False,
    "too_long": False,
    "startup_seconds": None,
    "loop_ms": None,
    "julia": None,
    "seed": None,
    "figure_forced": False,
    "video": None,
    "stills": {},
    "frames_missing": [],
    "recording_error": None,
}

_INSTANCES = []
_STARTED = time.time()

#: Steps after which the run is stopped: past the form's maximum duration. 0 for no limit.
MAX_STEPS = int(os.environ.get("ROBOTARIUM_SIM_MAX_STEPS") or 0)

#: The testbed's fixed step, as limits.STEP_SECONDS (this file imports nothing of the package).
STEP_SECONDS = 0.033

#: The wall clock, in seconds, of each interval between the main file's step() calls.
_INTERVALS = []
_LAST_RETURN = [None]


def still_step(seconds: float) -> int:
    """The experiment step a still at `seconds` is taken at: the first at or after it."""
    return int(math.ceil(float(seconds) / STEP_SECONDS - 1e-9))


class Recorder:
    """
    The run's video and stills, captured from the figure of the first Robotarium that has one.

    Args:
        video (str): The .mp4 to write, or None.
        fps (int): Its frames per second.
        every (int): Keep one frame in this many steps.
        dpi (int): The stills' resolution (the figure's is set to it before rps makes it).
        frames (list): Experiment times in seconds, and "end", to save stills at.
        frames_dir (str): Where the stills go.
        file_format (str): "png" or "pdf".
    """

    def __init__(self, video, fps, every, dpi, frames, frames_dir, file_format):
        self.video_path = video
        self.fps = fps
        self.every = max(1, every)
        self.dpi = dpi
        self.frames_dir = Path(frames_dir) if frames_dir else None
        self.format = file_format
        self.end = "end" in frames
        self.stills = {}
        for each in frames:
            if each != "end":
                self.stills.setdefault(still_step(each), []).append(float(each))
        self.robotarium = None
        self.figure = None
        self.video = None
        self.last = None
        self.finished = False

    @property
    def wanted(self) -> bool:
        return bool(self.video_path or self.stills or self.end)

    def attach(self, robotarium) -> None:
        """Records robotarium's figure, if it is the first with one."""
        if self.robotarium is not None:
            return
        figure = getattr(robotarium, "_fig", None) or getattr(robotarium, "figure", None)
        if figure is None or not hasattr(figure, "savefig"):
            REPORT["recording_error"] = ("the Robotarium made no figure to record (rps did not "
                                         "create one with show_figure=True)")
            return
        self.robotarium = robotarium
        self.figure = figure
        if self.video_path:
            try:
                runtime = _private_runtime()
                self.video = runtime.VideoRecorder(figure, self.video_path, fps=self.fps)
            except Exception as error:  # noqa: BLE001 -- the stills can still be taken
                REPORT["recording_error"] = "no video: %s: %s" % (type(error).__name__, error)
                self.video = None
                return
            if not self.video.enabled:
                REPORT["recording_error"] = "ffmpeg is not on PATH, so no video was written"
                self.video = None

    def capture(self, robotarium, step: int) -> None:
        """
        What to keep of experiment step `step`, before it is taken. Runs inside the main
        file's step(), so a recording that fails stops recording and is reported; it never
        fails the run.
        """
        if robotarium is not self.robotarium or self.finished:
            return
        self.last = step
        try:
            if self.video is not None and step % self.every == 0:
                self.video.frame()
            for seconds in self.stills.pop(step, ()):
                self._still(_still_label(seconds), step * STEP_SECONDS)
        except Exception as error:  # noqa: BLE001 -- see above
            self._fail(error)

    def finish(self) -> None:
        """
        The "end" still and the video's last frame: the robots as the last step left them,
        with what the main file drew before that step (one step behind them).
        """
        if self.finished:
            return
        self.finished = True
        if self.robotarium is None:
            return  # no figure, or rps never ran: nothing was due
        steps = REPORT["steps"]
        try:
            if self.end:
                self._still("end", steps * STEP_SECONDS)
            if self.video is not None and self.last is not None:
                self.video.frame()  # the poses after the last step
        except Exception as error:  # noqa: BLE001 -- the video is still closed below
            REPORT["recording_error"] = "%s: %s" % (type(error).__name__, error)
        finally:
            self._close_video()
        REPORT["frames_missing"] = sorted(seconds for each in self.stills.values()
                                          for seconds in each)

    def _fail(self, error) -> None:
        """Stops recording after `error`, keeping what was recorded, and reports it."""
        REPORT["recording_error"] = "%s: %s; nothing more was recorded" % (
            type(error).__name__, error)
        self.finished = True
        self.stills.clear()
        self._close_video()

    def _close_video(self) -> None:
        if self.video is None:
            return
        video, self.video = self.video, None
        try:
            path = video.close()
        except Exception as error:  # noqa: BLE001 -- a dead ffmpeg, a full disk
            REPORT["recording_error"] = REPORT["recording_error"] or "%s: %s" % (
                type(error).__name__, error)
            path = None
        REPORT["video"] = str(Path(path).resolve()) if path else None

    def _still(self, label: str, seconds: float) -> None:
        """A still, saved as label.<format>; `seconds` is its step's time, k x 0.033 s."""
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        path = self.frames_dir / ("%s.%s" % (label, self.format))
        self.figure.savefig(str(path), dpi=self.dpi, facecolor="white")
        REPORT["stills"][label] = {"path": str(path.resolve()), "seconds": round(seconds, 3)}


def _still_label(seconds: float) -> str:
    """t14s, t14p5s: a file name with no second dot, which \\includegraphics needs."""
    text = ("%.3f" % float(seconds)).rstrip("0").rstrip(".")
    return "t%ss" % text.replace(".", "p")


def _private_runtime():
    """
    This package's robotarium_runtime, loaded under a name of its own, for VideoRecorder: the
    main file's `import robotarium_runtime` must still get the copy the bundle carries.
    """
    path = Path(__file__).resolve().parent.parent / "robotarium_runtime.py"
    if not path.is_file():
        spec = importlib.util.find_spec("robotarium_runtime")
        path = Path(spec.origin)
    spec = importlib.util.spec_from_file_location("_robotarium_simshim_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recorder_from_environment():
    environ = os.environ
    frames = [each.strip() for each in (environ.get("ROBOTARIUM_SIM_FRAMES") or "").split(",")
              if each.strip()]
    frames = [each if each == "end" else float(each) for each in frames]
    every = int(environ.get("ROBOTARIUM_SIM_EVERY") or 1)
    recorder = Recorder(video=environ.get("ROBOTARIUM_SIM_VIDEO") or None,
                        fps=int(environ.get("ROBOTARIUM_SIM_FPS") or 30),
                        every=every, dpi=int(environ.get("ROBOTARIUM_SIM_DPI") or 200),
                        frames=frames, frames_dir=environ.get("ROBOTARIUM_SIM_FRAMES_DIR"),
                        file_format=environ.get("ROBOTARIUM_SIM_FORMAT") or "png")
    return recorder if recorder.wanted else None


RECORDER = None


def _headless() -> bool:
    return os.environ.get("MPLBACKEND", "").lower() == "agg"


def _quiet_canvas(robotarium) -> None:
    """
    Headless, makes rps's per-step draw_idle() and flush_events() do nothing: under Agg,
    draw_idle renders the whole figure at once, every step, for nobody. Frames that are kept
    are drawn by canvas.draw() and savefig(), which this leaves alone.
    """
    if not _headless():
        return
    figure = getattr(robotarium, "_fig", None) or getattr(robotarium, "figure", None)
    canvas = getattr(figure, "canvas", None)
    if canvas is None or getattr(canvas, "_robotarium_quiet", False):
        return
    canvas.draw_idle = lambda *args, **kwargs: None
    canvas.flush_events = lambda *args, **kwargs: None
    canvas._robotarium_quiet = True


def loop_statistics(intervals: list):
    """Median, 99th percentile and maximum of the intervals, in milliseconds; None for none."""
    if not intervals:
        return None
    ordered = sorted(intervals)

    def percentile(fraction):
        position = fraction * (len(ordered) - 1)
        low = int(math.floor(position))
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {"median": round(1000 * percentile(0.5), 3), "p99": round(1000 * percentile(0.99), 3),
            "max": round(1000 * ordered[-1], 3), "count": len(ordered)}


def stop_too_long() -> None:
    """
    Ends the run past MAX_STEPS, its exit handlers and the report run first. Not by raising:
    an exception from step() can be caught by the main file -- a bare `except:` would catch
    even a BaseException -- and a loop around it would then spin for ever.
    """
    REPORT["too_long"] = True
    print("robotarium simulate: stopped after %d steps, past the form's maximum duration."
          % MAX_STEPS, file=sys.stderr)
    # What exiting would have run: a RunLogger's last save, the main file's own handlers, and
    # this report (registered first, so run last).
    try:
        atexit._run_exitfuncs()
    finally:
        write_report()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(4)


def install(fast: bool) -> bool:
    """Wraps rps.robotarium.Robotarium; False when rps cannot be imported."""
    try:
        import rps
        import rps.robotarium as module
    except ImportError as error:
        REPORT["exception"] = "ImportError: %s" % error
        return False
    REPORT["rps"] = str(Path(rps.__file__).resolve().parent)

    cls = module.Robotarium
    original_init = cls.__init__
    original_step = cls.step

    def __init__(self, *args, **kwargs):
        if REPORT["startup_seconds"] is None:
            REPORT["startup_seconds"] = round(time.time() - _STARTED, 3)
        if fast:
            kwargs["sim_in_real_time"] = False
        if RECORDER is not None and len(args) < 2:
            # The video and the stills are pictures of the figure, so there has to be one.
            if kwargs.get("show_figure") is False:
                REPORT["figure_forced"] = True
            kwargs["show_figure"] = True
            # rps takes no dpi; it makes its figure at matplotlib's default, read here.
            import matplotlib
            matplotlib.rcParams["figure.dpi"] = RECORDER.dpi
        # The testbed always drives the robots to their initial poses; a run that skips the
        # drive is shorter than the one the testbed will do, and starts from somewhere else.
        # Only touched when the main file passed it: rps before V2 has no such keyword.
        if kwargs.get("skip_initialization"):
            print("robotarium simulate: ignoring skip_initialization=True -- the testbed always "
                  "drives the robots to their initial poses first.", file=sys.stderr)
            REPORT["skipped_initialization"] = True
            kwargs["skip_initialization"] = False
        REPORT["robots"].append(kwargs.get("number_of_robots", -1))
        _INSTANCES.append(self)
        original_init(self, *args, **kwargs)
        _quiet_canvas(self)
        if RECORDER is not None:
            RECORDER.attach(self)

    def step(self, *args, **kwargs):
        entered = time.perf_counter()
        if getattr(self, "initializing", False):
            REPORT["init_steps"] += 1
            _LAST_RETURN[0] = None
        else:
            if _LAST_RETURN[0] is not None:
                _INTERVALS.append(entered - _LAST_RETURN[0])
            if RECORDER is not None:
                RECORDER.capture(self, REPORT["steps"])
            REPORT["steps"] += 1
        if MAX_STEPS and REPORT["init_steps"] + REPORT["steps"] > MAX_STEPS:
            stop_too_long()
        try:
            return original_step(self, *args, **kwargs)
        finally:
            if not getattr(self, "initializing", False):
                _LAST_RETURN[0] = time.perf_counter()

    cls.__init__ = __init__
    cls.step = step

    # The figure exists from here, before the drive to the initial poses is drawn.
    original_visualization = getattr(cls, "_initialize_visualization", None)
    if original_visualization is not None:
        def _initialize_visualization(self, *args, **kwargs):
            result = original_visualization(self, *args, **kwargs)
            _quiet_canvas(self)
            return result
        cls._initialize_visualization = _initialize_visualization

    for name in ("debug", "call_at_scripts_end"):
        original = getattr(cls, name, None)
        if original is not None:
            setattr(cls, name, _marking_end(original))
    return True


def _marking_end(method):
    def wrapped(self, *args, **kwargs):
        REPORT["ended"] = True
        if RECORDER is not None and self is RECORDER.robotarium:
            RECORDER.finish()
        return method(self, *args, **kwargs)
    return wrapped


def finish_recording() -> None:
    if RECORDER is not None:
        try:
            RECORDER.finish()
        except Exception as error:  # noqa: BLE001 -- at exit, report rather than raise
            REPORT["recording_error"] = "%s: %s" % (type(error).__name__, error)


def write_report() -> None:
    errors = {}
    for instance in _INSTANCES:
        for key, count in (getattr(instance, "_errors", None) or {}).items():
            errors[key] = errors.get(key, 0) + int(count)
    REPORT["errors"] = errors
    REPORT["wall_seconds"] = round(time.time() - _STARTED, 2)
    REPORT["loop_ms"] = loop_statistics(_INTERVALS)
    runtime = sys.modules.get("robotarium_runtime")
    if runtime is not None and hasattr(runtime, "julia_timings"):
        try:
            REPORT["julia"] = runtime.julia_timings() or None
        except Exception:  # noqa: BLE001 -- an older vendored copy, or a broken one
            pass
    destination = os.environ.get("ROBOTARIUM_SIM_REPORT")
    if destination:
        Path(destination).write_text(json.dumps(REPORT), encoding="utf-8")


def _interrupted(number, frame):
    raise KeyboardInterrupt


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m robotarium._simshim MAIN_FILE", file=sys.stderr)
        return 2
    main_file = Path(sys.argv[1]).resolve()
    atexit.register(write_report)

    global RECORDER
    RECORDER = _recorder_from_environment()

    seed = os.environ.get("ROBOTARIUM_SIM_SEED")
    if seed not in (None, ""):
        # rps draws the robots' starting points with random.sample and the drive's waypoints
        # with numpy.random: seeded, two runs start from the same poses.
        REPORT["seed"] = int(seed)
        random.seed(int(seed))
        try:
            import numpy
            numpy.random.seed(int(seed))
        except ImportError:
            pass

    # Headless, the simulator's own plt.show() has nothing to show and matplotlib says so on
    # every run; that warning is about how the run is watched, not about the experiment.
    if os.environ.get("MPLBACKEND", "").lower() == "agg":
        warnings.filterwarnings("ignore", message=".*non-interactive, and thus cannot be shown")

    # Windows has no SIGINT to send to another process; simulate.py sends Ctrl-Break instead,
    # and it means the same here: stop, and report on the way out.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _interrupted)

    if not install(fast=os.environ.get("ROBOTARIUM_SIM_FAST") == "1"):
        print("The Robotarium simulator (rps) is not importable: %s" % REPORT["exception"],
              file=sys.stderr)
        return 3
    # Registered after the report, so run before it; and after install() has imported rps,
    # and with it pyplot, whose own exit handler closes every figure: handlers run last
    # registered first, so the "end" still and the video's last frame come before that.
    atexit.register(finish_recording)

    # As `python main.py` would: the main file's directory first on the path, argv[0] the file.
    sys.path.insert(0, str(main_file.parent))
    sys.argv = [str(main_file)]
    try:
        runpy.run_path(str(main_file), run_name="__main__")
    except SystemExit as exit_:
        code = exit_.code
        if code is None or isinstance(code, int):
            return code or 0
        # sys.exit("message"): Python prints the message and exits 1, and so does this.
        print(code, file=sys.stderr)
        REPORT["exception"] = "SystemExit: %s" % (code,)
        return 1
    except BaseException:  # noqa: BLE001 -- the report is the point; the traceback is printed
        REPORT["exception"] = traceback.format_exc(limit=-8)
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
