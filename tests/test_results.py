"""
Fetching what comes back from the testbed, against a stand-in for the site: the record, the
return messages, the video, the log and data files, where they land, and what is not sent.
"""

import json
import os
import urllib.request
from pathlib import Path

import pytest

from robotarium import cli, ledger
from robotarium.client import RobotariumClient, RobotariumError
from robotarium.results import FetchResult, describe, experiment_id_from, fetch

from conftest import APP_PAGE, PASSWORD, USERNAME, FakeRobotarium


def site_root():
    return os.environ["ROBOTARIUM_API_URL"].rsplit("/api/", 1)[0]


@pytest.fixture
def completed(fake_site):
    """A finished experiment with a video, a log file, two data files and a return message."""
    FakeRobotarium.stored = {
        "log-1": ("log.txt", b"step 0\nstep 1\n"),
        "data-1": ("poses.npy", b"\x93NUMPY fake"),
        "data-2": ("run_20261022.npz", b"PK fake"),
        "script-1": ("main.py", b"print('hi')\n"),
    }
    FakeRobotarium.videos = {"/videos/exp-1/download": b"\x00\x00\x00\x18ftypmp42" * 100}
    FakeRobotarium.experiments = {"exp-1": {
        "title": "Edge potential", "status": "completed", "numRobots": "9",
        "createdAt": "Thu, 22 Oct 2026 02:29:15 GMT", "finishedAt": "Thu, 22 Oct 2026 14:24:26 GMT",
        "videoUrls": site_root() + "/videos/exp-1",
        "returnMessages": "Traceback (most recent call last):\n  ...\nValueError: boom",
        "scripts": ["script-1"], "logFiles": ["log-1"], "dataFiles": ["data-1", "data-2"],
    }}
    return fake_site


def logged_in_client():
    client = RobotariumClient()
    client.login(USERNAME, PASSWORD)
    return client


def test_every_section_comes_back_as_files(completed, tmp_path):
    result = fetch("exp-1", tmp_path / "out", client=logged_in_client(), out=lambda *_: None)
    out = tmp_path / "out"
    assert result.status == "completed" and result.finished
    assert json.loads((out / "experiment.json").read_text())["title"] == "Edge potential"
    assert (out / "return_messages.txt").read_text().endswith("ValueError: boom\n")
    assert (out / "video.mp4").read_bytes() == FakeRobotarium.videos["/videos/exp-1/download"]
    assert (out / "logs" / "log.txt").read_bytes() == b"step 0\nstep 1\n"
    assert (out / "data" / "poses.npy").read_bytes() == b"\x93NUMPY fake"
    assert (out / "data" / "run_20261022.npz").exists()
    assert not (out / "scripts").exists()
    assert result.missing == []
    assert not list(out.rglob("*.part"))


def test_the_api_calls_carry_the_token_and_the_video_does_not(completed, tmp_path):
    fetch("exp-1", tmp_path, client=logged_in_client(), out=lambda *_: None)
    calls = {each["path"]: each["authorization"] for each in completed[1:]}
    assert calls["/api/experiment/exp-1"] == "Bearer jwt-token"
    assert calls["/api/files/preview/logFiles/log-1"] == "Bearer jwt-token"
    assert calls["/api/files/preview/dataFiles/data-2"] == "Bearer jwt-token"
    assert calls["/videos/exp-1/download"] is None
    lists = [each["json"] for each in completed if each["path"] == "/api/files/id_list"]
    assert lists == [{"ids": ["log-1"], "type": "logFiles"},
                     {"ids": ["data-1", "data-2"], "type": "dataFiles"}]


def test_scripts_and_no_video_on_request(completed, tmp_path):
    fetch("exp-1", tmp_path, client=logged_in_client(), video=False, scripts=True,
          out=lambda *_: None)
    assert (tmp_path / "scripts" / "main.py").read_bytes() == b"print('hi')\n"
    assert not (tmp_path / "video.mp4").exists()


def test_fetching_again_keeps_what_is_there_unless_forced(completed, tmp_path):
    client = logged_in_client()
    fetch("exp-1", tmp_path, client=client, out=lambda *_: None)
    (tmp_path / "logs" / "log.txt").write_text("mine")
    again = fetch("exp-1", tmp_path, client=client, out=lambda *_: None)
    assert tmp_path / "logs" / "log.txt" in again.kept
    assert (tmp_path / "logs" / "log.txt").read_text() == "mine"
    fetch("exp-1", tmp_path, client=client, force=True, out=lambda *_: None)
    assert (tmp_path / "logs" / "log.txt").read_bytes() == b"step 0\nstep 1\n"


def test_an_unfinished_experiment_says_where_it_stands(fake_site, tmp_path):
    FakeRobotarium.experiments = {"exp-2": {"title": "Queued", "status": "submitted",
                                            "scripts": [], "logFiles": [], "dataFiles": []}}
    result = fetch("exp-2", tmp_path, client=logged_in_client(), out=lambda *_: None)
    assert not result.finished
    assert result.queue == {"queue_position": 3, "queue_length": 7}
    assert result.missing == ["return messages", "video", "log files", "data files"]
    text = describe(result)
    assert "number 3 of 7 in the queue" in text
    assert "not available yet: return messages, video, log files, data files" in text
    assert "fetch again once it has run" in text
    assert (tmp_path / "experiment.json").exists()


def test_a_file_name_cannot_leave_its_folder(fake_site, tmp_path):
    FakeRobotarium.stored = {"a": ("../../escape.txt", b"1"), "b": ("..", b"2"),
                             "c": ("escape.txt", b"3")}
    FakeRobotarium.experiments = {"exp-3": {"status": "completed", "dataFiles": ["a", "b", "c"],
                                            "logFiles": []}}
    fetch("exp-3", tmp_path / "out", client=logged_in_client(), out=lambda *_: None)
    written = sorted(path.name for path in (tmp_path / "out" / "data").iterdir())
    assert written == ["b", "c-escape.txt", "escape.txt"]
    assert not (tmp_path / "escape.txt").exists()


def test_a_cut_off_download_leaves_no_file(completed, tmp_path):
    FakeRobotarium.truncate = "/api/files/preview/logFiles/log-1"
    result = fetch("exp-1", tmp_path, client=logged_in_client(), video=False,
                   out=lambda *_: None)
    [(what, reason)] = result.failed
    assert what == tmp_path / "logs" / "log.txt" and "was cut off" in reason
    assert not (tmp_path / "logs" / "log.txt").exists()
    assert not list(tmp_path.rglob("*.part"))
    assert (tmp_path / "data" / "poses.npy").exists()


@pytest.mark.parametrize("urls", ["-", "", None, ["-", ""], [None, " - "]])
def test_a_run_without_a_video_says_so_and_fetches_the_rest(completed, tmp_path, urls):
    FakeRobotarium.experiments["exp-1"]["videoUrls"] = urls
    lines = []
    result = fetch("exp-1", tmp_path, client=logged_in_client(), out=lines.append)
    assert "  no video: the site lists none" in lines
    assert result.failed == [] and result.missing == ["video"]
    assert not (tmp_path / "video.mp4").exists()
    assert not [each for each in completed if each["path"].endswith("/download")]
    assert (tmp_path / "return_messages.txt").read_text().endswith("ValueError: boom\n")
    assert (tmp_path / "logs" / "log.txt").read_bytes() == b"step 0\nstep 1\n"
    assert (tmp_path / "data" / "poses.npy").exists()
    assert (tmp_path / "data" / "run_20261022.npz").exists()
    assert "nothing came back for: video" in describe(result)


def test_the_command_succeeds_for_a_run_without_a_video(completed, logged_in, tmp_path,
                                                         capsys):
    FakeRobotarium.experiments["exp-1"]["videoUrls"] = "-"
    assert cli.main(["fetch", "exp-1", "-o", str(tmp_path / "here")]) == 0
    captured = capsys.readouterr()
    assert "no video" in captured.out and captured.err == ""
    assert (tmp_path / "here" / "logs" / "log.txt").exists()


def test_an_experiment_that_has_not_run_saves_no_web_page_as_its_video(fake_site, tmp_path):
    FakeRobotarium.experiments = {"exp-4": {"title": "Queued", "status": "submitted",
                                            "videoUrls": "", "scripts": ["script-1"],
                                            "logFiles": [], "dataFiles": []}}
    # As on the site: what "" made fetch ask for answers with the app's page and HTTP 200.
    with urllib.request.urlopen(site_root() + "/download") as response:
        assert response.status == 200 and response.read() == APP_PAGE
    lines = []
    result = fetch("exp-4", tmp_path, client=logged_in_client(), out=lines.append)
    assert "  no video: the site lists none yet" in lines
    assert result.failed == [] and "video" in result.missing
    assert not (tmp_path / "video.mp4").exists()
    assert not list(tmp_path.rglob("*.part"))
    assert not [each for each in fake_site[1:] if each["path"].endswith("/download")]


def test_a_web_page_where_the_video_should_be_is_not_saved_as_one(completed, tmp_path):
    FakeRobotarium.experiments["exp-1"]["videoUrls"] = site_root() + "/experiment/exp-1"
    result = fetch("exp-1", tmp_path, client=logged_in_client(), out=lambda *_: None)
    [(what, reason)] = result.failed
    assert what == tmp_path / "video.mp4"
    assert "answered HTTP 200 with a web page (text/html), not a video" in reason
    assert not (tmp_path / "video.mp4").exists()
    assert not list(tmp_path.rglob("*.part"))
    assert (tmp_path / "logs" / "log.txt").exists() and (tmp_path / "data" / "poses.npy").exists()


def test_a_failed_video_does_not_stop_the_rest(completed, logged_in, tmp_path, capsys):
    FakeRobotarium.experiments["exp-1"]["videoUrls"] = site_root() + "/api/videos/gone"
    assert cli.main(["fetch", "exp-1", "-o", str(tmp_path / "here")]) == 1
    captured = capsys.readouterr()
    here = tmp_path / "here"
    assert not (here / "video.mp4").exists()
    assert (here / "return_messages.txt").exists()
    assert (here / "logs" / "log.txt").read_bytes() == b"step 0\nstep 1\n"
    assert (here / "data" / "poses.npy").exists()
    assert (here / "data" / "run_20261022.npz").exists()
    assert not list(here.rglob("*.part"))
    assert ("failed  %s: GET videos/gone/download answered HTTP 404" % (here / "video.mp4")
            in captured.out)
    assert "wrote   %s" % (here / "logs" / "log.txt") in captured.out
    assert "1 download(s) failed" in captured.err


def test_a_section_that_cannot_be_listed_does_not_stop_the_rest(completed, tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(FakeRobotarium, "drop", "/api/files/id_list")
    result = fetch("exp-1", tmp_path, client=logged_in_client(), out=lambda *_: None)
    assert [what for what, _ in result.failed] == ["log files", "data files"]
    assert (tmp_path / "video.mp4").exists() and (tmp_path / "return_messages.txt").exists()


def test_a_web_page_saved_as_the_video_by_an_earlier_fetch_is_replaced(completed, tmp_path):
    (tmp_path / "video.mp4").write_bytes(APP_PAGE)
    client = logged_in_client()
    result = fetch("exp-1", tmp_path, client=client, out=lambda *_: None)
    assert (tmp_path / "video.mp4").read_bytes() == FakeRobotarium.videos["/videos/exp-1/download"]
    assert tmp_path / "video.mp4" in result.written
    again = fetch("exp-1", tmp_path, client=client, out=lambda *_: None)
    assert tmp_path / "video.mp4" in again.kept


def test_an_experiment_queued_again_says_its_earlier_files_are_gone(fake_site, tmp_path):
    FakeRobotarium.experiments = {"exp-5": {
        "title": "Again", "status": "submitted", "videoUrls": "",
        "createdAt": "Wed, 21 Oct 2026 22:28:00 GMT",
        "finishedAt": "Thu, 22 Oct 2026 14:24:26 GMT",
        "lastUpdatedAt": "Thu, 22 Oct 2026 23:05:12 GMT",
        "scripts": ["script-1"], "logFiles": [], "dataFiles": []}}
    result = fetch("exp-5", tmp_path, client=logged_in_client(), out=lambda *_: None)
    assert result.requeued and not result.finished
    text = describe(result)
    assert "has been queued again; files from its earlier run are no longer listed" in text
    assert "finished   Thu, 22 Oct 2026 14:24:26 GMT (its earlier run)" in text
    assert "It has not finished" not in text


EARLIER, LATER = "Thu, 22 Oct 2026 14:24:26 GMT", "Thu, 22 Oct 2026 23:05:12 GMT"


@pytest.mark.parametrize("record, requeued", [
    ({"status": "submitted"}, False),
    ({"status": "submitted", "finishedAt": EARLIER, "lastUpdatedAt": LATER}, True),
    ({"status": "submitted", "finishedAt": "2026-10-22T14:24:26",
      "lastUpdatedAt": "2026-10-22T23:05:12"}, True),
    ({"status": "submitted", "finishedAt": EARLIER}, True),
    ({"status": "submitted", "finishedAt": LATER, "lastUpdatedAt": EARLIER}, False),
    ({"status": "completed", "finishedAt": EARLIER, "lastUpdatedAt": LATER}, False),
    ({"status": "draft", "finishedAt": EARLIER, "lastUpdatedAt": LATER}, False),
])
def test_queued_again_is_told_from_the_status_and_dates(record, requeued):
    assert FetchResult("x", record, Path(".")).requeued is requeued


def test_an_unknown_experiment_gives_the_sites_reason(fake_site, tmp_path):
    with pytest.raises(RobotariumError, match="Experiment not found"):
        fetch("nope", tmp_path, client=logged_in_client(), out=lambda *_: None)


@pytest.mark.parametrize("text", [
    "6716f0a1b2", " 6716f0a1b2 ", "https://www.robotarium.gatech.edu/experiment/6716f0a1b2",
    "https://www.robotarium.gatech.edu/experiment/6716f0a1b2/",
    "www.robotarium.gatech.edu/experiment/6716f0a1b2?tab=video",
])
def test_an_experiment_is_named_by_id_or_by_its_page(text):
    assert experiment_id_from(text) == "6716f0a1b2"


def test_a_page_that_is_not_an_experiments_is_refused():
    with pytest.raises(ValueError, match="not an experiment's page"):
        experiment_id_from("https://www.robotarium.gatech.edu/dashboard")


def test_the_command_fetches_the_last_submission(completed, logged_in, tmp_path, capsys):
    project = tmp_path / "exp"
    assert cli.main(["init", str(project)]) == 0
    ledger.record(project / ".robotarium", {"outcome": "failed", "title": "Older"})
    ledger.record(project / ".robotarium", {"outcome": "submitted", "experiment_id": "exp-1",
                                            "title": "Edge potential",
                                            "submitted_at": "2026-10-21T22:28:00"})
    capsys.readouterr()
    assert cli.main(["fetch", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    results = project / ".robotarium" / "results" / "exp-1"
    assert (results / "video.mp4").exists() and (results / "logs" / "log.txt").exists()
    assert "The last experiment submitted from here: Edge potential" in out
    assert "experiment exp-1: completed" in out
    assert "ValueError: boom" in out
    assert USERNAME not in out and PASSWORD not in out


def test_the_command_takes_a_page_address_and_a_directory(completed, logged_in, tmp_path):
    page = "https://www.robotarium.gatech.edu/experiment/exp-1"
    assert cli.main(["fetch", page, "-o", str(tmp_path / "here"), "--no-video"]) == 0
    assert (tmp_path / "here" / "data" / "poses.npy").exists()
    assert not (tmp_path / "here" / "video.mp4").exists()


def test_the_command_without_an_experiment_or_a_ledger_says_so(fake_site, logged_in, tmp_path,
                                                               capsys):
    assert cli.main(["fetch"]) == 1
    assert "No experiment given, and none submitted from here" in capsys.readouterr().err
    assert fake_site == []
