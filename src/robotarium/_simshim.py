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
"""

import atexit
import json
import os
import runpy
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
}

_INSTANCES = []
_STARTED = time.time()

#: Steps after which the run is stopped: past the form's maximum duration. 0 for no limit.
MAX_STEPS = int(os.environ.get("ROBOTARIUM_SIM_MAX_STEPS") or 0)


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
        if fast:
            kwargs["sim_in_real_time"] = False
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

    def step(self, *args, **kwargs):
        if getattr(self, "initializing", False):
            REPORT["init_steps"] += 1
        else:
            REPORT["steps"] += 1
        if MAX_STEPS and REPORT["init_steps"] + REPORT["steps"] > MAX_STEPS:
            stop_too_long()
        return original_step(self, *args, **kwargs)

    cls.__init__ = __init__
    cls.step = step

    for name in ("debug", "call_at_scripts_end"):
        original = getattr(cls, name, None)
        if original is not None:
            setattr(cls, name, _marking_end(original))
    return True


def _marking_end(method):
    def wrapped(self, *args, **kwargs):
        REPORT["ended"] = True
        return method(self, *args, **kwargs)
    return wrapped


def write_report() -> None:
    errors = {}
    for instance in _INSTANCES:
        for key, count in (getattr(instance, "_errors", None) or {}).items():
            errors[key] = errors.get(key, 0) + int(count)
    REPORT["errors"] = errors
    REPORT["wall_seconds"] = round(time.time() - _STARTED, 2)
    destination = os.environ.get("ROBOTARIUM_SIM_REPORT")
    if destination:
        Path(destination).write_text(json.dumps(REPORT), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m robotarium._simshim MAIN_FILE", file=sys.stderr)
        return 2
    main_file = Path(sys.argv[1]).resolve()
    atexit.register(write_report)

    # Headless, the simulator's own plt.show() has nothing to show and matplotlib says so on
    # every run; that warning is about how the run is watched, not about the experiment.
    if os.environ.get("MPLBACKEND", "").lower() == "agg":
        warnings.filterwarnings("ignore", message=".*non-interactive, and thus cannot be shown")

    if not install(fast=os.environ.get("ROBOTARIUM_SIM_FAST") == "1"):
        print("The Robotarium simulator (rps) is not importable: %s" % REPORT["exception"],
              file=sys.stderr)
        return 3

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
