"""
Fetching what the testbed returns for an experiment: the sections of its page on the site, as
files.

    .robotarium/results/<experiment id>/
        experiment.json         the record the page shows: title, status, dates, robots, ...
        return_messages.txt     what the testbed said about the run, when it said anything
        video.mp4               the overhead video
        logs/                   the log files (log.txt)
        data/                   the data files the run saved beside its main file
        scripts/                the files as uploaded, with --scripts

A file already there is kept, so fetching again after more has come back downloads only what is
new; force replaces them. Each download goes to a .part file first and is renamed when complete,
so an interrupted one is never mistaken for a finished one. One that fails is noted in the
result's failed list and the rest are still downloaded.

    from robotarium.results import fetch
    result = fetch("6716f0...", ".robotarium/results/6716f0...")
    print(result.status, result.written)
"""

import email.utils
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import credentials as credentials_module
from . import ledger
from .client import RobotariumClient, RobotariumError, video_problem

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

#: What the record holds in place of an address or an id when there is none: videoUrls is "-"
#: for a run that made no video and "" for one that has not run.
NOTHING = ("", "-")

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
        failed (list): What could not be downloaded, as (file, reason) pairs; the file is the
            section's name instead when the section could not be listed.
    """

    experiment_id: str
    experiment: dict
    directory: Path
    written: list = field(default_factory=list)
    kept: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    return_messages: str = None
    queue: dict = None
    failed: list = field(default_factory=list)

    @property
    def status(self) -> str:
        return str(self.experiment.get("status") or "unknown")

    @property
    def finished(self) -> bool:
        return self.status.lower() in FINISHED

    @property
    def requeued(self) -> bool:
        """
        Whether it has been queued again after running: unfinished, yet with a finishedAt (no
        later than its lastUpdatedAt, when both can be read). The site then lists nothing of the
        earlier run.
        """
        finished_at = self.experiment.get("finishedAt")
        if self.finished or self.status == "draft" or not finished_at:
            return False
        finished_at = _parse_date(finished_at)
        updated_at = _parse_date(self.experiment.get("lastUpdatedAt"))
        try:
            return finished_at is None or updated_at is None or finished_at <= updated_at
        except TypeError:  # one with a time zone, one without
            return True


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
    messages, video, log files and data files (and with scripts, the uploaded files).

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

    urls = _listed(experiment.get("videoUrls"))
    if not urls:
        result.missing.append("video")
        if video:
            out("  no video: the site lists none%s" % ("" if result.finished else " yet"))
    elif video:
        for number, url in enumerate(urls, 1):
            target = directory / ("video.mp4" if number == 1 else "video-%d.mp4" % number)
            again = target.exists() and not force and not _holds_a_video(target)
            if again:
                out("  %s here is not a video (an earlier fetch saved a web page as one): "
                    "downloading it again" % target.name)
            _download(result, target, force or again, out,
                      lambda sink, url=url: client.download_video(url, sink))

    for key, file_type, folder, label in SECTIONS:
        if key == "scripts" and not scripts:
            continue
        ids = _listed(experiment.get(key))
        if not ids:
            result.missing.append(label)
            continue
        try:
            listed = client.file_details(ids, file_type)
        except RobotariumError as error:
            out("  could not list the %s: %s" % (label, error))
            result.failed.append((label, str(error)))
            continue
        taken = set()
        for details in listed:
            file_id = details.get("fileId")
            if not file_id:
                continue
            name = _safe_name(details.get("name"), file_id, taken)
            _download(result, directory / folder / name, force, out,
                      lambda sink, file_id=file_id: client.download_file(file_id, file_type,
                                                                         sink))
    return result


def describe(result: FetchResult) -> str:
    """What fetch() did, as a few lines of text, for `robotarium fetch`."""
    experiment = result.experiment
    lines = ["%s" % (experiment.get("title") or result.experiment_id),
             "  experiment %s: %s" % (result.experiment_id, result.status)]
    queue = result.queue or {}
    if queue.get("queue_position") is not None:
        lines.append("  number %s of %s in the queue" % (queue["queue_position"],
                                                         queue.get("queue_length", "?")))
    earlier = " (its earlier run)" if result.requeued else ""
    for key, label, note in (("createdAt", "created", ""), ("finishedAt", "finished", earlier)):
        if experiment.get(key):
            lines.append("  %-10s %s%s" % (label, experiment[key], note))
    lines.append("")
    for path in result.written:
        lines.append("  wrote   %s" % path)
    for path in result.kept:
        lines.append("  kept    %s" % path)
    for what, reason in result.failed:
        lines.append("  failed  %s: %s" % (what, reason))
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
    elif result.requeued:
        lines += ["", "This experiment has been queued again; files from its earlier run are no "
                      "longer listed. Fetch again once it has run."]
    elif not result.finished:
        lines += ["", "It has not finished: fetch again once it has run."]
    return "\n".join(lines)


def _download(result: FetchResult, target: Path, force: bool, out, write) -> None:
    """
    One file, through a .part file, unless it is already there. A failure, the site's or this
    machine's, is noted in result.failed rather than raised: the other files are still worth
    having.
    """
    if target.exists() and not force:
        result.kept.append(target)
        return
    partial = target.with_name(target.name + ".part")
    out("  downloading %s ..." % target.relative_to(result.directory))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(partial, "wb") as sink:
                write(sink)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    except (RobotariumError, OSError) as error:
        out("    failed, nothing saved: %s" % error)
        result.failed.append((target, str(error)))
        return
    result.written.append(target)


def _holds_a_video(path: Path) -> bool:
    """Whether a file already here begins as a video does, rather than as a web page."""
    try:
        with open(path, "rb") as file:
            head = file.read(1024)
    except OSError:
        return True  # unreadable: left as it is, as any file already here is
    return video_problem("application/octet-stream", head) is None


def _listed(value) -> list:
    """
    The addresses or ids a record's field holds, as a list, leaving out the placeholders
    (NOTHING) it holds when there are none: one string, a list of them, or nothing at all.
    """
    values = value if isinstance(value, (list, tuple)) else [value]
    values = [each.strip() if isinstance(each, str) else each for each in values]
    return [each for each in values if each and each not in NOTHING]


def _parse_date(text):
    """
    A date from the record, as the site writes them ("Thu, 22 Oct 2026 14:24:26 GMT") or in
    ISO form; None when it is neither.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError:
        return None


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
