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

#: What the site answers any address outside /api/ with: its single-page app, with HTTP 200.
APP_PAGE = (b"<!doctype html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n"
            b"  <title>Robotarium</title>\n  <base href=\"/\">\n</head>\n"
            b"<body>\n  <app-root></app-root>\n  <script src=\"main.js\"></script>\n</body>\n"
            b"</html>\n")


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
    #: Experiments by id, as GET experiment/<id> answers with them.
    experiments = {}
    #: Files by id: (name, content). Downloaded from files/preview/<type>/<id>.
    stored = {}
    #: Videos by path, served outside /api/ as the site may.
    videos = {}
    #: A path whose reply is cut off halfway through.
    truncate = None

    def log_message(self, *arguments):
        pass

    def _send_bytes(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.path == type(self).truncate:
            self.wfile.write(body[:len(body) // 2])
            self.close_connection = True
            return
        self.wfile.write(body)

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
        if self.path == "/api/files/id_list":
            if record["authorization"] != "Bearer jwt-token":
                return self._reply({"msg": "Missing Authorization Header"}, status=401)
            return self._reply([{"fileId": each, "name": type(self).stored[each][0],
                                 "uploadDate": "2026-10-22T14:24:26", "isMain": False}
                                for each in record["json"]["ids"] if each in type(self).stored])
        return self._reply({"message": "no such route"}, status=404)

    def do_GET(self):
        self._record()
        if self.path == "/api/settings/":
            return self._reply(type(self).settings)
        if self.path in type(self).videos:
            return self._send_bytes(type(self).videos[self.path], "video/mp4")
        if not self.path.startswith("/api/"):
            return self._send_bytes(APP_PAGE, "text/html; charset=utf-8")
        if self.headers.get("Authorization") != "Bearer jwt-token":
            return self._reply({"msg": "Missing Authorization Header"}, status=401)
        if self.path.startswith("/api/experiment/get-queue/"):
            return self._reply({"queue_position": 3, "queue_length": 7})
        if self.path.startswith("/api/experiment/"):
            experiment = type(self).experiments.get(self.path.rsplit("/", 1)[-1])
            if experiment is None:
                return self._reply({"message": "Experiment not found"}, status=404)
            return self._reply({"status": "success", "experiment": experiment})
        if self.path.startswith("/api/files/preview/"):
            file_id = self.path.rsplit("/", 1)[-1]
            if file_id in type(self).stored:
                return self._send_bytes(type(self).stored[file_id][1],
                                        "application/octet-stream")
        return self._reply({"message": "no such route"}, status=404)

    def do_PUT(self):
        self._record()
        return self._reply(type(self).mark_main_reply)


@pytest.fixture
def fake_site(monkeypatch):
    """A stand-in for the site on localhost, with ROBOTARIUM_API_URL pointed at it."""
    FakeRobotarium.requests = []
    FakeRobotarium.experiments, FakeRobotarium.stored, FakeRobotarium.videos = {}, {}, {}
    FakeRobotarium.truncate = None
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
