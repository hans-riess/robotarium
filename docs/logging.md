# Logging

Three kinds of record are kept, for three different questions. A fourth, what the testbed
sends back, is downloaded by `robotarium fetch`.

| Question | Record | Where |
|---|---|---|
| What did the robots do? | `RunLogger`, in your main file | on the testbed, printed into `log.txt` for `robotarium fetch` to write back out; `.robotarium/runs/` in simulation, with any video and stills |
| What did I send, and when? | the submission ledger and snapshots | `.robotarium/submissions.jsonl`, `.robotarium/snapshots/` |
| What did `robotarium` itself do? | `-v` / `--log-file` | stderr / a file you name |
| What came back from the testbed? | `robotarium fetch`: video, log files, data files, return messages | `.robotarium/results/<experiment id>/` |

## In the experiment: `RunLogger`

`RunLogger` is part of `robotarium_runtime`, which travels with the experiment. It records any
quantities you give it at each step and writes them as one compressed `.npz`.

```python
from robotarium_runtime import RunLogger

log = RunLogger("formation", autosave_every=300, meta={"gain": GAIN})

for step in range(ITERATIONS):
    x = r.get_poses()
    ...
    log.record(poses=x, velocities=dxu, error=np.linalg.norm(x[:2] - goals))
    if step == switch_step:
        log.note("switched to the second formation")
    r.step()

log.save()
r.debug()
```

- **Each keyword is one series.** Arrays are stacked over time, so `poses` recorded as a 3 x N
  array becomes a T x 3 x N array. A series must keep the same shape at every step, and a change
  is an error at the step it happens. Different series may be recorded at different steps; each
  keeps the step numbers it was recorded at.
- **`note()`** prints a remark stamped with the step and the testbed time (step x 0.033 s), and
  keeps it in the record, with `"wall"`: the seconds since `robotarium_runtime` was imported.
  The wall time places a note in the testbed's video and `log.txt`, where Julia's startup and
  the drive to the initial poses come before step 0.
- **`autosave_every=N`** rewrites the file every N records. The testbed stops a run at its
  declared duration, and the autosave means a stopped run still leaves its data. The file is
  also written when the interpreter exits. Each write goes to a temporary file first and is then
  moved into place, so an interrupted write never leaves a truncated file.
- **The record describes itself.** Along with your arrays, the file holds the bundle's title,
  robot count, commit and file hashes (from `robotarium_bundle.py`), the start time, the Python
  and numpy versions, your `meta`, and the notes. A file that comes back from the testbed days
  later can be matched to the exact files that produced it.
- **Each write is also printed**, for the testbed's `log.txt`. See below.
- **`dtype="float32"`** stores every floating-point value as float32, half the size; integers,
  booleans and text are kept as recorded.
- **With a Julia backend**, the record's meta holds `"julia"`, the startup timings
  (`robotarium_runtime.julia_timings()`; see [julia.md](julia.md#timing-on-the-testbed)).

On the testbed the file is written beside the main file. In `robotarium simulate`,
`ROBOTARIUM_LOG_DIR` points it at `.robotarium/runs/` instead. `RunLogger(directory=...)`
overrides both.

### On the testbed: the copy in `log.txt`

The testbed returns what a run prints, as `log.txt`, but so far none of the files it writes: no
`.npz` or `.npy` has come back, whether it was written beside the main file or in the working
directory ([#7](https://github.com/hans-riess/robotarium/issues/7)). So each time `RunLogger`
writes its file, it also prints the file, base64-encoded between two marker lines:

```
-----BEGIN ROBOTARIUM RUN formation_20261021_153102.npz 182344 <its SHA-256>-----
UEsDBC0AAAAIAAAAIQDoNKiT//////////8JABQAcG9zZXMubnB5AQAQAOAAAAAAAAAATAAAAAAA
...
-----END ROBOTARIUM RUN formation_20261021_153102.npz-----
```

`robotarium fetch` finds these copies in `logs/log.txt` and writes each one into `data/` as the
`.npz` it was. A copy is used only if it matches its size and SHA-256, so one that was cut short
is never mistaken for the file.

- **Autosaves print too.** Every write prints the whole file again, so with `autosave_every`
  the log holds several copies, each longer than the last. `fetch` keeps the last complete one.
  If the run was stopped while printing a copy, that's the one before, and `fetch` says so.
- **Mind the size.** A copy is a third larger than the file, and each write prints the whole
  file again. Seven robots over 4,545 steps (poses, commands, 16 residuals, an energy) make a
  1.8 MB file; autosaving every 300 records prints about 19 MB of base64 into `log.txt`.
  `RunLogger(copy_every=N)` prints only every Nth write, and `dtype="float32"` halves each one:

  ```python
  log = RunLogger("formation", autosave_every=300, copy_every=5, dtype="float32")
  ```

  The write at exit always prints, and so does `log.save(final=True)`, so the last copy in
  `log.txt` is the whole run. A write whose copy was held back is written and printed again at
  exit, so a run that ends just after an autosave still leaves its copy.
- **When.** A copy is printed whenever stdout isn't a terminal: on the testbed, and in
  `robotarium simulate`, which shows each copy as one line. `RunLogger(stdout_copy=False)` turns
  it off; `stdout_copy=True` prints it in a terminal too.
- **If the Robotarium starts returning data files**, `fetch` downloads them as well, and keeps
  the site's file over a copy of the same name.

Reading a log back:

```bash
robotarium inspect circle_20261021_153102.npz
```

```python
from robotarium_runtime import load_run

run = load_run("circle_20261021_153102.npz")
run.data["poses"]       # T x 3 x N
run.steps["poses"]      # the step each row was recorded at
run.meta["bundle"]      # title, robots, git, files, built_at
run.meta["notes"]
```

Or without this package, since it's a plain `.npz`: `np.load(path)`, with `json.loads(str(f["__meta__"]))`
for the metadata.

### Julia's startup, in `log.txt`

A run that loads a `backend.jl` prints its Julia timings: one readable line and one marker line,
once when the backend has loaded and again, with the first calls, at the first `RunLogger`
save:

```
robotarium_runtime: Julia 1.11.9: packages 152.1 s, start 7.9 s, include 12.4 s, first build_operators() 13.7 s; 186.1 s in all
-----ROBOTARIUM TIMING {"first_calls":{"build_operators":13.7},"include_seconds":12.4,...}-----
```

`robotarium fetch` keeps the last marker as `.robotarium/results/<id>/julia_timing.json` and
prints it. The duration checks then use the testbed's own figure (see
[julia.md](julia.md#the-startup-budget)).

### Video, locally

`robotarium simulate --video` records a run without touching the main file, and `--frames`
saves stills at chosen times (see [simulator.md](simulator.md#videos-and-still-frames)). To
record from the main file instead, `VideoRecorder` records the simulator's figure to an MP4
with ffmpeg. It's a no-op where ffmpeg or a figure is missing, which includes the testbed,
where the Robotarium records its own video.

```python
from robotarium_runtime import VideoRecorder

video = VideoRecorder(r._fig, "run.mp4")
...  # after each r.step():
video.frame()
...
video.close()
```

## Submissions: the ledger and snapshots

Every `robotarium submit` that reaches the network appends one JSON line to
`.robotarium/submissions.jsonl`, whether it succeeded or not:

```json
{"submitted_at": "2026-10-21T15:31:02-04:00", "outcome": "submitted", "experiment_id": "...",
 "draft": false, "title": "Circle (20261021T1530)", "duration": 60, "robots": 4,
 "main": "main.py", "files": {"main.py": "<sha256>", ...}, "git": "abc1234",
 "snapshot": "snapshots/circle_20261021t1530-20261021T153058.zip",
 "set": {"controller": "baseline"},
 "simulation": {"steps": 1200, "init_steps": 310, "errors": {}, "robots": [4]}, ...}
```

`"set"` appears when the submission was a `--set` variant.

A failed attempt has `"outcome": "failed"`, or `"interrupted"` for Ctrl-C. It also records the
`stage` it failed at (`login`, `settings`, `upload`, `create` or `mark-main`), the reason, and a
`consequence` saying what the failure left on the site:
- **upload:** files uploaded and attached to no experiment.
- **create:** the connection dropped after the request went out, so the experiment may exist
  anyway. Check the dashboard before submitting again.
- **mark-main:** the experiment was created but its main file isn't marked. Tick it on the site
  instead of resubmitting.

The same sentence ends the error `submit` prints.
The zip in `snapshots/` holds exactly the files that were uploaded.

```bash
robotarium history          # readable
robotarium history -n 5
robotarium history --json   # for scripts
```

The ledger never contains your email address or password, so commit it if you want the history
shared. `robotarium init`'s `.gitignore` keeps it and ignores the rest of `.robotarium/`.

## The tool itself: `-v` and `--log-file`

```bash
robotarium -v submit
robotarium --log-file robotarium.log submit
```

`-v` prints what each step does to stderr: every API request's method, path and HTTP status,
the simulator command, and the staging of files. `--log-file` appends the same, timestamped, to
a file. Neither ever includes a request body, the password, or the session token.

The library logs through the standard `logging` module under the `robotarium` logger, so from
Python:

```python
import logging
logging.basicConfig()
logging.getLogger("robotarium").setLevel(logging.DEBUG)
```
