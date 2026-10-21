"""
General-purpose automation for Georgia Tech Robotarium submissions: describe an experiment once
(robotarium.toml, or an Experiment in Python), then bundle it, check it, rehearse it in the
simulator and submit it -- title, estimated duration, description, number of robots, the files
and which one is main -- without the web form.

    from robotarium import load_project, submit
    result = submit(load_project("."), draft=True)

robotarium_runtime, installed beside this package, is the half that travels with the experiment
to the testbed: a Julia backend loader, a run logger, a video recorder.
"""

from ._version import __version__
from .bundle import Bundle, BundleError, build
from .client import RobotariumClient, RobotariumError
from .credentials import Credentials, CredentialsError
from .experiment import Experiment, JuliaPackage, Project, load_project, parse_duration
from .findings import Finding
from .simulate import SimulationReport
from .submission import SubmissionRefused, SubmissionResult, prepare, submit

__all__ = [
    "__version__", "Bundle", "BundleError", "build", "RobotariumClient", "RobotariumError",
    "Credentials", "CredentialsError", "Experiment", "JuliaPackage", "Project", "load_project",
    "parse_duration", "Finding", "SimulationReport", "SubmissionRefused", "SubmissionResult",
    "prepare", "submit",
]
