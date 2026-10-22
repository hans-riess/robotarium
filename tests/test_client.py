"""The API client: what the form posts, the live limits, refusals, and what a video is."""

import io

import pytest

from robotarium.client import (RobotariumClient, RobotariumError, check_against_settings,
                               experiment_form, video_problem)

from conftest import APP_PAGE, PASSWORD, USERNAME


def test_the_form_sends_numbers_as_the_text_inputs_do():
    form = experiment_form("T", "D", 120, 5, ["a", "b"])
    assert form == {"title": "T", "estimatedDuration": "120", "description": "D",
                    "numRobots": "5", "demo": False, "scripts": ["a", "b"],
                    "status": "submitted", "exp_type": "ground"}


def test_a_draft_is_saved_rather_than_submitted():
    assert experiment_form("T", "D", 60, 1, [], draft=True)["status"] == "draft"


def test_the_live_limits_are_checked(tmp_path):
    settings = {"allowedFileExt": [".py", ".m"], "maxDuration": 60, "minRbt": 1, "maxRbt": 4}
    problems = check_against_settings(settings, 120, 5,
                                      [tmp_path / "main.py", tmp_path / "backend.jl"])
    assert len(problems) == 3
    assert any("backend.jl" in each for each in problems)
    assert any("120 s" in each for each in problems)
    assert any("5 robots" in each for each in problems)


def test_a_submission_within_the_limits_passes(tmp_path):
    settings = {"allowedFileExt": [".py"], "maxDuration": 600, "minRbt": 1, "maxRbt": 20}
    assert check_against_settings(settings, 120, 5, [tmp_path / "main.py"]) == []


def test_a_login_sends_the_password_encrypted_and_keeps_the_token(fake_site):
    client = RobotariumClient()
    client.login(USERNAME, PASSWORD)
    assert client.token == "jwt-token"
    assert PASSWORD.encode() not in fake_site[0]["raw"]
    client.settings()
    assert fake_site[1]["authorization"] == "Bearer jwt-token"


def test_a_refused_login_gives_the_sites_reason_and_not_the_password(fake_site):
    with pytest.raises(RobotariumError, match="Invalid credentials") as error:
        RobotariumClient().login(USERNAME, "wrong-password")
    assert "wrong-password" not in str(error.value)


def test_an_unreachable_site_is_named(monkeypatch):
    with pytest.raises(RobotariumError, match="Could not reach"):
        RobotariumClient("http://127.0.0.1:9/api/").settings()



def test_a_password_that_is_not_text_is_refused_without_quoting_it(fake_site):
    with pytest.raises(RobotariumError, match="not valid text") as error:
        RobotariumClient().login(USERNAME, "zqj\udcffxvk")
    assert "zqj" not in str(error.value) and "xvk" not in str(error.value) and fake_site == []


MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"


@pytest.mark.parametrize("content_type, head", [
    ("video/mp4", MP4),
    ("application/octet-stream", MP4),
    ("binary/octet-stream", MP4),
    ("", MP4),
    ("video/webm", b"\x1aE\xdf\xa3\x9fB\x86\x81\x01"),
    ("application/octet-stream; charset=binary", b"\x00\x00\x01\xbaD\x00\x04"),
])
def test_a_video_is_known_by_its_bytes_or_its_type(content_type, head):
    assert video_problem(content_type, head) is None


@pytest.mark.parametrize("content_type, head, said", [
    ("text/html; charset=utf-8", APP_PAGE, "a web page (text/html), not a video"),
    ("application/octet-stream", APP_PAGE, "a web page (application/octet-stream)"),
    ("video/mp4", b"\xef\xbb\xbf  <!DOCTYPE html><html>", "a web page (video/mp4)"),
    ("application/json", b'{"message": "Not found"}', "something else (application/json)"),
    ("video/mp4", b"", "an empty reply (video/mp4)"),
    ("", b"\x00\x01\x02\x03\x04\x05\x06\x07", "something else (no Content-Type)"),
])
def test_anything_else_is_not_a_video(content_type, head, said):
    assert said in video_problem(content_type, head)


def test_a_video_that_comes_back_as_a_web_page_writes_nothing(fake_site):
    client = RobotariumClient()
    client.login(USERNAME, PASSWORD)
    sink = io.BytesIO()
    with pytest.raises(RobotariumError, match=r"answered HTTP 200 with a web page \(text/html\), "
                                              r"not a video") as error:
        client.download_video("/experiment/exp-1", sink)
    assert sink.getvalue() == b"" and error.value.status == 200 and not error.value.uncertain
    assert fake_site[-1]["path"] == "/experiment/exp-1/download"
    assert fake_site[-1]["authorization"] is None
