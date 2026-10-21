"""The API client: what the form posts, the live limits, and refusals."""

import pytest

from robotarium.client import (RobotariumClient, RobotariumError, check_against_settings,
                               experiment_form)

from conftest import PASSWORD, USERNAME


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
