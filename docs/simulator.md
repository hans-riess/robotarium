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
robotarium simulate --set controller=baseline        # a variant: see "Variants" below
robotarium simulate --video --frames 0,14,27,end     # a video and stills: see below
robotarium simulate --seed 1                         # the same start every time
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
| the main file's own time between `step()` calls | a **warning** if the 99th percentile is over the testbed's 33 ms step |
| Julia's startup (with a `backend.jl`) | counted against the duration with the steps: see [julia.md](julia.md#the-startup-budget) |

Two things are always as on the testbed, whatever the main file asks for. The robots are
driven to their initial poses first, even if the main file passes `skip_initialization=True`,
because the testbed always does that drive and a run without it is shorter than the real one.
And the run never sees `ROBOTARIUM_USERNAME` or `ROBOTARIUM_PASSWORD`.

A run is stopped once it passes 600 s of testbed time (18,182 steps), the form's maximum, since
it could never be submitted. This also ends a drive to initial poses the robots can't reach,
which otherwise never finishes, and no `except` in the main file can catch it. A run cut short
this way, or by a timeout, gets no duration advice, because it doesn't show how long the whole
run takes.

`--timeout` bounds the wall clock as well. It stops the run with Ctrl-C (Ctrl-Break on Windows,
which the run takes the same way), so the report is still written and lists the run's
collisions and robot count along with the timeout. A run that ignores it is killed 10 s later,
and its step count is then unknown. If `robotarium` itself is ended, by Ctrl-C, a closed
terminal, `timeout`, or a CI job being cancelled, the run is stopped with it rather than left
running.

The wall clock is also compared with the declared duration. A run longer than its estimated
duration is an error, because the testbed stops it, and the message suggests a duration with
headroom. `duration = "measured"` uses that suggestion; the default, `"auto"`, declares the
form's maximum, 600 s.

With a `backend.jl`, the testbed first installs Julia and the packages and loads the file,
and only then does the main file create its Robotarium. That startup counts against the
duration too, so the comparison is startup + drive + steps x 0.033 s:

- a fixed duration shorter than that is an **error**, which suggests one that fits;
- under `"auto"`, more than 600 s is an **error** and more than 540 s a **warning**;
- `"measured"` declares a duration that includes the startup.

The startup figure comes from `[julia] startup`, from a timing fetched from the testbed, from
`robotarium julia check --cold`, or else an assumed 180 s; [julia.md](julia.md#the-startup-budget)
explains each. The report also notes how long Julia took here, already installed, which is
far less than on the testbed.

## Loop time

The shim times the main file's own work: the wall clock from one `step()` returning to the
next being called, after the drive to the initial poses. The report gives its median, 99th
percentile and maximum. On the testbed a step is 33 ms, so a 99th percentile above that is a
**warning**: the code may not keep up with the robots. Frame capture for `--video` is not
counted, and neither is the simulator's own work inside `step()`. This machine is not the
testbed's, so treat the numbers as a guide. Building operators once at startup (in Julia, say)
and keeping the loop to numpy keeps them small.

## Variants: `--set`

`--set KEY=VALUE` changes a `[config]` value for one command, so one manifest serves several
variants of an experiment (a sheaf controller and its baseline, say):

```bash
robotarium simulate --set controller=baseline --set gains.alpha=1.5
robotarium submit --draft --set controller=baseline -y
```

The value is read as TOML (`0.5`, `true`, `[1, 2]`, `{ a = 1 }`, `"12"`), and as a plain string
when it isn't one, so `controller=baseline` needs no quotes. A dotted key reaches into a nested
table. The bundle is then the variant's: `CONFIG` in `robotarium_bundle.py` holds the new
value, so the submitted bundle carries it. A title can name it with `{config.controller}`, so
the variant shows in the form's title and in the names of the recordings. `check`, `bundle`,
`simulate`, `submit` and `julia check` take `--set`. Each is listed as an INFO finding, and a key
the config doesn't have is a **warning**, to catch a misspelling. `submit` records the settings
in the ledger (`"set"`).

## Videos and still frames

```bash
robotarium simulate --video                          # .robotarium/runs/<title>_<time>.mp4
robotarium simulate --video out/run.mp4 --dpi 150 --every 2
robotarium simulate --frames 0,14,27,end             # stills only: no ffmpeg needed
robotarium simulate --video --frames 0,30,end --frames-format pdf --frames-dir figures/
```

`--video` records the simulator's figure to an H.264 `.mp4` (yuv420p, `+faststart`, through
ffmpeg, with the same `VideoRecorder` as `robotarium_runtime`). Without ffmpeg on `PATH`,
`--video` is an error before anything runs. `--frames` saves stills at experiment times in
seconds, plus `end` for the last pose; `--frames-format` is `png` (the default) or `pdf`.

| Option | Default | Meaning |
|---|---|---|
| `--video [PATH.mp4]` | `.robotarium/runs/<title slug>_<YYYYmmdd_HHMMSS>.mp4` | record a video |
| `--fps N` | 30 / `--every` | frames per second; the default plays the run in real time |
| `--every N` | 1 | one frame every N steps |
| `--dpi N` | 200 | the figure's resolution: 8.25 x 5.25 in, so 1650 x 1050 px at 200 |
| `--frames T1,T2,...,end` | none | stills at these times |
| `--frames-dir DIR` | beside the video, `<video>_frames/` | where the stills go |
| `--frames-format png\|pdf` | `png` | the stills' format |

**Time 0 is the experiment's first step**, after the drive to the initial poses (which isn't
recorded). A still at T s is taken at step ceil(T / 0.033): 14.5 s is step 440. It is captured
as `step()` is called, before the step is taken. At that moment the robots (drawn at the end
of the step before) and whatever the main file drew since, such as a `FloorOverlay`, both show
the poses at that step's time. `end` is taken at `r.debug()` (or at exit) and shows the poses
after the last step. A time past the end of the run is a **warning**.

Stills are named `t0s.png`, `t14s.png`, `t14p5s.png` and `end.png`, with no second dot, which
LaTeX's `\includegraphics` would take for the extension. The command prints each path after
`Run logs:`.

The recording needs the figure, so `show_figure=True` is forced (an INFO says so if the main
file passed `False`; the testbed still runs it as written), and matplotlib's `figure.dpi` is
set before rps creates its figure, since rps takes no dpi. Headless, rps's own per-step redraw
is turned off: under Agg, `draw_idle()` renders the whole figure every step, for nobody. Only
the frames kept are drawn, so a recorded run costs one render per frame. Recording settings
can also live in `[simulator]` (`video`, `fps`, `every`, `dpi`, `frames`, `frames_dir`,
`frames_format`); the command line wins. They apply to `robotarium simulate` only, not to the
simulation `submit` runs.

### Drawing on the figure: `FloorOverlay`

`robotarium_runtime.FloorOverlay` draws links, rails, rings, arrows, labels and a run clock on
the arena figure, in metres, updated in place each step:

```python
from robotarium_runtime import FloorOverlay

overlay = FloorOverlay(r)
for k in range(iterations):
    x = r.get_poses()
    overlay.segment("link12", x[:, 0], x[:, 1], color="#c03030")
    overlay.ring("slot1", goals[:, 0], 0.08, style="--")
    overlay.arrow("heading1", x[:, 0], 0.15 * np.array([np.cos(x[2, 0]), np.sin(x[2, 0])]))
    overlay.clock(k * 0.033, label="assembling")
    ...
    r.step()
```

Each method takes a key: the first call creates the artist, later calls move or restyle it.
`segment`, `polyline` (a 2 x N array, `closed=`), `ring` (unfilled), `dot`, `arrow` (a zero
vector hides it), `text` and `clock` draw; `hide`, `show`, `remove`, `clear(prefix)` and `keys`
act on keys. It never redraws the figure itself; rps's `step()` does. Without a figure
(`show_figure=False`, or a testbed that draws none), `overlay.enabled` is False, every call
does nothing, and matplotlib is never imported. An error while drawing turns the overlay off
with one printed line, rather than stop the robots. The clock lets a still from the testbed's
own video be matched to the run's time.

## Reproducible runs: `--seed`

rps picks the robots' starting points at random (`random.sample` over the chargers) and drives
them to the initial poses through random waypoints (`numpy.random`), so two runs start from
slightly different poses, by up to about 5 cm and 0.2 rad. `--seed N` (or `[simulator] seed`)
seeds both before the main file runs, so runs with the same seed and code start from the same
poses. Two variants (`--set controller=...`) then start alike, as long as they draw the same
random numbers before creating the Robotarium. The testbed's real robots start where they are.

`robotarium simulate` exits non-zero on any error, so it can gate CI. `robotarium submit
--simulate`, or `[submit] simulate = true`, runs it before every submission and refuses to send
anything it rejects.

## Run logs

`RunLogger` writes into `.robotarium/runs/` during a simulation, rather than into the bundle.
The copy of each write that it prints for the testbed's `log.txt` shows as one line of output,
and the Julia timing marker isn't shown (the line before it says the same). Each finished run
also leaves `.robotarium/runs/last-simulation.json`, its step count and the hashes of the files
it ran, so `robotarium check` and `robotarium julia check --cold` can show the duration budget
without simulating again. Read a log back with:

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
