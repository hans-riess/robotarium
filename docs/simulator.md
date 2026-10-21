# Rehearsing in the Robotarium simulator

`robotarium simulate` runs your experiment in the
[Robotarium Python simulator](https://github.com/robotarium/robotarium_python_simulator) (the
`rps` package) before it goes into the testbed's queue, and judges the run the way the testbed
will.

## Getting the simulator

`rps` isn't on PyPI. Use any one of these:

```bash
pip install -e ".[sim]"     # in your clone, venv active: numpy, matplotlib, cvxopt, rps from git

# or a clone you can read and edit, installed editable:
git clone https://github.com/robotarium/robotarium_python_simulator
pip install -e robotarium_python_simulator

# or a clone used in place, without installing it:
robotarium simulate --simulator ../robotarium_python_simulator
# ... or persistently, in robotarium.toml:  [simulator] path = "../robotarium_python_simulator"
# ... or for every project:                 export ROBOTARIUM_SIMULATOR=~/src/robotarium_python_simulator
```

## Running

```bash
robotarium simulate              # headless and fast: the verdict, as quickly as possible
robotarium simulate --show       # a window, in real time: to watch it
robotarium simulate --timeout 600
```

The main file runs from `.robotarium/bundle/`, the same flat directory of files that would be
uploaded, as a separate Python process. A run that works here therefore has the testbed's
layout: supporting files beside the main file, the generated `robotarium_bundle.py` and the
vendored `robotarium_runtime.py` present, and nothing from your project directory importable by
accident.

The main file isn't modified. A thin wrapper around `rps.robotarium.Robotarium` records what the
simulator saw:

| Measured | Judged |
|---|---|
| robots each `Robotarium(...)` was made with | an **error** if it differs from `robots` in `robotarium.toml` |
| steps driving to the initial poses, and steps after | the run's wall clock on the testbed: steps x 0.033 s |
| `robots_too_close`, `robots_outside_boundaries` | an **error**: the testbed rejects an experiment with either |
| `exceeded_actuator_limits` | a **warning**, with the share of steps affected |
| whether `r.debug()` or `r.call_at_scripts_end()` ran | a **warning** if not: the Robotarium requires it |
| the exit status | an **error** if the main file failed, with its last line |

Two things are always as on the testbed, whatever the main file asks for. The robots are
driven to their initial poses first, even if the main file passes `skip_initialization=True`,
because the testbed always does that drive and a run without it is shorter than the real one.
And the run never sees `ROBOTARIUM_USERNAME` or `ROBOTARIUM_PASSWORD`.

A run is stopped once it passes 600 s of testbed time (18,182 steps), the form's maximum, since
it could never be submitted. This also ends a drive to initial poses the robots can't reach,
which otherwise never finishes, and no `except` in the main file can catch it. A run cut short
this way, or by a timeout, gets no duration advice, because it doesn't show how long the whole
run takes.

`--timeout` bounds the wall clock as well. On Linux and macOS it stops the run with Ctrl-C, so
the report is still written and lists the run's collisions and robot count along with the
timeout. A run that ignores Ctrl-C is killed 10 s later. On Windows a timeout terminates the run
outright, so its step count is unknown. If `robotarium` itself is ended, by Ctrl-C, a closed
terminal, `timeout`, or a CI job being cancelled, the run is stopped with it rather than left
running.

The wall clock is also compared with the declared duration. A run longer than its estimated
duration is an error, because the testbed stops it, and the message suggests a duration with
headroom. `duration = "measured"` uses that suggestion; the default, `"auto"`, declares the
form's maximum, 600 s.

`robotarium simulate` exits non-zero on any error, so it can gate CI. `robotarium submit
--simulate`, or `[submit] simulate = true`, runs it before every submission and refuses to send
anything it rejects.

## Run logs

`RunLogger` writes into `.robotarium/runs/` during a simulation, rather than into the bundle.
Read a log back with:

```bash
robotarium inspect .robotarium/runs/circle_20261021_153102.npz
```

See [logging.md](logging.md).

## Where the simulator and the testbed differ

The simulator models the robots' unicycle dynamics, the arena's boundaries and collision radius,
and the wheels' speed limits. It doesn't model the physical robots' imperfect tracking, so an
experiment that's marginal in simulation (a near miss, a lot of saturated wheel commands) may
behave differently on the floor. It also uses whatever Python packages your machine has, while
the testbed has a fixed list. `robotarium check` compares your imports against that list (the
simulator's `libraries.txt`), which is read from your simulator clone when there is one, and
otherwise from the copy shipped with this package.
