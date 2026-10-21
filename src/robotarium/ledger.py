"""
The submission ledger: one JSON line per attempt to submit, and a zip of exactly what was sent.

    .robotarium/submissions.jsonl                       when, what, which experiment id, how
                                                        it went
    .robotarium/snapshots/<title>-<stamp>.zip           the files as uploaded

Results come back from the testbed hours or days after the upload, by which time the files have
moved on; the snapshot is what lets a returned video or run log be opened against the exact code
that produced it, and the ledger says which snapshot that is. Failed attempts are recorded too,
with the reason, since "did that go through?" is the question the ledger is for.

Commit submissions.jsonl if the history is worth sharing; the snapshots are reproducible from it
and the commit it records only while that commit exists, so keep them if in doubt.
"""

import datetime
import json
import zipfile
from pathlib import Path

from .bundle import slug

LEDGER_NAME = "submissions.jsonl"
SNAPSHOT_DIR = "snapshots"


def snapshot(bundle, output_dir: Path) -> Path:
    """
    Zips the bundle's files, as they are about to be uploaded, and returns where.

    Args:
        bundle (Bundle): What is being sent.
        output_dir (Path): The project's .robotarium directory.
    """
    stamp = datetime.datetime.fromisoformat(bundle.built_at).strftime("%Y%m%dT%H%M%S")
    label = "%s-%s" % (slug(bundle.title), stamp)
    archive = Path(output_dir) / SNAPSHOT_DIR / ("%s.zip" % label)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        for path in bundle.files:
            handle.write(path, arcname="%s/%s" % (label, path.name))
    return archive


def record(output_dir: Path, entry: dict) -> Path:
    """
    Appends one attempt to the ledger and returns the ledger's path.

    Args:
        output_dir (Path): The project's .robotarium directory.
        entry (dict): What submission.submit records; JSON-serialisable.
    """
    ledger = Path(output_dir) / LEDGER_NAME
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return ledger


def read(output_dir: Path) -> list:
    """Every attempt in the ledger, oldest first; empty when there is none."""
    ledger = Path(output_dir) / LEDGER_NAME
    if not ledger.is_file():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def describe(entry: dict) -> str:
    """One attempt as a few lines of text, for `robotarium history`."""
    outcome = entry.get("outcome", "?")
    if outcome == "submitted":
        status = "experiment %s %s" % (entry.get("experiment_id"),
                                       "saved as a draft" if entry.get("draft") else "queued")
    else:
        status = "%s%s: %s" % (outcome.upper(), " at %s" % entry["stage"] if entry.get("stage")
                               else "", entry.get("error", "no reason recorded"))
        if entry.get("consequence"):
            status += "\n    %s" % entry["consequence"]
    lines = ["%s  %s" % (entry.get("submitted_at", "?"), entry.get("title", "?")),
             "    %s" % status,
             "    %s robots, %s s, main %s, %d file(s)%s" % (
                 entry.get("robots"), entry.get("duration"), entry.get("main"),
                 len(entry.get("files") or {}),
                 ", commit %s" % entry["git"] if entry.get("git") else "")]
    if entry.get("snapshot"):
        lines.append("    snapshot %s" % entry["snapshot"])
    return "\n".join(lines)
