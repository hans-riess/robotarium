"""
Static checks over the files of a bundle: what would fail on the testbed, found on this machine.

Each of these has cost, or would cost, a round trip through the Robotarium's queue -- an
experiment that waits its turn, runs, and comes back as a traceback:

    an import the testbed does not have       checked against its libraries.txt
    a local module imported but not uploaded  the form uploads only what it is given
    a relative import                         the bundle is flat, the main file no package
    numpy spelled newer than the executors    robosheaf's np.reshape(a, shape=...)
    a number_of_robots that disagrees         with the form's "Number of Robots"
    no r.debug() / r.call_at_scripts_end()    which the Robotarium requires at the end
    a Julia package nobody declares           nothing installs it on the testbed
    an include()d .jl that was not uploaded

None of this runs the code; simulate.py does that.
"""

import ast
import re
from pathlib import Path

from .experiment import BUNDLE_MODULE, GENERATED_NAMES, RUNTIME_MODULE
from .findings import ERROR, INFO, WARNING, Finding
from .libraries import (ALWAYS_AVAILABLE, TESTBED_PYTHON, newer_stdlib_modules,
                        stdlib_modules)

#: Spellings that work on the machine a bundle is built on and fail on the one that runs it.
#: This list is what has cost a round trip on robosheaf and its sibling project, not a model of
#: numpy's release history.
NEWER_THAN_THE_EXECUTORS = (
    (r"np\.reshape\([^\n]*\bshape\s*=",
     "np.reshape(a, shape=...) needs numpy 2.1, newer than the Robotarium executors have been "
     "seen to run. Pass the shape positionally: np.reshape(a, (2, 1))."),
    (r"np\.reshape\([^\n]*\bnewshape\s*=",
     "np.reshape(a, newshape=...) was removed in numpy 2.3. Pass the shape positionally."),
    (r"\bnp\.trapezoid\b",
     "np.trapezoid needs numpy 2.0. np.trapz is the spelling both versions accept."),
)

#: The calls that end a Robotarium experiment; the testbed requires one.
END_CALLS = ("call_at_scripts_end", "debug")

#: Julia's standard library, which needs no declaring: it ships with Julia. PythonCall comes
#: with juliacall.
JULIA_STDLIB = frozenset((
    "Base", "Core", "Main", "ArgTools", "Artifacts", "Base64", "CRC32c", "Dates",
    "DelimitedFiles", "Distributed", "Downloads", "FileWatching", "Future", "InteractiveUtils",
    "LazyArtifacts", "LibCURL", "LibGit2", "Libdl", "LinearAlgebra", "Logging", "Markdown",
    "Mmap", "NetworkOptions", "Pkg", "Printf", "Profile", "REPL", "Random", "SHA",
    "Serialization", "SharedArrays", "Sockets", "SparseArrays", "Statistics", "SuiteSparse",
    "TOML", "Tar", "Test", "UUIDs", "Unicode", "PythonCall",
))


def check(paths: list, experiment, libraries) -> list:
    """
    Every finding over a bundle's files.

    Args:
        paths (list): The files to be uploaded, main first, as they sit in the bundle.
        experiment (Experiment): What they are for.
        libraries (Libraries): The testbed's Python environment.
    """
    findings = []
    paths = [Path(each) for each in paths]
    names = {path.name for path in paths}
    main = paths[0]
    # What this package generated or vendored is checked by its own test suite, not here; it
    # still counts as uploaded, for the imports of the files that are checked.
    python_files = [path for path in paths
                    if path.suffix == ".py" and path.name not in GENERATED_NAMES]
    julia_files = [path for path in paths if path.suffix == ".jl"]

    trees = {}
    for path in python_files:
        source = path.read_text(encoding="utf-8")
        try:
            trees[path] = ast.parse(source, filename=path.name)
        except SyntaxError as error:
            findings.append(Finding(ERROR, "Does not compile: %s." % error.msg, file=path.name,
                                    line=error.lineno))
            continue
        findings.extend(_check_imports(path, trees[path], names, experiment, libraries))
        findings.extend(_check_numpy_spellings(path, source))

    if main.suffix == ".py" and main in trees:
        findings.extend(_check_main(main, trees, experiment))

    if julia_files:
        findings.extend(_check_julia(julia_files, names, trees, experiment))

    return findings


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

def imports(tree: ast.AST) -> list:
    """
    The imports in a module as (top-level name, line, level, optional) tuples. level is the
    relative-import depth (0 for absolute). optional is True for an import guarded by a try
    that catches ImportError, under `if TYPE_CHECKING:`, or under a sys.version_info test -- it
    may fail without failing the run.
    """
    found = []

    def visit(node, optional):
        if isinstance(node, ast.Try):
            guarded = optional or any(_catches_import_error(handler) for handler in node.handlers)
            for child in node.body:
                visit(child, guarded)
            for child in node.handlers + node.orelse + node.finalbody:
                visit(child, optional)
            return
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.orelse:
                visit(child, optional)
            return
        if isinstance(node, ast.If) and _is_version_check(node.test):
            # `if sys.version_info >= (3, 11): import tomllib / else: import tomli`: the branch
            # the testbed's Python takes is checked; the other is optional. A test that cannot
            # be worked out leaves both optional.
            on_testbed = _version_test_on_testbed(node.test)
            for child in node.body:
                visit(child, optional or on_testbed is not True)
            for child in node.orelse:
                visit(child, optional or on_testbed is not False)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno, 0, optional))
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            found.append((module, node.lineno, node.level, optional))
        for child in ast.iter_child_nodes(node):
            visit(child, optional)

    visit(tree, False)
    return found


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(each, ast.Name)
               and each.id in ("ImportError", "ModuleNotFoundError", "Exception")
               for each in types)


def _is_version_check(test: ast.AST) -> bool:
    return any(isinstance(node, ast.Attribute) and node.attr == "version_info"
               for node in ast.walk(test))


#: The testbed's Python as sys.version_info would compare; see libraries.TESTBED_PYTHON.
_TESTBED_VERSION = (3, 10, 0)

_COMPARE = {ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
            ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
            ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b}


def _version_test_on_testbed(test: ast.AST):
    """
    `sys.version_info <op> (3, 11)` (or `sys.version_info[:2]`, either side) worked out for the
    testbed's Python: True or False, or None for any test this cannot evaluate.
    """
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1
            and type(test.ops[0]) in _COMPARE):
        return None
    left, right = test.left, test.comparators[0]

    def version(node):
        if isinstance(node, ast.Subscript):
            if not (isinstance(node.slice, ast.Slice) and node.slice.lower is None
                    and node.slice.step is None and isinstance(node.slice.upper, ast.Constant)):
                return None
            whole = version(node.value)
            return None if whole is None else whole[:node.slice.upper.value]
        if isinstance(node, ast.Attribute) and node.attr == "version_info":
            return _TESTBED_VERSION
        return None

    def literal(node):
        try:
            value = ast.literal_eval(node)
        except ValueError:
            return None
        return value if isinstance(value, tuple) and all(isinstance(each, int)
                                                         for each in value) else None

    if version(left) is not None and literal(right) is not None:
        return _COMPARE[type(test.ops[0])](version(left), literal(right))
    if literal(left) is not None and version(right) is not None:
        return _COMPARE[type(test.ops[0])](literal(left), version(right))
    return None


def _is_type_checking(test: ast.AST) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or \
        (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _check_imports(path: Path, tree: ast.AST, names: set, experiment, libraries) -> list:
    findings = []
    stdlib = stdlib_modules()
    newer = newer_stdlib_modules()
    allowed = set(experiment.allow_imports or ())
    local_roots = {experiment.root, Path(experiment.main).parent}

    for module, line, level, optional in imports(tree):
        if level:
            findings.append(Finding(ERROR, "A relative import (from %s%s import ...) fails on the "
                                           "testbed, where the main file is not in a package. "
                                           "Import the module by name: the bundle is flat."
                                           % ("." * level, module), file=path.name, line=line))
            continue
        # The Robotarium's own API is there whatever is on disk here: a project inside a
        # simulator clone has an rps/ beside it, and must not be told rps cannot travel.
        if not module or module in stdlib or module in allowed or module in ALWAYS_AVAILABLE \
                or "%s.py" % module in names:
            continue
        if module in newer and not optional:
            findings.append(Finding(WARNING, "%s is not in the standard library of Python %s, "
                                             "which the testbed appears to run (its "
                                             "libraries.txt ships backports such as tomli)."
                                    % (module, TESTBED_PYTHON), file=path.name, line=line))
            continue
        if module == "robotarium":
            findings.append(Finding(ERROR, "The robotarium package exists only on this machine. "
                                           "Code that runs on the testbed imports "
                                           "robotarium_runtime instead.", file=path.name,
                                    line=line))
            continue
        if module == RUNTIME_MODULE[:-3]:
            findings.append(Finding(ERROR, "robotarium_runtime is imported but not in the bundle; "
                                           "set [experiment] runtime to true or \"auto\".",
                                    file=path.name, line=line))
            continue
        if module == BUNDLE_MODULE[:-3]:
            findings.append(Finding(ERROR, "robotarium_bundle is imported but not generated; "
                                           "declare a config, or import robotarium_runtime and "
                                           "use config().", file=path.name, line=line))
            continue
        here = [root / ("%s.py" % module) for root in local_roots] + \
            [root / module / "__init__.py" for root in local_roots]
        local = next((each for each in here if each.is_file()), None)
        if local is not None and local.name == "__init__.py":
            findings.append(Finding(ERROR, "%s is a package directory here; the form uploads "
                                           "files only, flat, so it cannot travel. Move what the "
                                           "experiment needs into modules beside the main file."
                                           % module, file=path.name, line=line))
        elif local is not None:
            findings.append(Finding(ERROR, "%s is imported but not uploaded; add %s to "
                                           "[experiment] files." % (module, local.name),
                                    file=path.name, line=line))
        elif libraries.provides(module):
            continue
        elif not optional:
            findings.append(Finding(WARNING, "%s is not in the Robotarium's libraries.txt (%s), "
                                             "so the import will fail there. If you know it is "
                                             "available, add it to [experiment] allow_imports."
                                             % (module, libraries.source), file=path.name,
                                    line=line))
    return findings


def _check_numpy_spellings(path: Path, source: str) -> list:
    findings = []
    for pattern, explanation in NEWER_THAN_THE_EXECUTORS:
        for match in re.finditer(pattern, source):
            line = source.count("\n", 0, match.start()) + 1
            findings.append(Finding(WARNING, explanation, file=path.name, line=line))
    return findings


def _check_main(main: Path, trees: dict, experiment) -> list:
    findings = []
    main_tree = trees[main]

    if not any(isinstance(module, str) and module == "rps"
               for tree in trees.values() for module, _, _, _ in imports(tree)):
        findings.append(Finding(WARNING, "No uploaded file imports rps; the testbed runs an "
                                         "experiment through rps.robotarium.Robotarium.",
                                file=main.name))

    ends = [node for tree in trees.values() for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in END_CALLS]
    if not ends:
        findings.append(Finding(WARNING, "Nothing calls r.debug() (or r.call_at_scripts_end()), "
                                         "which the Robotarium requires at the end of every "
                                         "experiment.", file=main.name))

    constants = _module_int_constants(main_tree)
    for node in ast.walk(main_tree):
        if not (isinstance(node, ast.Call) and _call_name(node.func) == "Robotarium"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "skip_initialization" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value is False):
                findings.append(Finding(WARNING, "skip_initialization: the testbed always drives "
                                                 "the robots to their initial poses first, so "
                                                 "a run without that drive is not the run it "
                                                 "will do (robotarium simulate does the drive "
                                                 "regardless).", file=main.name,
                                        line=node.lineno))
            if keyword.arg != "number_of_robots":
                continue
            value = keyword.value
            count = value.value if isinstance(value, ast.Constant) else \
                constants.get(value.id) if isinstance(value, ast.Name) else None
            if isinstance(count, int) and not isinstance(count, bool) \
                    and isinstance(experiment.robots, int) and count != experiment.robots:
                findings.append(Finding(ERROR, "Robotarium(number_of_robots=%d), but the form "
                                               "would say %d robots. They must agree."
                                        % (count, experiment.robots), file=main.name,
                                        line=node.lineno))
    return findings


def _call_name(func: ast.AST):
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _module_int_constants(tree: ast.Module) -> dict:
    """Names assigned exactly once at module level, to an int literal."""
    counts, values = {}, {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    counts[target.id] = counts.get(target.id, 0) + 1
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
                        values[target.id] = node.value.value
    return {name: value for name, value in values.items() if counts[name] == 1}


# ---------------------------------------------------------------------------
# Julia
# ---------------------------------------------------------------------------

_STATEMENT = re.compile(r"^\s*(?:@[\w.]+\s+)*(?:using|import)\s+(.*)$", re.DOTALL)
_NAME = re.compile(r"[^\W\d][\w!]*")


_CHAR = re.compile(r"'(?:\\.[^']{0,8}|[^'\\\n])'")


def julia_code(source: str) -> str:
    """
    A Julia source with every comment and the inside of every string, command and character
    literal blanked -- replaced by spaces, newlines kept, so each character stays where it was.
    What is left is code alone: a `;` or `#` or the word `using` inside a string cannot be read
    as a statement. Read as Julia's own lexer reads it: `#=` comments nest, triple-quoted strings
    span lines, `$(...)` inside a string is code that may hold strings of its own, and a `'` is a
    character literal unless it follows something it could transpose (`A'`).
    """
    masked = list(source)

    def blank(start, stop):
        for k in range(start, min(stop, len(source))):
            if masked[k] != "\n":
                masked[k] = " "

    i, n = 0, len(source)
    while i < n:
        j = _skip_non_code(source, i)
        if j == i:
            i += 1
            continue
        quote = _quote_at(source, i)
        if quote:
            # Keep the delimiters, so include("x.jl") can still be found by position.
            blank(i + len(quote), max(i + len(quote), j - len(quote)))
        else:
            blank(i, j)
        i = j
    return "".join(masked)


def _quote_at(source: str, i: int):
    for quote in ('"""', "```", '"', "`"):
        if source.startswith(quote, i):
            return quote
    return None


def _skip_non_code(source: str, i: int) -> int:
    """
    If a comment, string, command or character literal starts at i, the index just past it;
    otherwise i.
    """
    n = len(source)
    if source.startswith("#=", i):
        depth, j = 1, i + 2
        while j < n and depth:
            if source.startswith("#=", j):
                depth, j = depth + 1, j + 2
            elif source.startswith("=#", j):
                depth, j = depth - 1, j + 2
            else:
                j += 1
        return j
    if source[i] == "#":
        j = source.find("\n", i)
        return n if j < 0 else j
    quote = _quote_at(source, i)
    if quote:
        # A prefixed string (raw"...", r"...") has no interpolation.
        prefixed = i > 0 and (source[i - 1].isalnum() or source[i - 1] == "_")
        j = i + len(quote)
        while j < n and not source.startswith(quote, j):
            if source[j] == "\\":
                j += 2
            elif not prefixed and source.startswith("$(", j):
                j = _skip_parenthesised(source, j + 2)
            else:
                j += 1
        return min(n, j + len(quote))
    if source[i] == "'" and not (i > 0 and (source[i - 1].isalnum()
                                            or source[i - 1] in ")]}'_.!")):
        match = _CHAR.match(source, i)
        if match:
            return match.end()
    return i


def _skip_parenthesised(source: str, j: int) -> int:
    """The index just past the `)` closing a `$(` whose contents start at j."""
    depth, n = 1, len(source)
    while j < n:
        after = _skip_non_code(source, j)
        if after != j:
            j = after
            continue
        if source[j] == "(":
            depth += 1
        elif source[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def julia_includes(source: str) -> list:
    """The paths a Julia source include()s, from code only: not from comments or strings."""
    code = julia_code(source)
    found = []
    for match in re.finditer(r"\binclude\s*\(\s*\"", code):
        close = code.find('"', match.end())
        if close > match.end():
            found.append(source[match.end():close])
    return found


def julia_packages_used(source: str) -> list:
    """
    The packages a Julia source loads with `using` or `import`, in order, without repeats.

    Handles several statements on a line (`using A; using B`), a list continued over lines
    (`using A,` / `B`, blank or comment lines between), `using A: f, g`, `import A as B`,
    `import A.Sub`, and a macro in front (`@everywhere using A`). Strings and comments are
    ignored (see julia_code). Relative imports (`using .Inner`) are the file's own modules and
    are left out.
    """
    code = re.sub(r",\s*\n", ", ", julia_code(source))
    used = []
    for line in code.splitlines():
        for statement in line.split(";"):
            match = _STATEMENT.match(statement)
            if not match:
                continue
            clause = match.group(1).split(":")[0]
            for item in clause.split(","):
                item = item.strip()
                if item.startswith("."):
                    continue
                name = _NAME.match(item)
                if name and name.group(0) not in used:
                    used.append(name.group(0))
    return used


def _check_julia(julia_files: list, names: set, trees: dict, experiment) -> list:
    findings = []
    declared = {package.name for package in experiment.julia_packages}
    used = set()

    loaders = set()
    for tree in trees.values():
        for module, _, _, _ in imports(tree):
            loaders.add(module)

    # Code that declares packages itself -- juliapkg directly, or the runtime given packages= --
    # may well install what the manifest does not name, so an undeclared one is only a warning.
    declares_in_code = "juliapkg" in loaders or any(
        isinstance(node, ast.Call) and (
            _call_name(node.func) == "declare_julia_packages"
            or (_call_name(node.func) == "load_julia_backend"
                and any(keyword.arg == "packages" for keyword in node.keywords)))
        for tree in trees.values() for node in ast.walk(tree))

    for path in julia_files:
        source = path.read_text(encoding="utf-8")
        for package in julia_packages_used(source):
            used.add(package)
            if package in JULIA_STDLIB or package in declared:
                continue
            findings.append(Finding(
                WARNING if declares_in_code else ERROR,
                "%s loads %s, which nothing installs on the testbed. Declare it under [julia] "
                "packages in robotarium.toml (`robotarium julia lookup %s` prints the entry)."
                % (path.name, package, package), file=path.name))
        for included in julia_includes(source):
            if Path(included).name not in names:
                findings.append(Finding(ERROR, "include(%r), but %s is not uploaded."
                                               % (included, Path(included).name),
                                        file=path.name))
            elif "/" in included or "\\" in included:
                findings.append(Finding(ERROR, "include(%r): every file lands in one directory "
                                               "on the testbed; include it by base name."
                                        % included, file=path.name))

    if not loaders & {"juliacall", "robotarium_runtime"}:
        findings.append(Finding(WARNING, "%s %s uploaded, but no Python file imports juliacall "
                                         "or robotarium_runtime to load it."
                                % (", ".join(path.name for path in julia_files),
                                   "is" if len(julia_files) == 1 else "are"),
                                file=julia_files[0].name))
    elif "robotarium_runtime" not in loaders and declared:
        findings.append(Finding(INFO, "[julia] packages are declared to the testbed only through "
                                      "robotarium_runtime.load_julia_backend; code that imports "
                                      "juliacall directly must call juliapkg.add itself, before "
                                      "Julia starts."))

    for package in sorted(declared - used):
        findings.append(Finding(INFO, "[julia] declares %s, but no uploaded .jl file loads it."
                                % package))
    return findings
