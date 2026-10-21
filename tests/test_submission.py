"""
Submitting, start to finish, against a stand-in for the site: that the plan is shown before
anything is sent, that nothing is sent without a yes, that the form carries the experiment's
values, that the main file is the one marked, that the attempt is recorded, and that the
credentials are never printed.
"""

import json

import pytest

from robotarium import ledger
from robotarium.client import ConnectionLost, RobotariumClient, RobotariumError
from robotarium.experiment import Experiment, Project
from robotarium.submission import SubmissionRefused, submit

from conftest import PASSWORD, USERNAME, FakeRobotarium, write

MAIN = """\
import rps.robotarium as robotarium
from robotarium_runtime import config

N = 3
r = robotarium.Robotarium(number_of_robots=N)
r.debug()
"""


@pytest.fixture
def project(tmp_path):
    write(tmp_path / "exp" / "main.py", MAIN)
    write(tmp_path / "exp" / "helpers.py", "")
    experiment = Experiment(title="Run", description="Three robots.", duration="2m", robots=3,
                            main="main.py", files=["helpers.py"], root=tmp_path / "exp",
                            config={"gain": 2})
    return Project(experiment=experiment)


def test_a_yes_uploads_every_file_and_marks_the_main_one(project, fake_site, logged_in):
    result = submit(project, confirm=lambda plan: True, out=lambda *_: None)
    assert result.sent and result.experiment_id == "exp-1"

    names = ["main.py", "helpers.py", "robotarium_runtime.py", "robotarium_bundle.py"]
    assert [each["path"] for each in fake_site] == \
        ["/api/auth/login", "/api/settings/"] + ["/api/files/upload"] * 4 + \
        ["/api/experiment/create", "/api/files/update_metadata/file-main.py"]
    uploads = [each for each in fake_site if each["path"] == "/api/files/upload"]
    assert [each["filename"] for each in uploads] == names
    assert uploads[0]["content"] == (result.prepared.bundle.directory / "main.py").read_bytes()

    create = next(each["json"] for each in fake_site if each["path"].endswith("create"))
    assert create == {"title": "Run", "estimatedDuration": "120", "description": "Three robots.",
                      "numRobots": "3", "demo": False, "status": "submitted",
                      "exp_type": "ground", "scripts": ["file-%s" % name for name in names]}

    metadata = fake_site[-1]["json"]["metadata"]
    assert {each["fileId"]: each["isMain"] for each in metadata} == \
        {"file-%s" % name: name == "main.py" for name in names}
    assert all(each["experimentId"] == "exp-1" for each in metadata)
    assert fake_site[0]["authorization"] is None
    assert all(each["authorization"] == "Bearer jwt-token" for each in fake_site[1:])


def test_the_attempt_is_recorded_with_a_snapshot(project, fake_site, logged_in):
    result = submit(project, confirm=False, out=lambda *_: None)
    entries = ledger.read(project.output_dir)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["outcome"] == "submitted" and entry["experiment_id"] == "exp-1"
    assert entry["files"] == result.prepared.bundle.hashes()
    assert (project.output_dir / entry["snapshot"]).is_file()
    assert USERNAME not in json.dumps(entry) and PASSWORD not in json.dumps(entry)


def test_the_plan_is_shown_before_anything_is_sent(project, fake_site, logged_in):
    seen = []
    printed = []
    submit(project, confirm=lambda plan: seen.append(len(fake_site)) or False,
           out=printed.append)
    text = "\n".join(printed)
    assert seen == [0] and fake_site == []
    assert "MAIN FILE" in text and "helpers.py" in text and "Estimated Duration   120 s" in text
    assert "Nothing sent." in text


@pytest.mark.parametrize("answer", ["n", "", "nope", EOFError])
def test_anything_but_yes_sends_nothing(answer, project, fake_site, logged_in, monkeypatch):
    def respond(prompt):
        if answer is EOFError:
            raise EOFError
        return answer
    monkeypatch.setattr("builtins.input", respond)
    result = submit(project, out=lambda *_: None)
    assert not result.sent and fake_site == []


def test_a_dry_run_needs_no_credentials_and_sends_nothing(project, fake_site):
    result = submit(project, dry_run=True, out=lambda *_: None)
    assert not result.sent and fake_site == []


def test_the_credentials_are_never_printed_or_sent_in_the_clear(project, fake_site, logged_in,
                                                                capsys):
    submit(project, confirm=False)
    printed = capsys.readouterr()
    for secret in (USERNAME, PASSWORD):
        assert secret not in printed.out and secret not in printed.err
    assert all(PASSWORD.encode() not in each["raw"] for each in fake_site)


def test_a_draft_is_saved_not_queued(project, fake_site, logged_in):
    submit(project, draft=True, confirm=False, out=lambda *_: None)
    create = next(each["json"] for each in fake_site if each["path"].endswith("create"))
    assert create["status"] == "draft"


def test_errors_refuse_before_anything_is_sent(project, fake_site, logged_in):
    project.experiment.robots = 5
    with pytest.raises(SubmissionRefused, match="number_of_robots=3"):
        submit(project, confirm=False, out=lambda *_: None)
    assert fake_site == []


def test_strict_refuses_on_warnings(project, fake_site, logged_in):
    write(project.experiment.root / "main.py", MAIN + "import pandas\n")
    with pytest.raises(SubmissionRefused, match="pandas"):
        submit(project, confirm=False, strict=True, out=lambda *_: None)
    assert fake_site == []


def test_a_refused_login_uploads_nothing_and_is_recorded(project, fake_site, logged_in,
                                                         monkeypatch):
    monkeypatch.setenv("ROBOTARIUM_PASSWORD", "wrong")
    with pytest.raises(Exception, match="Invalid credentials"):
        submit(project, confirm=False, out=lambda *_: None)
    assert [each["path"] for each in fake_site] == ["/api/auth/login"]
    entry = ledger.read(project.output_dir)[-1]
    assert entry["outcome"] == "failed" and "Invalid credentials" in entry["error"]


def test_the_sites_live_limits_refuse_before_uploading(project, fake_site, logged_in,
                                                       monkeypatch):
    monkeypatch.setattr(FakeRobotarium, "settings", {"allowedFileExt": [".py"],
                                                     "maxDuration": 60, "minRbt": 1,
                                                     "maxRbt": 20})
    with pytest.raises(Exception, match="over the site's maximum of 60"):
        submit(project, confirm=False, out=lambda *_: None)
    assert "/api/files/upload" not in [each["path"] for each in fake_site]


def test_auto_without_a_simulation_is_refused(project, fake_site, logged_in):
    project.experiment.duration = "auto"
    with pytest.raises(SubmissionRefused, match="only a simulation"):
        submit(project, simulate_first=False, confirm=False, out=lambda *_: None)


def test_a_matlab_main_file_is_not_simulated(tmp_path, fake_site, logged_in):
    write(tmp_path / "main.m", "r = Robotarium('NumberOfRobots', 2);\nr.debug();\n")
    experiment = Experiment(title="M", description="MATLAB.", duration=60, robots=2,
                            main="main.m", root=tmp_path)
    with pytest.raises(SubmissionRefused, match="Only a Python main file"):
        submit(experiment, simulate_first=True, confirm=False, out=lambda *_: None)
    result = submit(experiment, confirm=False, out=lambda *_: None)
    uploads = [each["filename"] for each in fake_site if each["path"] == "/api/files/upload"]
    assert result.sent and uploads == ["main.m"]


# ---------------------------------------------------------------------------
# failures part-way through: always recorded, and what they leave on the site named
# ---------------------------------------------------------------------------

def test_a_dropped_create_is_recorded_as_possibly_created(project, fake_site, logged_in,
                                                          monkeypatch):
    monkeypatch.setattr(FakeRobotarium, "drop", "/api/experiment/create")
    with pytest.raises(RobotariumError, match="may have been created") as error:
        submit(project, confirm=False, out=lambda *_: None)
    assert isinstance(error.value, ConnectionLost)
    entry = ledger.read(project.output_dir)[-1]
    assert (entry["outcome"], entry["stage"]) == ("failed", "create")
    assert "check" in entry["consequence"] and "got no answer" in entry["error"]


def test_a_refused_main_file_mark_names_the_created_experiment(project, fake_site, logged_in,
                                                               monkeypatch):
    monkeypatch.setattr(FakeRobotarium, "mark_main_reply",
                        {"status": "error", "message": "no such file"})
    with pytest.raises(RobotariumError, match="Experiment exp-1 was created"):
        submit(project, confirm=False, out=lambda *_: None)
    entry = ledger.read(project.output_dir)[-1]
    assert (entry["stage"], entry["experiment_id"]) == ("mark-main", "exp-1")


def test_an_interrupted_upload_is_recorded(project, fake_site, logged_in, monkeypatch):
    real_upload = RobotariumClient.upload_file
    calls = []

    def upload(self, path):
        calls.append(path)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_upload(self, path)

    monkeypatch.setattr(RobotariumClient, "upload_file", upload)
    printed = []
    with pytest.raises(KeyboardInterrupt):
        submit(project, confirm=False, out=printed.append)
    entry = ledger.read(project.output_dir)[-1]
    assert (entry["outcome"], entry["stage"], entry["orphaned_uploads"]) == \
        ("interrupted", "upload", 1)
    assert any("attached to no experiment" in line for line in printed)
