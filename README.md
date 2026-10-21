# robotarium

Automation for [Georgia Tech Robotarium](https://www.robotarium.gatech.edu/) submissions.
Describe an experiment once (title, estimated duration, description, number of robots, the files
and which one is main), then check it, rehearse it in the simulator, and submit it from the
command line or from Python, without the web form.

```bash
robotarium init my_experiment          # a working experiment to start from
cd my_experiment
robotarium simulate --show             # watch it in the Robotarium simulator
robotarium submit                      # check, show exactly what will be sent, confirm, send
robotarium history                     # what was sent, when, and how it went
```

This package generalises the submission tooling built for
[robosheaf](https://github.com/hans-riess/robosheaf): its login and upload client, its
`backend.jl` method for running Julia on the testbed, and its habit of failing on your machine
rather than at the end of the testbed's queue.

- [Install](#install)
- [Credentials](#credentials)
- [Describing an experiment](#describing-an-experiment)
- [Commands](#commands)
- [What gets checked](#what-gets-checked)
- [On the testbed: `robotarium_runtime`](#on-the-testbed-robotarium_runtime)
- [Julia: the backend.jl method](#julia-the-backendjl-method)
- [Logging](#logging)
- [From Python](#from-python)
- [Development](#development)

## Install

Python 3.11 or newer. Submitting needs only the standard library, so the extras are optional:

```bash
git clone https://github.com/hans-riess/robotarium
pip install -e "robotarium[sim]"            # + the Robotarium simulator, for `simulate`
pip install -e "robotarium[sim,julia]"      # + juliacall, for a backend.jl
pip install -e "robotarium[sim,keyring]"    # + keyring, for `robotarium login`
```

`[sim]` installs `rps` from the
[robotarium_python_simulator](https://github.com/robotarium/robotarium_python_simulator)
repository. To use a clone of it instead, see [docs/simulator.md](docs/simulator.md).

## Credentials

`submit` logs in with the email address and password you use on the Robotarium website. **The
password is never taken on the command line, never printed, and never written to the submission
ledger.** Keep it in one of these, listed in order of preference:

1. **The system keyring**, on your own machine:

   ```bash
   pip install keyring
   robotarium login --verify     # asks for both, checks them, keeps the password in the keyring
   ```

2. **A `.env` file beside `robotarium.toml`**, kept out of git. `robotarium init` writes a
   `.gitignore` that already ignores it, and a `.env.example` to copy:

   ```bash
   cp .env.example .env && chmod 600 .env    # then fill in the two lines
   ```

   ```dotenv
   ROBOTARIUM_USERNAME=you@example.edu
   ROBOTARIUM_PASSWORD=...
   ```

   `robotarium` warns if the file is readable by other users, or if git would commit it.

3. **Environment variables** `ROBOTARIUM_USERNAME` and `ROBOTARIUM_PASSWORD`, for CI (as
   repository secrets) or fed from a password manager's CLI.

4. **A prompt**, when nothing else has them and a terminal is attached.

```bash
robotarium credentials --verify   # which source each value comes from, and a test login
robotarium logout                 # forget what `login` stored
```

[docs/credentials.md](docs/credentials.md) covers each option in detail: precedence, CI,
password managers, and what to do if a password leaks.

## Describing an experiment

An experiment is described in a `robotarium.toml`:

```toml
[experiment]
title = "Circle formation ({timestamp})"     # also {date} {time} {git} {robots} {main}
description = """
Four GRITSBots drive to evenly spaced points on a circle.
"""
duration = "auto"        # seconds, "90s", "2m", or "auto": measured in the simulator
robots = 4               # 1-18
main = "main.py"         # the Main File
files = ["helpers.py", "backend.jl", "data/*.npy"]   # supporting files; globs work

[config]                 # parameters, carried to the testbed as Python (the form takes no .toml)
radius = 0.5

[julia]                  # packages backend.jl loads
packages = [{ name = "JSON3", uuid = "0f8b85d8-7281-11e9-16c2-39a750bddbf1", version = "1" }]

[submit]
draft = true             # save on the site rather than queue; flip when you trust it
simulate = true          # refuse to submit anything the simulator rejects
```

Every key is in [docs/manifest.md](docs/manifest.md). Without a manifest, `submit` takes it all
as flags:

```bash
robotarium submit --title "Swap" --description "Two robots swap places." \
    --duration 90s --robots 2 --main swap.py --file helpers.py
```

**The form uploads every file flat**, under its base name, into one directory beside the main
file. So the bundle is built that way too: `robotarium bundle` lays the files out in
`.robotarium/bundle/`, and `simulate` runs from there. A file from `data/` is opened on the
testbed as `Path(__file__).parent / "name.npy"`.

## Commands

| Command | Does |
|---|---|
| `robotarium init DIR [--julia] [--robots N]` | Write a new experiment (manifest, main file, `.gitignore`, `.env.example`, and with `--julia` a `backend.jl`) that simulates and submits as it stands. |
| `robotarium check` | Every offline check. Nothing is sent. Exits non-zero on an error (on a warning too with `--strict`). |
| `robotarium bundle` | Build `.robotarium/bundle/` and show the form as it would be filled. |
| `robotarium simulate [--show] [--simulator PATH]` | Run the bundle in the Robotarium simulator and judge the run as the testbed would. See [docs/simulator.md](docs/simulator.md). |
| `robotarium submit [--dry-run] [--draft\|--queue] [--simulate] [-y]` | Bundle, check, optionally simulate, **print exactly what will be sent and wait for a yes**, then upload every file, create the experiment, mark the main file, and record the attempt. |
| `robotarium history [-n N] [--json]` | The submission ledger. |
| `robotarium login`, `logout`, `credentials` | Where the login is kept. |
| `robotarium limits` | The site's live limits (robots, duration, file types). |
| `robotarium julia lookup NAME...` | A `[julia] packages` entry from Julia's General registry. |
| `robotarium julia check` | Load the `backend.jl` locally, through the same code the testbed runs. |
| `robotarium inspect RUN.npz` | What a `RunLogger` file holds. |

Every experiment command takes the manifest (or a directory at or under it) as an optional
positional argument; the default is the current directory. `robotarium -v ...` logs each step,
and `--log-file FILE` keeps that log.

## What gets checked

Before anything is sent, and as far as possible before anything is run:

- **The form:** title and description present; duration 1-600 s; 1-18 robots; the main file a
  `.py` or `.m`.
- **The files:** they exist; no directories; only extensions the form takes (no `.json`, `.yaml`,
  `.txt`, `.zip`); no two with the same base name.
- **Inside the Python:**
  - imports the testbed lacks, checked against the simulator's `libraries.txt`;
  - local modules imported but not uploaded, and package directories, which can't travel;
  - relative imports, which fail in a flat bundle;
  - `Robotarium(number_of_robots=...)` disagreeing with the form;
  - no `r.debug()` / `r.call_at_scripts_end()` at the end;
  - numpy spellings newer than the testbed's.
- **Inside the Julia:** packages loaded but not declared; `include`d files not uploaded.
- **In the simulator** (`simulate`, `submit --simulate`, or `duration = "auto"`): collisions and
  leaving the arena (errors: the testbed rejects them), saturated wheel commands, the robot
  count actually used, whether the run fits its declared duration.
- **Against the live site**, after logging in and before uploading: its current limits.

Errors refuse the submission. Warnings are shown in the plan you confirm.

## On the testbed: `robotarium_runtime`

The testbed doesn't have this package installed. What runs there is your main file and its
supporting files, plus two files `robotarium bundle` adds when your code uses them:

- **`robotarium_runtime.py`**, vendored from this package (standard library and numpy only,
  Python 3.10 compatible): `config()`, `RunLogger`, `load_julia_backend()`, `VideoRecorder`,
  `bundle_path()`.
- **`robotarium_bundle.py`**, generated: the title, robots, duration, commit and file hashes of
  the build, the `[config]` as `CONFIG`, and the `[julia]` packages, all as Python literals.

The same `import robotarium_runtime` works locally, since it's installed as a top-level module.
Outside a bundle, `config()` and `load_julia_backend()` read `robotarium.toml` instead, so
`python main.py` and the submitted bundle see the same values.

```python
from robotarium_runtime import RunLogger, config

CONFIG = config(default={"radius": 0.5})
log = RunLogger("circle", autosave_every=300)
...
log.record(poses=x, velocities=dxu)
```

## Julia: the backend.jl method

Put Julia code in a `.jl` file, list it in `files`, declare its packages under `[julia]`, and
load it from the main file:

```python
from robotarium_runtime import as_arrays, call_json, load_julia_backend

backend = load_julia_backend("backend.jl")
result = as_arrays(call_json(backend.formation, {"robots": 4}), ["targets", "laplacian"])
```

`load_julia_backend` declares the packages to juliapkg *before* Julia starts. This is the
lesson robosheaf learnt on the testbed, where adding them afterwards loaded mismatched
dependency versions. It then includes the file from beside the main file and returns its module.
`robotarium julia lookup NAME` prints a package's entry. `robotarium julia check` loads the
backend locally exactly as the testbed will. `robotarium init --julia` is a working example.
[docs/julia.md](docs/julia.md) explains each step and why.

## Logging

- **In the experiment:** `RunLogger` records any per-step quantities to a self-describing
  `.npz` (bundle title, commit, file hashes, notes), autosaving so that a run stopped at its
  declared duration still leaves data. `robotarium inspect` reads it.
- **Submissions:** `.robotarium/submissions.jsonl` records every attempt, successful or not,
  with the experiment id, form values and file hashes. A zip snapshot of exactly what was
  uploaded goes to `.robotarium/snapshots/`. It holds no credentials and is safe to commit.
- **The tool:** `-v` and `--log-file` show each step and each API call (never a body or a
  secret).

See [docs/logging.md](docs/logging.md).

## From Python

```python
from robotarium import Experiment, load_project, prepare, submit

# from a manifest
result = submit(load_project("my_experiment"), draft=True)
print(result.experiment_id, result.snapshot)

# or entirely in Python
experiment = Experiment(
    title="Consensus ({date})",
    description="Eight robots agree on a heading.",
    duration="2m",
    robots=8,
    main="consensus.py",
    files=["graphs.py"],
    config={"gain": 0.8},
)
prepared = prepare(experiment, simulate_first=True)   # bundle + every check + simulation
for finding in prepared.findings:
    print(finding)
result = submit(experiment, confirm=True)             # asks on the terminal before sending
```

`submit(..., confirm=False)` sends without asking. `confirm` may also be a function that's
given the plan text and returns whether to send. Lower-level pieces are importable too:
`robotarium.client.RobotariumClient` (login, upload, create, mark main),
`robotarium.credentials.resolve`, `robotarium.simulate.run`, and `robotarium.preflight.check`.

## How it talks to the site

There is no documented Robotarium API. `robotarium.client` makes the same five calls the
website's own JavaScript makes, including encrypting the password the way its login page does.
[docs/site-api.md](docs/site-api.md) lists them and explains how to re-read them if the site
changes.

## Development

```bash
pip install -e ".[sim,test]"
pytest
```

The suite never contacts the real site. Submission is tested end to end against a local
stand-in that records every request, including checks that the password never appears in clear
text and that nothing is sent without a yes. The simulator tests run the real `rps` and are
skipped where it isn't installed.
