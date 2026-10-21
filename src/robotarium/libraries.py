"""
What Python the Robotarium can import: its libraries.txt, read as importable module names.

The testbed runs a fixed environment, published as libraries.txt in robotarium_python_simulator;
an import outside it is an ImportError at the far end of the queue. The list is read from, in
order: a path given explicitly, the libraries.txt of the simulator checkout `rps` is imported
from (fresher, when the simulator is installed editable from a clone), and the copy shipped in
this package (data/libraries.txt).

libraries.txt names distributions ("opencv-python"), not modules ("cv2"); DISTRIBUTION_MODULES
maps the ones whose names differ.
"""

import importlib.util
import re
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

#: Import names that differ from the distribution's name, for what libraries.txt lists.
DISTRIBUTION_MODULES = {
    "opencv-python": ("cv2",),
    "scikit-learn": ("sklearn",),
    "pyyaml": ("yaml",),
    "pillow": ("PIL",),
    "python-dateutil": ("dateutil",),
    "gtsam-develop": ("gtsam",),
    "matplotlib": ("matplotlib", "mpl_toolkits", "pylab"),
    "fonttools": ("fontTools",),
    "pyocclient": ("owncloud",),
    "cuda-bindings": ("cuda",),
    "cuda-pathfinder": ("cuda",),
    "pytest": ("pytest", "_pytest"),
    "setuptools": ("setuptools", "pkg_resources"),
}

#: Importable on the testbed though libraries.txt does not list them: the Robotarium's own API.
ALWAYS_AVAILABLE = ("rps",)


@dataclass
class Libraries:
    """
    The testbed's Python environment.

    Args:
        source (str): Where the list was read from.
        distributions (dict): Distribution name, lowercased, to its pinned version.
    """

    source: str
    distributions: dict

    @property
    def modules(self) -> set:
        """Every top-level module the listed distributions provide, plus ALWAYS_AVAILABLE."""
        names = set(ALWAYS_AVAILABLE)
        for distribution in self.distributions:
            if distribution in DISTRIBUTION_MODULES:
                names.update(DISTRIBUTION_MODULES[distribution])
            elif distribution.startswith("nvidia-"):
                names.add("nvidia")
            else:
                names.add(re.sub(r"[-.]", "_", distribution))
        return names

    def provides(self, module: str) -> bool:
        """Whether `module`, a top-level import name, is importable on the testbed."""
        return module in self.modules or module.lower() in self.modules

    def version(self, distribution: str):
        """The version pinned for a distribution, or None."""
        return self.distributions.get(distribution.lower())


def parse(text: str) -> dict:
    """Distribution name (lowercased) to version, from requirements-style text."""
    pinned = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"([A-Za-z0-9_.\-]+)\s*(?:==\s*([^\s;]+))?", line)
        if match:
            pinned[match.group(1).lower()] = match.group(2)
    return pinned


def simulator_libraries_file():
    """The libraries.txt beside the `rps` this Python imports, when it is a checkout; or None."""
    try:
        spec = importlib.util.find_spec("rps")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    candidate = Path(spec.origin).resolve().parent.parent / "libraries.txt"
    return candidate if candidate.is_file() else None


def load(path=None, simulator_path=None) -> Libraries:
    """
    The testbed's environment, from the first source that has it; see the module docstring.

    Args:
        path: A libraries.txt to read instead.
        simulator_path: A robotarium_python_simulator checkout, whose libraries.txt comes next.
    """
    candidates = [Path(path)] if path else []
    if simulator_path:
        candidates.append(Path(simulator_path) / "libraries.txt")
    found = simulator_libraries_file()
    if found:
        candidates.append(found)
    for candidate in candidates:
        if candidate.is_file():
            return Libraries(str(candidate), parse(candidate.read_text(encoding="utf-8")))
    shipped = resources.files("robotarium").joinpath("data/libraries.txt")
    return Libraries("the copy shipped with robotarium (%s)" % SHIPPED_FROM,
                     parse(shipped.read_text(encoding="utf-8")))


#: Which libraries.txt data/libraries.txt was copied from.
SHIPPED_FROM = "robotarium_python_simulator ba1d74e, 2026-06-29"


def stdlib_modules() -> set:
    """The standard library's top-level modules, as this interpreter knows them."""
    return set(sys.stdlib_module_names)
