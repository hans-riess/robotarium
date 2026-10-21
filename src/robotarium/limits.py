"""
What the Robotarium's submission form and testbed accept, as known offline.

These are the limits a submission is checked against before anything is sent. The site also
publishes its live limits (GET settings/, see client.RobotariumClient.settings), and `submit`
checks against those too once it has logged in; the numbers here are what lets `robotarium
check` refuse a bad submission without a network connection or credentials.
"""

#: The arena physically holds this many GRITSBots. The simulator accepts up to 50, which is why
#: a submission that simulates cleanly can still be one the testbed cannot run.
MIN_ROBOTS = 1
MAX_ROBOTS = 18

#: The form's cap on "Estimated Duration", in seconds.
MAX_DURATION_SECONDS = 600

#: What the upload form accepts. Directories are refused outright and so is every other
#: extension -- notably .json, .yaml, .toml, .txt and .zip -- which is why configuration has to
#: travel inside a .py file (see bundle.py) and why juliapkg.json cannot travel at all.
ALLOWED_EXTENSIONS = (
    ".py", ".m", ".jl",
    ".mat", ".npy",
    ".mex", ".mexw64", ".mexa64", ".mexmaci64",
    ".jpg", ".jpeg", ".png", ".gif", ".tiff", ".bmp",
)

#: What the form can run as the main file.
MAIN_FILE_EXTENSIONS = (".py", ".m")

#: The testbed advances at this fixed rate whatever an experiment's own time step says. A run's
#: wall clock is its number of Robotarium.step() calls times this, and the wall clock is what the
#: form's "Estimated Duration" asks for. Matches ARobotarium.TIME_STEP in the simulator.
STEP_SECONDS = 0.033

#: What the form calls a GRITSBot experiment, as opposed to "quad".
EXPERIMENT_TYPE = "ground"


def suggested_duration(steps: int) -> int:
    """
    An "Estimated Duration" for a run of `steps` Robotarium steps: the wall clock with a quarter
    again of headroom, rounded up to the minute and capped at the form's maximum. The testbed
    stops a run that outlasts what was declared, and declaring too much costs nothing.

    Args:
        steps (int): Calls to Robotarium.step(), the drive to the initial poses included.
    """
    wall_clock = steps * STEP_SECONDS
    minutes = -(-wall_clock * 1.25 // 60)
    return int(min(MAX_DURATION_SECONDS, max(60, minutes * 60)))
