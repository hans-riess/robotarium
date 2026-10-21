"""
Shared fixtures. Nothing in the suite reaches the real Robotarium: `fake_site` is a local
stand-in that answers the way the site's API does and records what it was sent.
"""

import email.parser
import email.policy
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from robotarium import scaffold
from robotarium._cryptojs import encrypt_password

USERNAME = "someone@example.edu"
PASSWORD = "not-a-real-password"

HAS_SIMULATOR = importlib.util.find_spec("rps") is not None and \
    importlib.util.find_spec("cvxopt") is not None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No test sees this machine's credentials, keyring, or per-user files."""
    for name in ("ROBOTARIUM_USERNAME", "ROBOTARIUM_PASSWORD", "ROBOTARIUM_ENV_FILE",
                 "ROBOTARIUM_API_URL", "ROBOTARIUM_SIMULATOR", "ROBOTARIUM_LOG_DIR",
                 "ROBOTARIUM_JULIA_REGISTRY", "APPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("robotarium.credentials.keyring_password", lambda username: None)
    monkeypatch.setattr("robotarium.credentials.keyring_available", lambda: False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def project_dir(tmp_path):
    """A freshly scaffolded four-robot experiment, short enough to simulate quickly."""
    directory = tmp_path / "circle"
    scaffold.create(directory, robots=4, title="Circle")
    manifest = directory / "robotarium.toml"
    text = manifest.read_text(encoding="utf-8").replace("iterations = 1500", "iterations = 150")
    manifest.write_text(text, encoding="utf-8")
    return directory


class FakeRobotarium(BaseHTTPRequestHandler):
    """Answers the calls a submission makes the way the site's API does, and records them."""

    requests = []
    settings = {"allowedFileExt": [".py", ".jl", ".m", ".npy"], "maxDuration": 600,
                "minRbt": 1, "maxRbt": 20}
    #: A path to answer with nothing at all, closing the connection: a dropped request.
    drop = None
    #: A reply to give to the main-file PUT instead of success.
    mark_main_reply = {"message": "updated"}
    #: A reply to give to experiment/create instead of success.
    create_reply = {"experimentId": "exp-1", "status": "success", "message": "Experiment created"}

    def log_message(self, *arguments):
        pass

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        record = {"method": self.command, "path": self.path, "raw": raw,
                  "authorization": self.headers.get("Authorization")}
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            record["json"] = json.loads(raw)
        elif content_type.startswith("multipart/form-data"):
            message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw)
            part = next(message.iter_parts())
            assert part.get_param("name", header="content-disposition") == "file"
            record["filename"] = part.get_filename()
            record["content"] = part.get_payload(decode=True)
        type(self).requests.append(record)
        return record

    def do_POST(self):
        record = self._record()
        if self.path == type(self).drop:
            self.close_connection = True
            return
        if self.path == "/api/auth/login":
            if record["json"] == {"email": USERNAME, "password": encrypt_password(PASSWORD)}:
                return self._reply({"token": "jwt-token", "username": "someone"})
            return self._reply({"message": "Invalid credentials"})
        if self.path == "/api/files/upload":
            return self._reply({"file_id": "file-%s" % record["filename"]})
        if self.path == "/api/experiment/create":
            return self._reply(type(self).create_reply)
        return self._reply({"message": "no such route"}, status=404)

    def do_GET(self):
        self._record()
        if self.path == "/api/settings/":
            return self._reply(type(self).settings)
        return self._reply({"message": "no such route"}, status=404)

    def do_PUT(self):
        self._record()
        return self._reply(type(self).mark_main_reply)


@pytest.fixture
def fake_site(monkeypatch):
    """A stand-in for the site on localhost, with ROBOTARIUM_API_URL pointed at it."""
    FakeRobotarium.requests = []
    server = HTTPServer(("127.0.0.1", 0), FakeRobotarium)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ROBOTARIUM_API_URL", "http://127.0.0.1:%d/api/" % server.server_port)
    yield FakeRobotarium.requests
    server.shutdown()
    server.server_close()


@pytest.fixture
def logged_in(monkeypatch):
    """Credentials in the environment, as CI would provide them."""
    monkeypatch.setenv("ROBOTARIUM_USERNAME", USERNAME)
    monkeypatch.setenv("ROBOTARIUM_PASSWORD", PASSWORD)


@pytest.fixture
def fresh_runtime():
    """robotarium_runtime with its caches and any imported robotarium_bundle cleared."""
    import robotarium_runtime
    robotarium_runtime._DECLARED.clear()
    robotarium_runtime._BACKENDS.clear()
    robotarium_runtime._CLAIMED.clear()
    sys.modules.pop("robotarium_bundle", None)
    yield robotarium_runtime
    robotarium_runtime._DECLARED.clear()
    robotarium_runtime._BACKENDS.clear()
    sys.modules.pop("robotarium_bundle", None)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
