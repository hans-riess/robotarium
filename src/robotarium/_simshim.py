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
run takes to watch and nothing about what it does.
"""

import atexit
import json
import os
import runpy
import sys
import time
import traceback
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
}

_INSTANCES = []
_STARTED = time.time()


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
        REPORT["robots"].append(kwargs.get("number_of_robots", -1))
        _INSTANCES.append(self)
        original_init(self, *args, **kwargs)

    def step(self, *args, **kwargs):
        if getattr(self, "initializing", False):
            REPORT["init_steps"] += 1
        else:
            REPORT["steps"] += 1
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
        return code if isinstance(code, int) else (0 if code is None else 1)
    except BaseException:  # noqa: BLE001 -- the report is the point; the traceback is printed
        REPORT["exception"] = traceback.format_exc(limit=-8)
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
