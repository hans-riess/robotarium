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

## Checking it

`robotarium check` reads every `using` and `import` in the uploaded `.jl` files. It reports as an
error any package that's neither in the standard library nor declared, since nothing would
install it on the testbed. It also errors on an `include("x.jl")` whose file isn't uploaded, or
that's included by a path rather than a base name, because every file lands in one directory.
It warns when a `.jl` is uploaded but no Python file imports juliacall or `robotarium_runtime`
to load it.

`robotarium julia check` goes further. It builds the bundle and loads the backend from the bundle
directory through the same `robotarium_runtime` code the testbed runs: packages declared, Julia
started, file included. It then prints the module and what it exports. The first run downloads
Julia and the packages into your environment, which takes a few minutes.

```bash
pip install -e ".[julia]"     # in your clone, venv active: juliacall, which brings juliapkg
robotarium julia check
```

`robotarium simulate` then runs the whole experiment, Julia included, in the simulator.

## On the testbed

`juliacall` and `juliapkg` are on the Robotarium's library list (`libraries.txt` in the
simulator repository), and robosheaf has run CellularSheaves.jl there this way. Julia and the
declared packages are installed when the experiment starts, which needs the testbed to reach
Julia's package servers. If `load_julia_backend` fails there, the message distinguishes a package
that couldn't be installed from a `.jl` file that couldn't be loaded.

Installing takes time at the start of a run. Leave headroom in the estimated duration, or use
`duration = "auto"`, which measures the robots' steps but not Julia's startup. The testbed's
clock may start before the robots move, so err on the long side.

## The CONFIG pattern, for comparison

robosheaf bakes its whole configuration into the main file as a dict literal between two marker
comments. This package does the same job more generally: the `[config]` table (or `config =
"params.toml"`) is carried as `CONFIG` in `robotarium_bundle.py` and read with
`robotarium_runtime.config()`. Your main file never needs regenerating.
