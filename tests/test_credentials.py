"""Where the login comes from, and that it is never shown."""

import os
import subprocess

import pytest

from robotarium import credentials
from robotarium.credentials import CredentialsError, resolve

from conftest import PASSWORD, USERNAME, write


def test_the_environment_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("ROBOTARIUM_USERNAME", USERNAME)
    monkeypatch.setenv("ROBOTARIUM_PASSWORD", PASSWORD)
    write(tmp_path / ".env", "ROBOTARIUM_USERNAME=other@example.edu\n")
    found = resolve(project_root=tmp_path, interactive=False)
    assert (found.username, found.password) == (USERNAME, PASSWORD)
    assert found.username_source == found.password_source == "the environment"


def test_a_dotenv_file_beside_the_manifest_is_read(tmp_path):
    project = tmp_path / "project"
    env = write(project / ".env", "# mine\nexport ROBOTARIUM_USERNAME='%s'\n"
                                  "ROBOTARIUM_PASSWORD=\"%s\"\n" % (USERNAME, PASSWORD))
    os.chmod(env, 0o600)
    found = resolve(project_root=project, interactive=False)
    assert (found.username, found.password) == (USERNAME, PASSWORD)
    assert found.password_source == str(env.resolve())
    assert found.warnings == ()


def test_sources_combine_field_by_field(monkeypatch, tmp_path):
    monkeypatch.setenv("ROBOTARIUM_USERNAME", USERNAME)
    write(tmp_path / ".env", "ROBOTARIUM_PASSWORD=%s\n" % PASSWORD)
    found = resolve(project_root=tmp_path, interactive=False)
    assert found.username_source == "the environment"
    assert found.password_source.endswith(".env")


def test_the_keyring_supplies_the_password(monkeypatch):
    monkeypatch.setenv("ROBOTARIUM_USERNAME", USERNAME)
    monkeypatch.setattr(credentials, "keyring_password",
                        lambda username: PASSWORD if username == USERNAME else None)
    found = resolve(interactive=False)
    assert found.password == PASSWORD and found.password_source == "the system keyring"


def test_an_explicit_env_file_must_exist(tmp_path):
    with pytest.raises(CredentialsError, match="does not exist"):
        resolve(env_file=tmp_path / "nope.env", interactive=False)


def test_missing_credentials_are_named_not_shown(monkeypatch):
    monkeypatch.setenv("ROBOTARIUM_USERNAME", USERNAME)
    with pytest.raises(CredentialsError, match="ROBOTARIUM_PASSWORD") as error:
        resolve(interactive=False)
    assert USERNAME not in str(error.value)


def test_a_prompt_fills_what_is_missing(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: USERNAME)
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)
    found = resolve(interactive=True)
    assert (found.username, found.password) == (USERNAME, PASSWORD)


def test_neither_value_appears_in_repr_or_str(monkeypatch):
    monkeypatch.setenv("ROBOTARIUM_USERNAME", USERNAME)
    monkeypatch.setenv("ROBOTARIUM_PASSWORD", PASSWORD)
    found = resolve(interactive=False)
    for text in (repr(found), str(found)):
        assert USERNAME not in text and PASSWORD not in text


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_a_file_others_can_read_is_warned_about(tmp_path):
    env = write(tmp_path / ".env", "ROBOTARIUM_USERNAME=%s\nROBOTARIUM_PASSWORD=%s\n"
                % (USERNAME, PASSWORD))
    os.chmod(env, 0o644)
    found = resolve(project_root=tmp_path, interactive=False)
    assert any("chmod 600" in each for each in found.warnings)


def test_a_dotenv_git_would_commit_is_warned_about(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    env = write(tmp_path / ".env", "ROBOTARIUM_USERNAME=%s\nROBOTARIUM_PASSWORD=%s\n"
                % (USERNAME, PASSWORD))
    os.chmod(env, 0o600)
    found = resolve(project_root=tmp_path, interactive=False)
    assert any("not ignored" in each for each in found.warnings)

    write(tmp_path / ".gitignore", ".env\n")
    assert resolve(project_root=tmp_path, interactive=False).warnings == ()


def test_login_to_a_file_is_private_and_logout_removes_it(tmp_path):
    where = credentials.store(USERNAME, PASSWORD, where="file")
    path = credentials.user_credentials_file()
    assert where == str(path) and path.is_file()
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
    found = resolve(interactive=False)
    assert (found.username, found.password) == (USERNAME, PASSWORD)

    assert credentials.forget() == [str(path)]
    assert not path.exists()


def test_login_to_the_keyring_keeps_the_password_out_of_the_file(monkeypatch):
    stored = {}

    class FakeKeyring:
        @staticmethod
        def set_password(service, username, password):
            stored[(service, username)] = password

    monkeypatch.setattr(credentials, "keyring_available", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "keyring", FakeKeyring)
    credentials.store(USERNAME, PASSWORD, where="keyring")
    assert stored == {(credentials.KEYRING_SERVICE, USERNAME): PASSWORD}
    assert PASSWORD not in credentials.user_credentials_file().read_text()


def test_an_email_is_masked():
    assert credentials.mask("hans@gmail.com") == "h***@g***.com"


@pytest.mark.parametrize("password", ['"quoted"', " spaced ", "hash # inside", "back\\slash",
                                      "it's", "#start", "new\nline"])
def test_any_password_survives_the_credentials_file(password):
    credentials.store(USERNAME, password, where="file")
    assert resolve(interactive=False).password == password


def test_dotenv_values_read_as_dotenv_does(tmp_path):
    env = write(tmp_path / "x.env", "A=x  # note\nB='single # kept'\nC=\"dq\" # c\nD=a#b\n")
    assert credentials.read_env_file(env) == {"A": "x", "B": "single # kept", "C": "dq",
                                              "D": "a#b"}
