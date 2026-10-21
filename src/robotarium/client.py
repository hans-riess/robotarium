"""
Talking to the Robotarium's website: logging in, uploading files, and creating the experiment with
one of them marked as the main file.

The site is an Angular application over a JSON API at https://www.robotarium.gatech.edu/api/.
What follows is the API as the site's own JavaScript calls it (read from its main bundle, not
guessed), in the order a person clicking through "New Experiment" causes:

    POST auth/login                 {"email", "password"} -> {"token", ...}. The password is
                                    AES-encrypted in the browser first; see _cryptojs.py.
                                    Every later call sends "Authorization: Bearer <token>".
    GET  settings/                  {"allowedFileExt", "maxDuration", "minRbt", "maxRbt"}: the
                                    limits the form validates against.
    POST files/upload               multipart, one file in the field "file" -> {"file_id"}.
                                    Once per file; dropping two files on the form makes two calls.
    POST experiment/create          the form's values -- title, estimatedDuration, description,
                                    numRobots, demo, scripts (the file ids), status, exp_type --
                                    -> {"experimentId", "status", "message"}.
    PUT  files/update_metadata/<id> {"metadata": [{"fileId", "experimentId", "isMain"}, ...]},
                                    sent for the main file's id: this is what ticking
                                    "Main File" amounts to.

The API is not documented or versioned by the Robotarium team, so it can change under this
module. docs/site-api.md says how it was read and how to re-read it.

Standard library only: urllib honours HTTPS_PROXY and the system's certificate store on its own.
"""

import http.client
import json
import logging
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ._cryptojs import encrypt_password
from .limits import EXPERIMENT_TYPE

#: The API root. ROBOTARIUM_API_URL overrides it, for a stand-in site under test or if the
#: Robotarium ever moves its API.
API_URL = "https://www.robotarium.gatech.edu/api/"

#: Where a submitted experiment can be seen.
DASHBOARD_URL = "https://www.robotarium.gatech.edu/dashboard"

#: How long to wait on one request: for a stalled connection, not a slow one.
TIMEOUT_SECONDS = 60

log = logging.getLogger(__name__)


class RobotariumError(RuntimeError):
    """The site refused a request, with the reason it gave. Never carries the credentials."""


class ConnectionLost(RobotariumError):
    """
    A request was sent and no answer came back: the connection dropped or timed out. Unlike a
    refusal, the site may have acted on it -- an experiment may exist although create failed.
    """


def api_url() -> str:
    """The API root this process talks to: ROBOTARIUM_API_URL if set, else API_URL."""
    url = os.environ.get("ROBOTARIUM_API_URL") or API_URL
    return url if url.endswith("/") else url + "/"


class RobotariumClient:
    """
    One logged-in conversation with the site. Holds the token, never the password.

        client = RobotariumClient()
        client.login(credentials.username, credentials.password)
        file_ids = [client.upload_file(path) for path in paths]
        experiment_id = client.create_experiment(experiment_form(...))
        client.mark_main_file(file_ids[0], file_ids, experiment_id)

    Most callers want submission.submit(), which does all of the above with the checks around it.

    Args:
        url (str): The API root, with its trailing slash; default api_url().
    """

    def __init__(self, url: str = None):
        self.url = url or api_url()
        self.token = None

    def login(self, email: str, password: str) -> None:
        """
        Logs in as the login page does, keeping the token for every later call.

        Args:
            email (str): The account's email address; the site logs in by email.
            password (str): The password in the clear; it leaves here encrypted, as the page
                sends it.
        """
        reply = self._request("POST", "auth/login",
                              body={"email": email, "password": encrypt_password(password)})
        if not reply.get("token"):
            raise RobotariumError("Login refused: %s" % _reason(reply, "no token in the reply"))
        self.token = reply["token"]

    def settings(self) -> dict:
        """The limits the form validates against: allowedFileExt, maxDuration, minRbt, maxRbt."""
        return self._request("GET", "settings/")

    def upload_file(self, path: Path) -> str:
        """
        Uploads one file, as dropping it on the form does, and returns the id the site gave it.

        Args:
            path (Path): The file to upload. It arrives under its base name.
        """
        path = Path(path)
        reply = self._request("POST", "files/upload", files={"file": path})
        if not reply.get("file_id"):
            raise RobotariumError("Upload of %s refused: %s"
                                  % (path.name, _reason(reply, "no file_id in the reply")))
        return reply["file_id"]

    def create_experiment(self, form: dict) -> str:
        """
        Creates the experiment from the form's values and returns its id.

        Args:
            form (dict): What experiment_form returns.
        """
        reply = self._request("POST", "experiment/create", body=form)
        if reply.get("status") == "error" or not reply.get("experimentId"):
            raise RobotariumError("Experiment refused: %s"
                                  % _reason(reply, "no experimentId in the reply"))
        return reply["experimentId"]

    def mark_main_file(self, main_file_id: str, file_ids: list, experiment_id: str) -> None:
        """
        Attaches the files to the experiment and marks one of them as the main file, as the
        page does after creating the experiment.

        Args:
            main_file_id (str): The id of the file to run.
            file_ids (list): Every uploaded file's id, the main one included.
            experiment_id (str): What create_experiment returned.
        """
        metadata = [{"fileId": file_id, "experimentId": experiment_id,
                     "isMain": file_id == main_file_id} for file_id in file_ids]
        reply = self._request("PUT", "files/update_metadata/%s" % main_file_id,
                              body={"metadata": metadata})
        if isinstance(reply, dict) and reply.get("status") == "error":
            raise RobotariumError("Marking the main file refused: %s"
                                  % _reason(reply, "no reason"))

    def _request(self, method: str, path: str, body: dict = None, files: dict = None):
        """
        One call to the API, its JSON reply decoded. A refusal raises RobotariumError with the
        reason the site gave -- and with nothing of the request, which may hold the password.
        The debug log names the call and its outcome, never a body.
        """
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif files is not None:
            data, headers["Content-Type"] = _multipart(files)
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token

        request = urllib.request.Request(self.url + path, data=data, headers=headers,
                                         method=method)
        log.debug("%s %s%s", method, path,
                  " (%d bytes)" % len(data) if data is not None and files else "")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = response.read()
                log.debug("%s %s -> HTTP %d", method, path, response.status)
        except urllib.error.HTTPError as error:
            log.debug("%s %s -> HTTP %d", method, path, error.code)
            raise RobotariumError("%s %s answered HTTP %d: %s" % (
                method, path, error.code, _reason(_decode(error.read()), error.reason))) from None
        except urllib.error.URLError as error:
            raise RobotariumError("Could not reach %s: %s" % (self.url, error.reason)) from None
        except (OSError, http.client.HTTPException) as error:
            # Raised from reading the reply, which urllib does not wrap: the request went out.
            log.debug("%s %s -> no answer (%s)", method, path, type(error).__name__)
            raise ConnectionLost("%s %s got no answer: %s" % (
                method, path, error or type(error).__name__)) from None

        return _decode(payload)


def experiment_form(title: str, description: str, duration_seconds: int, robots: int,
                    file_ids: list, draft: bool = False) -> dict:
    """
    The experiment form's values as the page posts them.

    The page's inputs are all type="text", so the numbers go as strings, the way a person
    typing them would send them.

    Args:
        title (str): "Title".
        description (str): "Experiment Description".
        duration_seconds (int): "Estimated Duration", in seconds.
        robots (int): "Number of Robots".
        file_ids (list): The uploaded files' ids.
        draft (bool): Save the experiment rather than queue it -- the page's "Save Experiment"
            rather than its "Submit Experiment".
    """
    return {
        "title": title,
        "estimatedDuration": str(int(duration_seconds)),
        "description": description,
        "numRobots": str(int(robots)),
        "demo": False,
        "scripts": list(file_ids),
        "status": "draft" if draft else "submitted",
        "exp_type": EXPERIMENT_TYPE,
    }


def check_against_settings(settings: dict, duration_seconds: int, robots: int,
                           paths: list) -> list:
    """
    What the site's live limits would refuse about a submission, as sentences; empty when
    nothing. The form checks these in the browser, so the API may not.

    Args:
        settings (dict): What RobotariumClient.settings returned.
        duration_seconds (int): The estimated duration.
        robots (int): The number of robots.
        paths (list): The files about to be uploaded.
    """
    problems = []

    allowed = [str(extension).lower() for extension in settings.get("allowedFileExt") or []]
    for path in paths:
        path = Path(path)
        if allowed and path.suffix.lower() not in allowed:
            problems.append("%s: the site does not accept %s files (it takes %s)."
                            % (path.name, path.suffix or "extensionless", " ".join(allowed)))

    max_duration = settings.get("maxDuration")
    if max_duration is not None and duration_seconds > int(max_duration):
        problems.append("Estimated duration %d s is over the site's maximum of %s s."
                        % (duration_seconds, max_duration))

    min_robots, max_robots = settings.get("minRbt"), settings.get("maxRbt")
    if (min_robots is not None and robots < int(min_robots)) \
            or (max_robots is not None and robots > int(max_robots)):
        problems.append("%d robots is outside the site's %s-%s." % (robots, min_robots,
                                                                    max_robots))

    return problems


def _multipart(files: dict):
    """
    A multipart/form-data body holding the named files, as the browser builds one from a
    FormData, and its Content-Type header.

    Args:
        files (dict): Field name to the path of the file to send in it.
    """
    boundary = "----robotarium%s" % uuid.uuid4().hex
    parts = []
    for field, path in files.items():
        path = Path(path)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append(("--%s\r\n"
                      "Content-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                      "Content-Type: %s\r\n\r\n" % (boundary, field, path.name, content_type)
                      ).encode("utf-8") + path.read_bytes() + b"\r\n")
    body = b"".join(parts) + ("--%s--\r\n" % boundary).encode("utf-8")
    return body, "multipart/form-data; boundary=%s" % boundary


def _decode(payload: bytes):
    """A reply's JSON, or the text itself wrapped as a message when it is not JSON."""
    try:
        return json.loads(payload.decode("utf-8") or "{}")
    except ValueError:
        return {"message": payload.decode("utf-8", "replace").strip()[:300]}


def _reason(reply, fallback) -> str:
    """The reason a reply gives: the API's own "message", or Flask-JWT's "msg"."""
    if isinstance(reply, dict):
        return str(reply.get("message") or reply.get("msg") or fallback)
    return str(fallback)
