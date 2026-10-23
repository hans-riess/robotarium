"""
Fetching what the testbed returns for an experiment: the sections of its page on the site, as
files.

    .robotarium/results/<experiment id>/
        experiment.json         the record the page shows: title, status, dates, robots, ...
        return_messages.txt     what the testbed said about the run, when it said anything
        video.mp4               the overhead video
        logs/                   the log files (log.txt)
        data/                   the data files, and the RunLogger files copied out of log.txt
        scripts/                the files as uploaded, with --scripts

So far the testbed has returned what a run prints but none of the files it writes
(hans-riess/robotarium#7), so robotarium_runtime.RunLogger also prints each write of its file,
base64-encoded between marker lines; fetch decodes those copies back into data/.

A file already there is kept, so fetching again after more has come back downloads only what is
new; force replaces them. Each download goes to a .part file first and is renamed when complete,
so an interrupted one is never mistaken for a finished one.

    from robotarium.results import fetch
    result = fetch("6716f0...", ".robotarium/results/6716f0...")
    print(result.status, result.written)
"""

import base64
import binascii
import hashlib
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import credentials as credentials_module
from . import ledger
from .client import RobotariumClient, RobotariumError

RESULTS_DIR = "results"

#: The page's file sections: the record's key, the type the site files them under, the folder
#: they go to here, and what to call them.
SECTIONS = (
    ("logFiles", "logFiles", "logs", "log files"),
    ("dataFiles", "dataFiles", "data", "data files"),
    ("scripts", "scripts", "scripts", "scripts"),
)

#: Statuses after which nothing more comes back.
FINISHED = ("completed", "error", "failed", "cancelled", "canceled", "rejected")

#: The lines a RunLogger prints a copy of its file between, as robotarium_runtime's COPY_BEGIN
#: and COPY_END write them: the name, the size in bytes and the SHA-256, then base64.
COPY_BEGIN = re.compile(r"^-----BEGIN ROBOTARIUM RUN (.+) (\d+) ([0-9a-f]{64})-----$")
COPY_END = "-----END ROBOTARIUM RUN %s-----"
BASE64_LINE = re.compile(r"^[A-Za-z0-9+/=]+$")

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """
    What fetch() found and wrote.

    Attributes:
        experiment_id (str): The experiment.
        experiment (dict): Its record, as the site returned it.
        directory (Path): Where its files are.
        written (list): Files downloaded this time.
        kept (list): Files that were already there, left alone.
        missing (list): Sections the site has nothing in yet, by name.
        return_messages (str): The return messages as text; None when there are none.
        queue (dict): {"queue_position", "queue_length"} while it waits; None otherwise.
        recovered (dict): The files in written and kept copied out of a log file, each to the
            log file it came from.
        damaged (list): What could not be copied out of the log files, as sentences.
    """

    experiment_id: str
    experiment: dict
    directory: Path
    written: list = field(default_factory=list)
    kept: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    return_messages: str = None
    queue: dict = None
    recovered: dict = field(default_factory=dict)
    damaged: list = field(default_factory=list)

    @property
    def status(self) -> str:
        return str(self.experiment.get("status") or "unknown")

    @property
    def finished(self) -> bool:
        return self.status.lower() in FINISHED


def experiment_id_from(text: str) -> str:
    """
    An experiment id from what a person may paste: the id, or the address of its page
    (https://www.robotarium.gatech.edu/experiment/<id>).
    """
    text = str(text).strip()
    if "/" in text:
        path = urllib.parse.urlsplit(text).path.rstrip("/")
        match = re.search(r"/experiment/([^/]+)$", path)
        if not match:
            raise ValueError("%s is not an experiment's page: expected .../experiment/<id>."
                             % text)
        text = urllib.parse.unquote(match.group(1))
    if not text:
        raise ValueError("No experiment id given.")
    return text


def directory_for(output_dir: Path, experiment_id: str) -> Path:
    """Where an experiment's results go by default: .robotarium/results/<id>/."""
    return Path(output_dir) / RESULTS_DIR / (re.sub(r"[^\w.-]", "_", experiment_id).lstrip(".")
                                             or "_")


def last_submitted(output_dir: Path) -> dict:
    """The ledger's latest attempt that created an experiment; None when there is none."""
    for entry in reversed(ledger.read(output_dir)):
        if entry.get("experiment_id"):
            return entry
    return None


def fetch(experiment_id: str, directory: Path, client: RobotariumClient = None,
          credentials=None, video: bool = True, scripts: bool = False, force: bool = False,
          out=print) -> FetchResult:
    """
    Downloads what the site holds for an experiment into directory: its record, return
    messages, video, log files and data files (and with scripts, the uploaded files). The
    RunLogger files a log file holds copies of are written into data/ too.

    Args:
        experiment_id (str): The experiment's id, as submit() and the ledger record it.
        directory (Path): Where to put it all; created if need be.
        client (RobotariumClient): A logged-in client; by default one is logged in with
            credentials.
        credentials (Credentials): The login, when there is no client; default
            credentials.resolve().
        video (bool): Download the video, which can run to hundreds of megabytes.
        scripts (bool): Download the uploaded files too (the ledger's snapshot holds them).
        force (bool): Download again what is already there.
        out: Where progress is printed; a function taking a line.
    """
    if client is None:
        found = credentials or credentials_module.resolve()
        client = RobotariumClient()
        client.login(found.username, found.password)

    directory = Path(directory)
    experiment = client.experiment(experiment_id)
    result = FetchResult(experiment_id=experiment_id, experiment=experiment, directory=directory)
    directory.mkdir(parents=True, exist_ok=True)
    _write_text(directory / "experiment.json",
                json.dumps(experiment, indent=2, sort_keys=True, default=str) + "\n")

    if not result.finished:
        try:
            queue = client.queue_position(experiment_id)
            result.queue = queue if isinstance(queue, dict) else None
        except RobotariumError as error:
            log.debug("No queue position for %s: %s", experiment_id, error)

    messages = experiment.get("returnMessages")
    if messages:
        result.return_messages = _as_text(messages)
        _write_text(directory / "return_messages.txt", result.return_messages + "\n")
        result.written.append(directory / "return_messages.txt")
    else:
        result.missing.append("return messages")

    urls = experiment.get("videoUrls")
    urls = [urls] if isinstance(urls, str) else [each for each in urls or [] if each]
    if not urls:
        result.missing.append("video")
    elif video:
        for number, url in enumerate(urls, 1):
            target = directory / ("video.mp4" if number == 1 else "video-%d.mp4" % number)
            _download(result, target, force, out,
                      lambda sink, url=url: client.download("%s/download" % url, sink))

    for key, file_type, folder, label in SECTIONS:
        if key == "scripts" and not scripts:
            continue
        ids = experiment.get(key) or []
        ids = [ids] if isinstance(ids, str) else [each for each in ids if each]
        if not ids:
            result.missing.append(label)
            continue
        taken = set()
        for details in client.file_details(ids, file_type):
            file_id = details.get("fileId")
            if not file_id:
                continue
            name = _safe_name(details.get("name"), file_id, taken)
            _download(result, directory / folder / name, force, out,
                      lambda sink, file_id=file_id: client.download_file(file_id, file_type,
                                                                         sink))
    _recover(result, force, out)
    return result


def run_copies(text: str) -> tuple:
    """
    The RunLogger files a run's output holds copies of.

    Returns:
        (found, damaged): found maps each file's name to the bytes of its last complete copy
        (a logger that autosaves prints one per write); damaged lists the names whose last
        copy is cut short or does not match its size and checksum.
    """
    found, damaged = {}, []
    current = None
    for line in text.splitlines():
        line = line.strip()
        begun = COPY_BEGIN.match(line)
        if current and (begun or line == COPY_END % current[0]):
            name, size, digest, lines = current
            content = None if begun else _decoded(lines, size, digest)
            if content is None:
                damaged.append(name)
            else:
                found[name] = content
                damaged = [each for each in damaged if each != name]
            current = None
        if begun:
            current = (begun.group(1), int(begun.group(2)), begun.group(3), [])
        elif current and BASE64_LINE.match(line):
            current[3].append(line)
        # Any other line inside a copy is someone else's output, printed meanwhile; the
        # checksum says whether the copy survived it.
    if current:
        damaged.append(current[0])
    return found, list(dict.fromkeys(damaged))


def _decoded(lines: list, size: int, digest: str):
    """A copy's bytes, or None when they are not what its first line says."""
    try:
        content = base64.b64decode("".join(lines), validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        return None
    return content


def _recover(result: FetchResult, force: bool, out) -> None:
    """
    Writes into data/ the RunLogger files the run printed into its log files. A data file the
    site returned under the same name is the one kept.
    """
    logs, data = result.directory / "logs", result.directory / "data"
    fetched = result.written + result.kept
    returned = {path.name.lower() for path in fetched if path.parent == data}
    for log_file in sorted(path for path in fetched if path.parent == logs):
        found, damaged = run_copies(log_file.read_text(encoding="utf-8", errors="replace"))
        found = {_safe_name(name, "run.npz", set()): content for name, content in found.items()}
        source = log_file.relative_to(result.directory)
        for name in damaged:
            name = _safe_name(name, "run.npz", set())
            result.damaged.append(
                "The last copy of %s in %s is incomplete (the run may have been stopped while "
                "printing it); %s" % (name, source, "data/%s is the one before it." % name
                                      if name in found else "there is no complete one."))
        for name, content in found.items():
            if name.lower() in returned:
                continue
            target = data / name
            result.recovered[target] = log_file
            if target.exists() and not force:
                result.kept.append(target)
                continue
            out("  copying %s out of %s ..." % (target.relative_to(result.directory), source))
            data.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            try:
                partial.write_bytes(content)
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
            result.written.append(target)
    if result.recovered and "data files" in result.missing:
        result.missing.remove("data files")


def describe(result: FetchResult) -> str:
    """What fetch() did, as a few lines of text, for `robotarium fetch`."""
    experiment = result.experiment
    lines = ["%s" % (experiment.get("title") or result.experiment_id),
             "  experiment %s: %s" % (result.experiment_id, result.status)]
    queue = result.queue or {}
    if queue.get("queue_position") is not None:
        lines.append("  number %s of %s in the queue" % (queue["queue_position"],
                                                         queue.get("queue_length", "?")))
    for key, label in (("createdAt", "created"), ("finishedAt", "finished")):
        if experiment.get(key):
            lines.append("  %-10s %s" % (label, experiment[key]))
    lines.append("")
    for verb, paths in (("wrote", result.written), ("kept", result.kept)):
        for path in paths:
            source = result.recovered.get(path)
            lines.append("  %-7s %s%s" % (verb, path, " (copied out of %s)" % source.name
                                          if source else ""))
    for sentence in result.damaged:
        lines.append("  %s" % sentence)
    if result.missing:
        lines.append("  %s: %s" % ("nothing came back for" if result.finished
                                   else "not available yet", ", ".join(result.missing)))
    if result.return_messages:
        shown = result.return_messages.splitlines()
        lines += ["", "Return messages:"] + ["  " + each for each in shown[:40]]
        if len(shown) > 40:
            lines.append("  ... %d more line(s) in %s" % (len(shown) - 40,
                                                          result.directory /
                                                          "return_messages.txt"))
    if result.status == "draft":
        lines += ["", "It is a draft: submit it from the dashboard to have it run."]
    elif not result.finished:
        lines += ["", "It has not finished: fetch again once it has run."]
    return "\n".join(lines)


def _download(result: FetchResult, target: Path, force: bool, out, write) -> None:
    """One file, through a .part file, unless it is already there."""
    if target.exists() and not force:
        result.kept.append(target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    out("  downloading %s ..." % target.relative_to(result.directory))
    try:
        with open(partial, "wb") as sink:
            write(sink)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    result.written.append(target)


def _safe_name(name, file_id: str, taken: set) -> str:
    """
    A file's name as the site gives it, cut down to a plain base name that cannot leave its
    folder; its id instead when nothing usable is left, and with the id in front when two files
    in one section share a name.
    """
    name = re.split(r"[\\/]", str(name or ""))[-1].strip()
    name = re.sub(r"[\x00-\x1f]", "", name).lstrip(".")
    name = name or re.sub(r"[^\w.-]", "_", file_id)
    if name.lower() in taken:
        name = "%s-%s" % (re.sub(r"[^\w.-]", "_", file_id), name)
    taken.add(name.lower())
    return name


def _as_text(messages) -> str:
    """The return messages, whatever shape the site gives them in, as text."""
    if isinstance(messages, str):
        return messages.rstrip()
    if isinstance(messages, list) and all(isinstance(each, str) for each in messages):
        return "\n".join(each.rstrip() for each in messages)
    return json.dumps(messages, indent=2, sort_keys=True, default=str)


def _write_text(path: Path, text: str) -> None:
    partial = path.with_name(path.name + ".part")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)
