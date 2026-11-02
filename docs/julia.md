# The backend.jl method

The method lets an experiment do part of its work in Julia: building operators, solving
something, anything Julia's ecosystem does better. The Julia code goes in a `.jl` file uploaded
next to the Python main file, and the main file loads it through juliacall at startup.
[robosheaf](https://github.com/hans-riess/robosheaf) runs this way on the Robotarium (its
`sheaf_backend.jl` builds cellular sheaves with CellularSheaves.jl). This package makes the
method reusable.

```bash
robotarium init my_experiment --julia      # a working example to start from
```

## How it works

```
robotarium.toml            [julia] packages = [{ name, uuid, version }, ...]
       |
       |  robotarium bundle: carried as JULIA in the generated robotarium_bundle.py,
       |  because the form takes no .json (so no juliapkg.json) and no .toml
       v
main.py                    backend = load_julia_backend("backend.jl")
       |
       |  robotarium_runtime, uploaded beside main.py:
       |    1. juliapkg.add() each package, then juliapkg.resolve(), BEFORE Julia starts
       |    2. import juliacall; Main.include("<main file's directory>/backend.jl")
       |    3. return the module backend.jl defines
       v
backend.formation(json)    Julia, called from Python; JSON in, NamedTuple out
```

The pieces, and why each is the way it is:

**The packages are declared before Julia starts.** Adding packages with `Pkg.add` from inside a
running juliacall session doesn't work reliably. juliacall has already loaded PythonCall's
dependencies by then, so a package the add needs to downgrade stays loaded at its old version,
and anything that needs the new one fails to precompile. That happened on the Robotarium:
Parsers 3 was loaded, and CSV, a CellularSheaves dependency, needed Parsers 2.
`load_julia_backend` therefore hands every package to juliapkg first, which resolves them
together with juliacall's own in one Pkg operation, in a separate Julia process. It also warns if
something imported juliacall before it ran.

**The declaration travels inside a `.py` file.** juliapkg normally reads a `juliapkg.json`, but
the upload form refuses `.json`. The manifest's `[julia]` table is copied into the generated
`robotarium_bundle.py`, and `load_julia_backend` reads it from there. When you run
`python main.py` directly in your project, it reads `robotarium.toml` instead, so both paths
declare the same packages.

**The `.jl` file is found next to the main file, not in the working directory.** The working
directory on the testbed need not be the bundle's directory, so a bare name like `"backend.jl"`
is resolved against the main file's directory.

**Data crosses the boundary as JSON on the way in and as a NamedTuple on the way out.**
`call_json(f, payload)` sends `json.dumps(payload)`, numpy arrays included, and the Julia side
parses it with JSON3. `as_arrays(result, names)` turns the NamedTuple's fields back into numpy
arrays. This is the simplest robust interface, and it's what robosheaf uses. Calling Julia
functions with native juliacall arguments works too, if you prefer.

## Writing one

`backend.jl`:

```julia
module Backend

using JSON3
using LinearAlgebra

export formation

function formation(spec_json::AbstractString)
    spec = JSON3.read(spec_json)
    n = Int(spec["robots"])
    # ... compute ...
    return (targets = targets, laplacian = laplacian)
end

end
```

`main.py`:

```python
from robotarium_runtime import as_arrays, call_json, load_julia_backend

backend = load_julia_backend("backend.jl")          # first, before anything imports juliacall
result = call_json(backend.formation, {"robots": N, "radius": 0.5})
arrays = as_arrays(result, ["targets", "laplacian"])
```

`robotarium.toml`:

```toml
[experiment]
files = ["backend.jl"]

[julia]
packages = [
    { name = "JSON3", uuid = "0f8b85d8-7281-11e9-16c2-39a750bddbf1", version = "1" },
]
```

To get a package's entry:

```bash
robotarium julia lookup JSON3 CellularSheaves
```

This reads the UUID and the latest version from Julia's General registry. The `version` is a Pkg
compat specifier: `"1"` accepts any 1.x and `"=1.14.3"` pins exactly. Pin exactly when the
experiment must reproduce.

The Julia standard library (`LinearAlgebra`, `Random`, `Statistics`, `SparseArrays`, ...) ships
with Julia and needs no entry.

### Pinning Julia

`[julia] version` is a Pkg compat specifier for Julia itself, and Pkg reads a bare version as a
caret specifier: `"1.11.9"` means at least 1.11.9 and below 2, so the testbed may install a
newer Julia (1.12, 1.13) than the one tested here. `"=1.11.9"` pins one release:

```toml
[julia]
version = "=1.11.9"
```

`robotarium check` warns about a version that isn't an exact pin. juliacall adds its own
bounds: the testbed's juliacall 0.9.35 asks for Julia `^1.10.3`, and with a Python whose
OpenSSL is older than 3.5 juliapkg also caps it below 1.12, so 1.11.9, the last 1.11 release,
is the pin that fits.

### Shared Julia code

A `.jl` from outside the project directory, such as a module several experiments share,
is listed like any file (`files = ["backend.jl", "../common/sheafcore.jl"]`) and uploaded
flat beside `backend.jl`. Include it by its base name, as every file lands in one directory:

```julia
include(joinpath(@__DIR__, "sheafcore.jl"))
```

## Checking it

`robotarium check` reads every `using` and `import` in the uploaded `.jl` files. It reports as an
error any package that's neither in the standard library nor declared, since nothing would
install it on the testbed. It also errors on an `include("x.jl")` whose file isn't uploaded, or
that's included by a path rather than a base name, because every file lands in one directory.
It warns when a `.jl` is uploaded but no Python file imports juliacall or `robotarium_runtime`
to load it.

It also warns about a `[julia] version` that isn't an exact pin (see "Pinning Julia" above).

`robotarium julia check` goes further. It builds the bundle and loads the backend from the bundle
directory through the same `robotarium_runtime` code the testbed runs: packages declared, Julia
started, file included. It then prints the module, what it exports, and how long each step
took. The first run downloads Julia and the packages into your environment, which takes a few
minutes. `--call FUNC` also calls a function twice after loading, timing its first call;
`--set KEY=VALUE` checks a variant's bundle; `--cold` times the whole install from nothing
(see "Timing a cold start" below).

```bash
pip install -e ".[julia]"     # in your clone, venv active: juliacall, which brings juliapkg
pip install 'juliacall==0.9.35' 'juliapkg==0.1.24'   # the testbed's versions
robotarium julia check
robotarium julia check --call build_operators
```

`robotarium simulate` then runs the whole experiment, Julia included, in the simulator.

## On the testbed

`juliacall` and `juliapkg` are on the Robotarium's library list (`libraries.txt` in the
simulator repository: juliacall 0.9.35, juliapkg 0.1.24), and robosheaf has run
CellularSheaves.jl there this way. Julia and the declared packages are installed when the
experiment starts, which needs the testbed to reach Julia's package servers. If
`load_julia_backend` fails there, the message distinguishes a package that couldn't be
installed from a `.jl` file that couldn't be loaded.

### The startup budget

The testbed appears to install Julia and every package at the start of each run: robosheaf's
run with CellularSheaves.jl began to move about 171 s into its video. That time counts against
the declared duration as much as the robots' steps do. `robotarium simulate` and `submit
--simulate` therefore compare startup + drive + steps x 0.033 s with the duration (see
[simulator.md](simulator.md#running)), and `"measured"` declares a duration that includes it.

The startup figure is the first of these that exists:

| Source | Taken as | How to get it |
|---|---|---|
| `[julia] startup = 240` (or `"4m"`) | stated | write it in the manifest |
| a timing fetched from the testbed, for the same Julia and packages | x 1.1 | `robotarium fetch` after a run: `julia_timing.json` |
| a cold start measured here, for the same Julia and packages | x 1.5 | `robotarium julia check --cold` |
| nothing | 180 s, with a warning | |

"The same Julia and packages" is a key: a sha256 of `[julia] version`, the packages, and the
juliacall and juliapkg versions. A timing of other packages isn't used. Under `"auto"`, the
default, the form's maximum of 600 s is declared, and the assumed figure is only a note; the
checks warn when startup and run come within 60 s of 600 s, and refuse past it.

### Timing a cold start: `robotarium julia check --cold`

```bash
robotarium julia check --cold                 # Julia, registry, packages, all from nothing
robotarium julia check --cold --keep-julia    # reuse the Julia installed here; packages from nothing
robotarium julia check --cold --cpus 2 --call build_operators --call 'probe={"robots": 6}'
```

`--cold` runs the same load as `julia check` in a scratch Julia depot (`JULIA_DEPOT_PATH`),
juliapkg project (`PYTHON_JULIAPKG_PROJECT`) and juliaup depot, with no `julia` or `juliaup`
on `PATH`, so nothing installed here is found. `JULIA_PKG_SERVER`, `JULIA_PKG_USE_CLI_GIT`,
proxies and CA bundles pass through, since a machine that needs them to reach the package
servers needs them for a cold start too. The output streams with the time of each line, and
juliapkg's and Pkg's own lines split it into phases:

```
Cold start of backend.jl (Julia 1.11.9, 4 CPUs): 576 s in all
  installing Julia                    0.0 s   (reused, --keep-julia)
  registry, downloads, resolve      312.3 s
  precompiling                      236.2 s   (211 packages; Pkg says 236 s)
  juliapkg in all                   552.6 s
  starting Julia (juliacall)          2.6 s
  include backend.jl                  5.6 s
  first probe()                      14.0 s   (then 0.001 s warm)
  ready, after                      575.6 s
Saved .robotarium/julia/cold-start.json

Budget: 863 s Julia startup (576 s cold here x 1.5) + 17 s robots (527 steps) = 881 s, with the duration "measured": 600 s would be declared, and the form's maximum is 600 s.
```

That run loaded CellularSheaves.jl 0.1.0 with JSON3 and Graphs, on a machine whose network
reaches GitHub but not Julia's package servers (`JULIA_PKG_SERVER=""`, so the registry and every
package came as git clones). Where the package servers are reachable, as on the testbed,
downloads are much faster: robosheaf's testbed run began to move after about 171 s. A cold
start measured here is then an upper bound, and `[julia] startup` states a better figure until
a testbed timing has been fetched.

| Option | Meaning |
|---|---|
| `--keep-julia` | point juliapkg at the Julia installed here (`PYTHON_JULIAPKG_EXE`) rather than download one |
| `--cpus N` | precompile with N tasks (`JULIA_NUM_PRECOMPILE_TASKS`), as a smaller machine would |
| `--call FUNC[=JSON]` | after loading, call `FUNC` twice through `call_json` (with `{}` or the JSON given): the first call compiles it |
| `--scratch DIR` | install into DIR, which must be empty or new, rather than a temporary directory |
| `--keep-scratch` | leave the scratch directory in place afterwards |
| `--timeout S` | give up after S seconds (default 3600) |

The first call of each function matters: a package without a precompile workload, as
CellularSheaves.jl is, compiles its methods on that first call. The budget line adds the
last simulation's steps (`.robotarium/runs/last-simulation.json`) when its files match the
bundle's. The command warns when this machine's juliacall or juliapkg differs from the
testbed's, since the install then differs too: `pip install 'juliacall==0.9.35'
'juliapkg==0.1.24'`.

A cold start here is not the testbed's: its machine, its disk and its network are not this
one's, hence the margin of 1.5. Without `--cold`, `julia check` loads the backend in the
environment already installed and prints how long each step took there.

### Timing on the testbed

`load_julia_backend` times each step as it happens: juliapkg's resolve (installing Julia and the
packages), starting Julia, the `include`, and the first `call_json` of each function.
`robotarium_runtime.julia_timings()` returns them:

```python
{"resolve_seconds": 152.1, "start_seconds": 7.9, "include_seconds": 12.4,
 "first_calls": {"build_operators": 13.7}, "total_seconds": 186.1,
 "julia_version": "1.11.9", "juliacall": "0.9.35", "juliapkg": "0.1.24",
 "key": "<sha256>", "loaded_at": 173.0, ...}
```

They are printed into the run's output as one readable line and one marker line,
`-----ROBOTARIUM TIMING {json}-----`, once when the backend has loaded and again, with the first
calls, at the first `RunLogger` save. `RunLogger` keeps them in its record's meta as
`"julia"`. `robotarium fetch` reads the last marker out of `log.txt` into
`.robotarium/results/<id>/julia_timing.json`, which the budget then prefers to any local
measurement.

## The CONFIG pattern, for comparison

robosheaf bakes its whole configuration into the main file as a dict literal between two marker
comments. This package does the same job more generally: the `[config]` table (or `config =
"params.toml"`) is carried as `CONFIG` in `robotarium_bundle.py` and read with
`robotarium_runtime.config()`. Your main file never needs regenerating.
