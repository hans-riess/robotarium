"""
`robotarium init`: a new experiment directory that simulates and submits as it stands.

    robotarium init my_experiment               a Python experiment
    robotarium init my_experiment --julia       ... with a backend.jl, the robosheaf way
    robotarium init my_test --example two_robots    a ready-made example, to try the CLI on

Every file is a starting point to edit, and none is overwritten unless --force is given.
"""

from importlib import resources
from pathlib import Path

MANIFEST = '''\
# One Robotarium experiment. docs/manifest.md in the robotarium package lists every key.

[experiment]
title = "__TITLE__ ({timestamp})"   # {date} {time} {timestamp} {git} {robots} {main}
description = """
__DESCRIPTION__
"""
duration = "auto"   # seconds, "90s", "2m" -- or "auto": measured by a simulator run
robots = __ROBOTS__
main = "main.py"
files = [__FILES__]   # supporting files, uploaded flat beside main.py; globs work

# The run's parameters. The form takes no .toml, so they travel inside the bundle and the main
# file reads them with robotarium_runtime.config().
[config]
radius = 0.5
iterations = 1500
__JULIA__
[simulator]
# path = "../robotarium_python_simulator"   # a clone to run against, if rps is not installed
fast = true          # skip real-time pacing
headless = true      # no window; `robotarium simulate --show` opens one

[submit]
draft = true         # save on the site rather than queue it; set false when you trust it
simulate = true      # refuse to submit anything the simulator rejects
'''

JULIA_TABLE = '''
# Julia packages backend.jl loads. Declared to juliapkg before Julia starts, on the testbed and
# here alike. `robotarium julia lookup NAME` prints an entry.
[julia]
packages = [
    { name = "JSON3", uuid = "0f8b85d8-7281-11e9-16c2-39a750bddbf1", version = "1" },
]
'''

MAIN = '''\
"""
__TITLE__: __ROBOTS__ robots drive to evenly spaced points on a circle.

    robotarium simulate     rehearse it in the simulator
    robotarium submit       send it to the Robotarium
"""

import numpy as np

import rps.robotarium as robotarium
from rps.utilities.barrier_certificates import create_si_barrier_certificate_with_boundary
from rps.utilities.controllers import create_si_position_controller
from rps.utilities.transformations import create_si_to_uni_mapping

from robotarium_runtime import RunLogger, config

# From [config] in robotarium.toml; the default is what `python main.py` uses without one.
CONFIG = config(default={"radius": 0.5, "iterations": 1500})

N = __ROBOTS__
ITERATIONS = int(CONFIG["iterations"])

r = robotarium.Robotarium(number_of_robots=N, show_figure=True, sim_in_real_time=True)

# Speeds and the projection distance chosen so the wheels can follow every command: the
# simulator warns, and the testbed deviates, when they cannot.
si_to_uni, uni_to_si = create_si_to_uni_mapping(projection_distance=0.08)
controller = create_si_position_controller(velocity_magnitude_limit=0.1)
barrier = create_si_barrier_certificate_with_boundary(magnitude_limit=0.1)

angles = np.linspace(0, 2 * np.pi, N, endpoint=False)
goals = float(CONFIG["radius"]) * np.vstack([np.cos(angles), np.sin(angles)])

# Written beside main.py, which on the testbed is what comes back with the results; rewritten
# every 300 steps so a run stopped at its declared duration still leaves its data.
log = RunLogger("circle", autosave_every=300, meta={"goals": goals.tolist()})

for step in range(ITERATIONS):
    x = r.get_poses()
    xi = uni_to_si(x)

    dxi = controller(xi, goals)
    dxi = barrier(dxi, xi)
    dxu = si_to_uni(dxi, x)

    r.set_velocities(np.arange(N), dxu)
    log.record(poses=x, velocities=dxu)
    r.step()

log.note("finished %d steps" % ITERATIONS)
log.save()

# The Robotarium requires this call at the end of every experiment.
r.debug()
'''

MAIN_JULIA = '''\
"""
__TITLE__: __ROBOTS__ robots hold a circular formation computed in Julia.

backend.jl builds the formation and its graph Laplacian; this file runs displacement-based
formation control with them, x' = -k (x - targets) L.

    robotarium simulate     rehearse it in the simulator (the first run installs Julia)
    robotarium submit       send it to the Robotarium
"""

import numpy as np

import rps.robotarium as robotarium
from rps.utilities.barrier_certificates import create_si_barrier_certificate_with_boundary
from rps.utilities.transformations import create_si_to_uni_mapping

from robotarium_runtime import RunLogger, as_arrays, call_json, config, load_julia_backend

CONFIG = config(default={"radius": 0.5, "iterations": 1500})

N = __ROBOTS__
ITERATIONS = int(CONFIG["iterations"])
GAIN = 1.0
MAX_SPEED = 0.1

# Before anything else touches Julia: this declares [julia] packages to juliapkg, starts Julia,
# and include()s backend.jl from beside this file.
backend = load_julia_backend("backend.jl")
formation = as_arrays(call_json(backend.formation, {"robots": N, "radius": CONFIG["radius"]}),
                      ["targets", "laplacian"])
targets, laplacian = formation["targets"], formation["laplacian"]

r = robotarium.Robotarium(number_of_robots=N, show_figure=True, sim_in_real_time=True)

si_to_uni, uni_to_si = create_si_to_uni_mapping(projection_distance=0.08)
barrier = create_si_barrier_certificate_with_boundary(magnitude_limit=MAX_SPEED)

log = RunLogger("formation", autosave_every=300,
                meta={"targets": targets.tolist(), "laplacian": laplacian.tolist()})

for step in range(ITERATIONS):
    x = r.get_poses()
    xi = uni_to_si(x)

    dxi = -GAIN * (xi - targets) @ laplacian
    speeds = np.linalg.norm(dxi, axis=0)
    dxi[:, speeds > MAX_SPEED] *= MAX_SPEED / speeds[speeds > MAX_SPEED]
    dxi = barrier(dxi, xi)
    dxu = si_to_uni(dxi, x)

    r.set_velocities(np.arange(N), dxu)
    log.record(poses=x, velocities=dxu)
    r.step()

log.note("finished %d steps" % ITERATIONS)
log.save()

r.debug()
'''

BACKEND_JL = '''\
# The Julia half of the experiment. robotarium_runtime.load_julia_backend include()s this file
# and returns the module, so `backend.formation(...)` in main.py calls the function below.
#
# Every package used here must be declared under [julia] in robotarium.toml: nothing else
# installs it on the testbed. The standard library (LinearAlgebra, ...) needs no declaring.
module Backend

using JSON3
using LinearAlgebra

export formation

"""
    formation(spec_json) -> (targets = 2xN matrix, laplacian = NxN matrix)

Points evenly spaced on a circle, and the Laplacian of the cycle through them. Takes JSON --
robotarium_runtime.call_json sends {"robots": N, "radius": r} -- and returns a NamedTuple whose
fields robotarium_runtime.as_arrays turns into numpy arrays.
"""
function formation(spec_json::AbstractString)
    spec = JSON3.read(spec_json)
    n = Int(spec["robots"])
    radius = Float64(spec["radius"])

    angles = [2pi * (k - 1) / n for k in 1:n]
    targets = vcat((radius .* cos.(angles))', (radius .* sin.(angles))')

    adjacency = zeros(Float64, n, n)
    for i in 1:n, j in (mod1(i - 1, n), mod1(i + 1, n))
        if i != j
            adjacency[i, j] = 1.0
        end
    end
    laplacian = Diagonal(vec(sum(adjacency; dims = 2))) - adjacency

    return (targets = targets, laplacian = Matrix(laplacian))
end

end
'''

GITIGNORE = '''\
# Credentials: never commit them. See .env.example.
.env

# What `robotarium` builds. The submission ledger is the record of what was sent, so it is kept.
.robotarium/*
!.robotarium/submissions.jsonl

__pycache__/
*.npz
*.mp4
'''

ENV_EXAMPLE = '''\
# Copy to .env, beside robotarium.toml, and fill in. .gitignore keeps .env out of git; on a
# shared machine also run: chmod 600 .env
# Or skip the file altogether: `robotarium login` keeps the password in the system keyring.
ROBOTARIUM_USERNAME=you@example.edu
ROBOTARIUM_PASSWORD=
'''


def create(directory, julia: bool = False, robots: int = 4, title: str = None,
           force: bool = False) -> list:
    """
    Writes a new experiment into `directory`, and returns (path, written) for each file:
    written is False for a file that already existed and was left alone.

    Args:
        directory: Where; created if missing.
        julia (bool): Include a backend.jl and load it from main.py.
        robots (int): The number of robots, 1-18.
        title (str): The experiment's title; default from the directory's name.
        force (bool): Overwrite files that exist.
    """
    directory = Path(directory).expanduser().resolve()
    if not 1 <= int(robots) <= 18:
        raise ValueError("robots must be 1-18, not %s" % robots)
    title = title or directory.name.replace("_", " ").replace("-", " ").strip().capitalize() \
        or "Experiment"
    description = ("%d GRITSBots hold a circular formation computed in Julia." if julia
                   else "%d GRITSBots drive to evenly spaced points on a circle.") % int(robots)

    def fill(template: str) -> str:
        return (template.replace("__TITLE__", title).replace("__ROBOTS__", str(int(robots)))
                .replace("__DESCRIPTION__", description)
                .replace("__FILES__", '"backend.jl"' if julia else "")
                .replace("__JULIA__", JULIA_TABLE if julia else ""))

    files = {"robotarium.toml": fill(MANIFEST), "main.py": fill(MAIN_JULIA if julia else MAIN),
             ".gitignore": GITIGNORE, ".env.example": ENV_EXAMPLE}
    if julia:
        files["backend.jl"] = BACKEND_JL

    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in files.items():
        path = directory / name
        if path.exists() and not force:
            written.append((path, False))
            continue
        path.write_text(text, encoding="utf-8")
        written.append((path, True))
    return written


def examples() -> list:
    """The names of the examples shipped with the package (robotarium/examples/)."""
    root = resources.files("robotarium").joinpath("examples")
    return sorted(each.name for each in root.iterdir()
                  if each.is_dir() and each.joinpath("robotarium.toml").is_file())


def create_example(name: str, directory, force: bool = False) -> list:
    """
    Copies a shipped example into `directory`, with the same .gitignore and .env.example
    `create` writes, and returns (path, written) for each file as `create` does.

    Args:
        name (str): One of examples().
        directory: Where; created if missing.
        force (bool): Overwrite files that exist.
    """
    if name not in examples():
        raise ValueError("No example %r; there is %s." % (name, ", ".join(examples())))
    source = resources.files("robotarium").joinpath("examples", name)
    files = {each.name: each.read_text(encoding="utf-8") for each in source.iterdir()
             if each.is_file() and not each.name.endswith((".pyc", ".pyo"))}
    files.update({".gitignore": GITIGNORE, ".env.example": ENV_EXAMPLE})

    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for file_name, text in sorted(files.items()):
        path = directory / file_name
        if path.exists() and not force:
            written.append((path, False))
            continue
        path.write_text(text, encoding="utf-8")
        written.append((path, True))
    return written
