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
Duration" asks for; limits.suggested_duration adds headroom. A duration of "auto" in
robotarium.toml is filled in from exactly that.

The simulator is the `rps` package from robotarium_python_simulator. It is found the way Python
finds any package, or under [simulator] path / --simulator / $ROBOTARIUM_SIMULATOR, which is put
on the run's PYTHONPATH: point it at a clone to run against that clone without installing it.
"""

import collections
import json
import logging
import os
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

SIMULATOR_VARIABLE = "ROBOTARIUM_SIMULATOR"

log = logging.getLogger(__name__)


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
    output: str = ""

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

    def findings(self, experiment=None) -> list:
        """
        The run's verdict as Findings, against the experiment's form fields when given.

        Args:
            experiment (Experiment): What the form will say; None to judge the run alone.
        """
        findings = []
        add = findings.append

        if self.timed_out:
            add(Finding(ERROR, "The run was stopped at the timeout, after %d steps."
                               % self.total_steps))
            return findings
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
        if self.returncode != 0:
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
        if self.total_steps:
            add(Finding(INFO, "%d steps (%d driving to the initial poses): about %.0f s on the "
                              "testbed. Suggested estimated duration: %d s."
                              % (self.total_steps, self.init_steps, self.testbed_seconds,
                                 self.suggested_duration)))
            if experiment is not None and isinstance(experiment.duration, int) \
                    and self.testbed_seconds > experiment.duration:
                add(Finding(ERROR, "The run takes about %.0f s on the testbed, and the form "
                                   "would declare %d s; the testbed stops a run that outlasts "
                                   "its declared duration. Declare %d, or \"auto\"."
                                   % (self.testbed_seconds, experiment.duration,
                                      self.suggested_duration)))
            if self.testbed_seconds > limits.MAX_DURATION_SECONDS:
                add(Finding(ERROR, "About %.0f s is over the form's %d s maximum."
                                   % (self.testbed_seconds, limits.MAX_DURATION_SECONDS)))
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
        log_dir=None, echo: bool = True, python: str = None) -> SimulationReport:
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
    """
    if bundle.main.suffix != ".py":
        raise ValueError("Only a Python main file can be simulated, not %s." % bundle.main.name)
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
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        environment["ROBOTARIUM_LOG_DIR"] = str(Path(log_dir).resolve())

    with tempfile.TemporaryDirectory() as scratch:
        report_path = Path(scratch) / "report.json"
        environment["ROBOTARIUM_SIM_REPORT"] = str(report_path)
        command = [python or sys.executable, "-m", "robotarium._simshim", str(bundle.main)]
        log.debug("running %s in %s", " ".join(command), bundle.directory)

        tail = collections.deque(maxlen=60)
        process = subprocess.Popen(command, cwd=bundle.directory, env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   bufsize=1)
        stopped = threading.Event()
        timer = None
        if timeout:
            def stop():
                # Ctrl-C first, so the shim's report is still written; a kill if that is
                # ignored.
                stopped.set()
                try:
                    if os.name == "posix":
                        process.send_signal(signal.SIGINT)
                    else:
                        process.terminate()
                except OSError:
                    pass
                killer = threading.Timer(10.0, process.kill)
                killer.daemon = True
                killer.start()
            timer = threading.Timer(float(timeout), stop)
            timer.start()
        try:
            for line in process.stdout:
                tail.append(line)
                if echo:
                    sys.stdout.write(line)
                    sys.stdout.flush()
            returncode = process.wait()
        finally:
            if timer is not None:
                timer.cancel()
            if process.poll() is None:
                process.kill()

        data = {}
        if report_path.is_file():
            data = json.loads(report_path.read_text(encoding="utf-8"))

    known = {key: data[key] for key in ("rps", "robots", "init_steps", "steps", "errors",
                                        "ended", "exception", "wall_seconds",
                                        "skipped_initialization") if key in data}
    return SimulationReport(returncode=returncode, timed_out=stopped.is_set(),
                            output="".join(tail), **known)

