"""
Fetching what comes back from the testbed, against a stand-in for the site: the record, the
return messages, the video, the log and data files, where they land, and what is not sent.
"""

import json
import os

import pytest

from robotarium import cli, ledger
from robotarium.client import ConnectionLost, RobotariumClient, RobotariumError
from robotarium.results import describe, experiment_id_from, fetch

from conftest import PASSWORD, USERNAME, FakeRobotarium


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
    with pytest.raises(ConnectionLost):
        fetch("exp-1", tmp_path, client=logged_in_client(), video=False, out=lambda *_: None)
    assert not (tmp_path / "logs" / "log.txt").exists()
    assert not list(tmp_path.rglob("*.part"))


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
