# Logging

Three kinds of record are kept, for three different questions.

| Question | Record | Where |
|---|---|---|
| What did the robots do? | `RunLogger`, in your main file | beside the main file on the testbed; `.robotarium/runs/` in simulation |
| What did I send, and when? | the submission ledger and snapshots | `.robotarium/submissions.jsonl`, `.robotarium/snapshots/` |
| What did `robotarium` itself do? | `-v` / `--log-file` | stderr / a file you name |

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
  keeps it in the record.
- **`autosave_every=N`** rewrites the file every N records. The testbed stops a run at its
  declared duration, and the autosave means a stopped run still leaves its data. The file is
  also written when the interpreter exits. Each write goes to a temporary file first and is then
  moved into place, so an interrupted write never leaves a truncated file.
- **The record describes itself.** Along with your arrays, the file holds the bundle's title,
  robot count, commit and file hashes (from `robotarium_bundle.py`), the start time, the Python
  and numpy versions, your `meta`, and the notes. A file that comes back from the testbed days
  later can be matched to the exact files that produced it.

On the testbed the file is written beside the main file. That's where the simulator's
`data_saving` example saves its `.npy` files, and where robosheaf's run logs land, for the
Robotarium to return with the experiment's results. In `robotarium simulate`,
`ROBOTARIUM_LOG_DIR` points it at `.robotarium/runs/` instead. `RunLogger(directory=...)`
overrides both.

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

### Video, locally

`VideoRecorder` records the simulator's figure to an MP4 with ffmpeg. It's a no-op where ffmpeg
or a figure is missing, which includes the testbed, where the Robotarium records its own video.

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
 "simulation": {"steps": 1200, "init_steps": 310, "errors": {}, "robots": [4]}, ...}
```

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
