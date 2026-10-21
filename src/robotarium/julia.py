"""
Tooling for the backend.jl method on this machine: finding a package's UUID, and loading a
bundle's Julia backend locally exactly as the testbed will.

The method itself -- Julia code in a .jl file uploaded beside the main file, its packages
declared to juliapkg before Julia starts, the file include()d through juliacall -- lives in
robotarium_runtime.load_julia_backend, which is what runs on the testbed. docs/julia.md walks
through it.
"""

import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request

from .experiment import JuliaPackage

#: The General registry, raw. ROBOTARIUM_JULIA_REGISTRY overrides it (a mirror, or a stand-in
#: under test).
REGISTRY_URL = "https://raw.githubusercontent.com/JuliaRegistries/General/master/"


class JuliaToolError(RuntimeError):
    """A lookup or local load failed; the message says why."""


def lookup(name: str) -> JuliaPackage:
    """
    A package's UUID and latest unyanked version, from Julia's General registry -- what a
    [julia] packages entry needs.

    Args:
        name (str): The registered name, e.g. "JSON3". Case matters, as it does to Julia.
    """
    base = os.environ.get("ROBOTARIUM_JULIA_REGISTRY") or REGISTRY_URL
    base = base if base.endswith("/") else base + "/"
    directory = "%s%s/%s/" % (base, name[:1].upper(), name)
    package = tomllib.loads(_fetch(directory + "Package.toml", name))
    versions = tomllib.loads(_fetch(directory + "Versions.toml", name))
    available = [version for version, entry in versions.items() if not entry.get("yanked")]
    if not available:
        raise JuliaToolError("%s has no unyanked versions in the registry." % name)
    latest = max(available, key=_version_key)
    return JuliaPackage(name=package.get("name", name), uuid=package["uuid"], version=latest)


def toml_entry(package: JuliaPackage) -> str:
    """The package as a line for robotarium.toml's [julia] packages list."""
    return '{ name = "%s", uuid = "%s", version = "%s" },' % (package.name, package.uuid,
                                                             package.version)


def _fetch(url: str, name: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise JuliaToolError("%s is not in the General registry (names are "
                                 "case-sensitive)." % name) from None
        raise JuliaToolError("The registry answered HTTP %d for %s." % (error.code, url)) \
            from None
    except urllib.error.URLError as error:
        raise JuliaToolError("Could not reach the registry: %s" % error.reason) from None


def _version_key(version: str):
    core, _, prerelease = version.partition("-")
    numbers = tuple(int(part) if part.isdigit() else 0 for part in core.split("."))
    return numbers + ((1,) if not prerelease else (0,))


#: Run in the bundle directory by check(): the testbed's own load path, then a report.
_CHECK_SCRIPT = r"""
import json, sys
import robotarium_runtime as runtime
backend = runtime.load_julia_backend(sys.argv[1])
from juliacall import Main as jl
names = [str(each) for each in jl.names(backend)] if backend is not jl else []
print("ROBOTARIUM_JULIA_CHECK " + json.dumps({"module": str(backend), "names": names}))
"""


def check(bundle, julia_file: str = None, python: str = None, timeout: float = 1800) -> dict:
    """
    Loads the bundle's Julia backend here, through robotarium_runtime from the bundle directory,
    as the testbed will -- packages declared before Julia starts, the file include()d by base
    name -- and returns {"module", "names"}: the module it defines and what it exports.

    The first run installs Julia and the packages, which takes minutes; later ones reuse them.

    Args:
        bundle (Bundle): A built bundle that carries robotarium_runtime.py.
        julia_file (str): Which .jl to load; default the bundle's only one.
        python (str): The interpreter, which needs juliacall; default this one.
        timeout (float): Seconds before giving up.
    """
    julia_files = [path.name for path in bundle.files if path.suffix == ".jl"]
    if julia_file is None:
        if len(julia_files) != 1:
            raise JuliaToolError("The bundle has %s; name the one to load."
                                 % (", ".join(julia_files) or "no .jl file"))
        julia_file = julia_files[0]
    if not (bundle.directory / "robotarium_runtime.py").is_file():
        raise JuliaToolError("The bundle does not carry robotarium_runtime.py: import it from "
                             "the main file, or set [experiment] runtime = true.")

    completed = subprocess.run([python or sys.executable, "-c", _CHECK_SCRIPT, julia_file],
                               cwd=bundle.directory, capture_output=True, text=True,
                               timeout=timeout)
    for line in completed.stdout.splitlines():
        if line.startswith("ROBOTARIUM_JULIA_CHECK "):
            return json.loads(line[len("ROBOTARIUM_JULIA_CHECK "):])
    output = (completed.stderr + completed.stdout).strip()
    # Julia's own "ERROR:" lines say what went wrong; the Python traceback after them only
    # says where it was noticed.
    julia_errors = [line for line in output.splitlines() if line.startswith("ERROR:")]
    last = output.splitlines()[-1:] or ["no output"]
    raise JuliaToolError("Loading %s failed:\n  %s" % (
        julia_file, "\n  ".join(julia_errors + last)))
