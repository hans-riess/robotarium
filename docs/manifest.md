# `robotarium.toml` reference

A `robotarium.toml` describes one experiment: what the submission form asks for, which files to
upload, and how to rehearse and send them. Every command looks for it in the directory you name,
or the current one, and then in each parent directory. `robotarium init` writes a working one.

Unknown keys are errors. A misspelt key fails loudly instead of being ignored.

```toml
[experiment]
title = "Circle formation ({timestamp})"
description = """
Four GRITSBots drive to evenly spaced points on a circle.
"""
duration = "auto"
robots = 4
main = "main.py"
files = ["helpers.py", "backend.jl", "data/*.npy"]
# config = "params.toml"
# runtime = "auto"
# allow_imports = ["mymodule"]

[config]
radius = 0.5
iterations = 1500

[julia]
version = "=1.11.9"
packages = [
    { name = "JSON3", uuid = "0f8b85d8-7281-11e9-16c2-39a750bddbf1", version = "1" },
]
# startup = "measured"

[simulator]
path = "../robotarium_python_simulator"
fast = true
headless = true
timeout = 900
# seed = 1
# video = true
# frames = [0, 14, "end"]

[submit]
draft = true
simulate = true

[output]
dir = ".robotarium"
```

## `[experiment]`: the form and the files

| Key | Required | Meaning |
|---|---|---|
| `title` | yes | The form's **Title**. Placeholders are filled when the bundle is built: `{date}` (2026-10-21), `{time}` (15:31), `{timestamp}` (20261021T1531), `{git}` (short commit, `-dirty` if changed), `{robots}`, `{main}` (the main file's stem), and `{config.KEY}`, a `[config]` value (`{config.controller}`; a dotted KEY reaches into a nested table), which shows a `--set` variant in the title. Write `{{` and `}}` for literal braces. |
| `description` | one of | The form's **Experiment Description**. |
| `description_file` | one of | ...or a text file to read it from, relative to the manifest. |
| `duration` | no | The form's **Estimated Duration**. `"auto"` (the default) declares the form's maximum, 600 s. Or give seconds (`120`), `"90s"`, `"2m"`, `"1m30s"` or `"1.5 min"`, rounded up to whole seconds, from 1 to 600. Or `"measured"` takes it from a simulator run (see below). |
| `robots` | yes | The form's **Number of Robots**, 1-18. Checked against `Robotarium(number_of_robots=...)` in the main file, statically and in simulation. |
| `main` | yes | The **Main File**: a `.py`, or a MATLAB `.m`. |
| `files` | no | Supporting files, relative to the manifest. Globs work (`"data/*.npy"`, `"**/*.py"`). The main file may be listed or not. A path may leave the project directory (`"../common/*.py"`); see below. |
| `config` | no | Parameters for the run: a `.toml`, `.json` or `.yaml` file. Or use a `[config]` table instead, but not both. |
| `runtime` | no | Whether to vendor `robotarium_runtime.py` into the bundle: `true`, `false`, or `"auto"` (default: whenever an uploaded `.py` imports it). |
| `allow_imports` | no | Top-level modules to accept although the Robotarium's `libraries.txt` doesn't list them. |

**Files are uploaded flat.** The form uploads each file under its base name into one directory
beside the main file. `data/waypoints.npy` arrives as `waypoints.npy`, so open it on the testbed
as `Path(__file__).parent / "waypoints.npy"` (or `robotarium_runtime.bundle_path("waypoints.npy")`),
not by its path in your project. Two files with the same base name are an error, and so is a
directory.

**Files from outside the project directory.** Code several experiments share can live beside
them and be listed by a relative path:

```toml
files = ["backend.jl", "../common/demokit.py", "../common/sheafcore.jl"]
```

Each is uploaded flat, like every file, so the main file imports `demokit` and `backend.jl`
includes `"sheafcore.jl"` by its base name. `robotarium check` notes each one, and still
refuses two files that would land on one name (`../a/x.py` and `../b/x.py`). It warns about a
file outside the git work tree the manifest is in, since the bundle's `{git}` commit and its
`-dirty` flag say nothing about that file; its sha256 in `robotarium_bundle.py` still
identifies it. `robotarium simulate` runs from the flat bundle, where the shared module is
importable; `python main.py` run in the project directory doesn't see `../common` unless it is
on `PYTHONPATH`.

**The form only takes these extensions:** `.py .m .jl .mat .npy .mex .mexw64 .mexa64
.mexmaci64 .jpg .jpeg .png .gif .tiff .bmp`. That leaves out `.json`, `.yaml`, `.toml`, `.txt`
and `.zip`, which is why configuration travels as Python (next section).

**`duration`.** The testbed stops a run that outlasts its declared duration, and declaring more
than the run needs costs nothing, since the run ends when the main file does. So by default,
`"auto"`, an experiment declares the form's maximum of 600 s, or the site's own maximum if
`submit` finds that lower. When you'd rather declare close to what the run takes, for example to
fit a queue that schedules by declared time, `"measured"` takes it from a simulator run. The
testbed advances at a fixed 0.033 s per `Robotarium.step()`, whatever your own time step says, so
`"measured"` counts the steps (the drive to the initial poses included), adds a quarter for
headroom, and rounds up to the minute. With a `backend.jl` it counts Julia's startup too, which
the testbed spends before the first step (`[julia] startup`). With a fixed duration, a
`--simulate` run checks that startup and steps fit.

## `[config]`: parameters that travel with the experiment

The form takes no `.toml` or `.json`, so the bundle carries the configuration as a Python literal
in the generated `robotarium_bundle.py`. The main file reads it with:

```python
from robotarium_runtime import config
CONFIG = config(default={"radius": 0.5})   # the default applies only when no config is declared
```

When you run `python main.py` directly in the project rather than from a bundle, `config()`
reads the same values from `robotarium.toml`, so both paths see the same numbers. Values must be
plain data: strings, numbers, booleans, lists and tables. A TOML date is refused because it
isn't a Python literal, so quote it.

**Variants.** `--set KEY=VALUE` on `check`, `bundle`, `simulate`, `submit` and `julia check`
changes a value for that command, and the bundle (its `CONFIG`, its title) is then the
variant's:

```bash
robotarium submit --draft --set controller=baseline --set gains.alpha=1.5 -y
```

The value is read as TOML, and as a string when it isn't valid TOML (`controller=baseline`).
A value the config already has keeps its type where the text allows it: a string stays the text
as typed (`label=12` is `"12"`, `label=nan` is `"nan"`), a float stays a float (`alpha=2` is
`2.0`), and a bool also takes Python's `True` and `False`. A value that still changes type
(`show=maybe` for a bool) is a warning. A dotted key sets a value in a nested table. A key the
config lacks is added, with a warning in case it is a misspelling. `python main.py` run
directly reads the manifest as written, without the settings.

## `[julia]`: packages for a `backend.jl`

| Key | Meaning |
|---|---|
| `packages` | `{ name, uuid, version }` for each Julia package your `.jl` files load. `version` is a Pkg compat specifier (`"1"`, `"0.0.1"`, `"=1.14.3"`) and is optional. `robotarium julia lookup NAME` prints the entry. A package listed twice is an error. |
| `version` | A Julia compat specifier for juliapkg. Optional. Write an exact pin, `"=1.11.9"`: a bare `"1.11.9"` is a caret specifier, at least 1.11.9 and below 2, so the testbed may install a newer Julia, and `check` warns about it. `"=1.11"` pins 1.11.0 (Pkg fills in the missing parts with 0), and `check` says so too. |
| `startup` | How long the testbed takes to install and start Julia before the first step, counted against the duration: `"measured"` (the default: from a testbed timing, a `julia check --cold`, or an assumed 180 s) or seconds (`240`, `"4m"`). A fetched testbed timing that is larger replaces a stated figure, with a warning. See [julia.md](julia.md#the-startup-budget). |

These are declared to juliapkg *before* Julia starts, by `robotarium_runtime.load_julia_backend`,
on the testbed and locally. The standard library (`LinearAlgebra`, `Random`, ...) needs no
entry. See [julia.md](julia.md).

## `[simulator]`: rehearsing

| Key | Default | Meaning |
|---|---|---|
| `path` | - | A `robotarium_python_simulator` clone to run against, put on the run's `PYTHONPATH`. Without it, whatever `rps` your Python imports is used. `$ROBOTARIUM_SIMULATOR` and `--simulator` do the same. |
| `fast` | `true` | Skip the simulator's real-time pacing. |
| `headless` | `true` | Draw off screen (matplotlib's Agg backend). `robotarium simulate --show` opens a window in real time. |
| `timeout` | none | Stop a run after this many seconds of wall clock. |
| `seed` | none | Seed `random` and `numpy.random` before the main file runs, so the random initial poses repeat. `--seed` overrides it. |
| `video` | `false` | `true`, or an `.mp4` path relative to the manifest: record each `robotarium simulate` run. |
| `fps`, `every`, `dpi` | 30 / `every`, 1, 200 | The video's frame rate, one frame every `every` steps, and the figure's resolution. |
| `frames` | none | Stills at these experiment times: `[0, 14.5, "end"]` or `"0,14.5,end"`. |
| `frames_dir` | beside the video | Where the stills go, relative to the manifest. |
| `frames_format` | `"png"` | `"png"` or `"pdf"`. |

The recording keys apply to `robotarium simulate` only, and its options override them. See
[simulator.md](simulator.md#videos-and-still-frames).

## `[submit]`

| Key | Default | Meaning |
|---|---|---|
| `draft` | `false` | Save the experiment on the site (the form's **Save Experiment**) rather than queue it (**Submit Experiment**). `--draft` and `--queue` override it. |
| `simulate` | `false` | Run the simulator before submitting, and refuse on any error it finds. `--simulate` and `--no-simulate` override it. A duration of `"measured"` always simulates. |

## `[output]`

| Key | Default | Meaning |
|---|---|---|
| `dir` | `.robotarium` | Where the bundle, simulation run logs, snapshots and the submission ledger go. |

```
.robotarium/
    bundle/              the current bundle: exactly what would be uploaded
    runs/                RunLogger files, videos and stills from `robotarium simulate`, and
                         last-simulation.json
    julia/               cold-start.json, from `robotarium julia check --cold`
    results/             what `robotarium fetch` downloaded, with julia_timing.json
    snapshots/           a zip of every bundle submitted, named by title and time
    submissions.jsonl    the ledger: one line per submission attempt
```

`robotarium init`'s `.gitignore` ignores all of it except `submissions.jsonl`, which records
what was sent and when, and is safe to commit.

## Without a manifest

`submit` also works from flags alone, for a one-off:

```bash
robotarium submit --title "Quick test" --description "Two robots swap places." \
    --duration 90s --robots 2 --main swap.py --file helpers.py
```

With a manifest, the same flags override its values for one submission.
