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

And what its experiment page (experiment/<id>) calls to show the results that come back:

    GET  experiment/<id>            {"experiment": {"title", "status", "videoUrls",
                                    "returnMessages", "scripts", "logFiles", "dataFiles", ...}};
                                    the three lists hold file ids.
    GET  experiment/get-queue/<id>  {"queue_position", "queue_length"}.
    POST files/id_list              {"ids", "type"} -> [{"fileId", "name", "uploadDate",
                                    "isMain"}, ...], type being "scripts", "logFiles" or
                                    "dataFiles".
    GET  files/preview/<type>/<id>  the file itself: what the page's download button fetches.
    GET  <videoUrls>/download       the video, which the page plays from there. "-" when
                                    the run made none, "" before it has run.

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
import urllib.parse
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

#: How much of a download to hold in memory at once.
CHUNK_BYTES = 1 << 20

#: The Content-Types a video may arrive as: a video's own, or the generic one storage gives a
#: file it knows nothing about.
VIDEO_TYPES = ("video/", "application/octet-stream")

log = logging.getLogger(__name__)


class RobotariumError(RuntimeError):
    """
    The site refused a request, with the reason it gave. Never carries the credentials.

    Args:
        message (str): What happened.
        status (int): The HTTP status the site answered with, when it answered with one.
        uncertain (bool): Whether the site may have done what was asked anyway -- an answer
            lost, a gateway's 5xx, a success that does not say what it made.
    """

    def __init__(self, message: str, status: int = None, uncertain: bool = None):
        super().__init__(message)
        self.status = status
        self.uncertain = bool(uncertain) if uncertain is not None else (status or 0) >= 500


class ConnectionLost(RobotariumError):
    """
    A request was sent and no answer came back: the connection dropped or timed out. Unlike a
    refusal, the site may have acted on it -- an experiment may exist although create failed.
    """

    def __init__(self, message: str):
        super().__init__(message, uncertain=True)


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
        try:
            encrypted = encrypt_password(password)
        except UnicodeEncodeError:
            # The error would quote the character; the message names the problem only.
            raise RobotariumError("The password is not valid text (it holds bytes that are not "
                                  "UTF-8).") from None
        reply = self._request("POST", "auth/login",
                              body={"email": email, "password": encrypted})
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
        if reply.get("status") == "error":
            raise RobotariumError("Experiment refused: %s" % _reason(reply, "no reason"))
        if not reply.get("experimentId"):
            # Answered, without an error and without an id: whether it made one is unknown.
            raise RobotariumError("The site answered without an experiment id: %s"
                                  % _reason(reply, "no message"), uncertain=True)
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

    def experiment(self, experiment_id: str) -> dict:
        """
        The experiment's record, as its page shows it: title, status, the video's address, the
        return messages, and the ids of its scripts, log files and data files.

        Args:
            experiment_id (str): What create_experiment returned.
        """
        reply = self._request("GET", "experiment/%s" % urllib.parse.quote(experiment_id, safe=""))
        if not isinstance(reply, dict) or not isinstance(reply.get("experiment"), dict):
            raise RobotariumError("No experiment %s: %s"
                                  % (experiment_id, _reason(reply, "no experiment in the reply")))
        return reply["experiment"]

    def queue_position(self, experiment_id: str) -> dict:
        """Where a queued experiment stands: {"queue_position", "queue_length"}."""
        return self._request("GET", "experiment/get-queue/%s"
                             % urllib.parse.quote(experiment_id, safe=""))

    def file_details(self, file_ids: list, file_type: str) -> list:
        """
        The name and upload date of each file, as the page lists them.

        Args:
            file_ids (list): Ids from the experiment's record.
            file_type (str): Which list they came from: "scripts", "logFiles" or "dataFiles".
        """
        reply = self._request("POST", "files/id_list", body={"ids": list(file_ids),
                                                             "type": file_type})
        if not isinstance(reply, list):
            raise RobotariumError("Listing the %s refused: %s"
                                  % (file_type, _reason(reply, "no list in the reply")))
        return reply

    def download_file(self, file_id: str, file_type: str, sink) -> None:
        """
        Writes one of the experiment's files to sink, as the page's download button fetches it.

        Args:
            file_id (str): Its id.
            file_type (str): "scripts", "logFiles" or "dataFiles".
            sink: A file open for binary writing.
        """
        self._request("GET", "files/preview/%s/%s" % (urllib.parse.quote(file_type, safe=""),
                                                      urllib.parse.quote(file_id, safe="")),
                      sink=sink)

    def download_video(self, video_url: str, sink) -> None:
        """
        Writes the experiment's video to sink, from <videoUrls>/download as the page plays it.
        Refused with RobotariumError, before anything is written, when the reply is not a video:
        the site answers an address it has no route for with its own page, HTML with HTTP 200.

        Args:
            video_url (str): The record's videoUrls (one of them, when it lists several).
            sink: A file open for binary writing.
        """
        self.download("%s/download" % video_url, sink, expect=video_problem)

    def download(self, url: str, sink, expect=None) -> None:
        """
        Writes what an address the site gave out (the video's) holds to sink. The session token
        goes with it only when the address is under the API root: the page plays the video
        without one, and it may be stored elsewhere.

        Args:
            url (str): The address, absolute or relative to the API root.
            sink: A file open for binary writing.
            expect: A function given the reply's Content-Type and its first chunk that returns
                why the reply is not what was asked for, or None when it is; a reason refuses
                the download with RobotariumError before anything reaches sink.
        """
        url = urllib.parse.urljoin(self.url, url)
        if url.startswith(self.url):
            self._request("GET", url[len(self.url):], sink=sink, expect=expect)
        else:
            parts = urllib.parse.urlsplit(url)
            self._request("GET", url, sink=sink, authorized=False, expect=expect,
                          label="%s://%s%s" % (parts.scheme, parts.netloc, parts.path))

    def _request(self, method: str, path: str, body: dict = None, files: dict = None,
                 sink=None, authorized: bool = True, label: str = None, expect=None):
        """
        One call to the API, its JSON reply decoded. A refusal raises RobotariumError with the
        reason the site gave -- and with nothing of the request, which may hold the password.
        The debug log names the call and its outcome, never a body.

        With sink, the reply is a file rather than JSON: it is written to sink as it arrives and
        nothing is returned. path may then be a whole URL, named in messages by label (which
        leaves out a query string that may sign it), and sent without the token if not
        authorized. expect, if given, is shown the Content-Type and the first chunk before
        anything is written, as download() describes.
        """
        headers = {"Accept": "*/*" if sink is not None else "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif files is not None:
            data, headers["Content-Type"] = _multipart(files)
        label = label or path

        url = path if "://" in path else self.url + path
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        if self.token and authorized:
            # Not carried over a redirect: a download may be handed to storage elsewhere.
            request.add_unredirected_header("Authorization", "Bearer %s" % self.token)
        log.debug("%s %s%s", method, label,
                  " (%d bytes)" % len(data) if data is not None and files else "")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                if sink is None:
                    payload = response.read()
                else:
                    payload, received = None, 0
                    chunk = response.read(CHUNK_BYTES)
                    problem = expect and expect(response.headers.get("Content-Type", ""), chunk)
                    if problem:
                        log.debug("%s %s -> HTTP %d, %s", method, label, response.status, problem)
                        raise RobotariumError("%s %s answered HTTP %d with %s." % (
                            method, label, response.status, problem), status=response.status)
                    while chunk:
                        received += len(chunk)
                        try:
                            sink.write(chunk)
                        except OSError as error:
                            raise _SinkFailed(error) from None
                        chunk = response.read(CHUNK_BYTES)
                    # A read in pieces ends quietly where the connection did; the whole-body
                    # read() above raises instead.
                    expected = response.headers.get("Content-Length")
                    if expected and expected.isdigit() and received < int(expected):
                        raise ConnectionLost("%s %s was cut off after %d of %s bytes."
                                             % (method, label, received, expected))
                log.debug("%s %s -> HTTP %d", method, label, response.status)
        except _SinkFailed as failed:
            raise failed.error from None
        except urllib.error.HTTPError as error:
            log.debug("%s %s -> HTTP %d", method, label, error.code)
            try:
                reply = _decode(error.read())
            except (OSError, http.client.HTTPException):
                reply = {}  # an error page cut off or stalled: the status is the answer
            raise RobotariumError("%s %s answered HTTP %d: %s" % (
                method, label, error.code, _reason(reply, error.reason)), status=error.code) \
                from None
        except urllib.error.URLError as error:
            raise RobotariumError("Could not reach %s: %s" % (
                self.url if label == path else label, error.reason)) from None
        except http.client.InvalidURL as error:
            raise RobotariumError("%s is not a usable %s: %s" % (
                (self.url, "API URL") if label == path else (label, "URL")) + (error,)) from None
        except (OSError, http.client.HTTPException) as error:
            # Raised from reading the reply, which urllib does not wrap: the request went out.
            log.debug("%s %s -> no answer (%s)", method, label, type(error).__name__)
            raise ConnectionLost("%s %s got no answer: %s" % (
                method, label, str(error) or type(error).__name__)) from None

        return None if sink is not None else _decode(payload)


class _SinkFailed(Exception):
    """Writing a download failed here (a full disk), not on the way: not a lost connection."""

    def __init__(self, error: OSError):
        super().__init__(str(error))
        self.error = error


def video_problem(content_type: str, head: bytes) -> str:
    """
    Why a reply is not a video, or None when it is: an MP4 (its "ftyp" box at offset 4), or
    anything sent as a video or as generic binary that does not read as a web page. The status
    alone does not say: the site answers an address it has no route for with its own page,
    HTML with HTTP 200.

    Args:
        content_type (str): The reply's Content-Type header; empty when it sent none.
        head (bytes): The reply's first chunk; empty when the reply was.
    """
    if head[4:8] == b"ftyp":
        return None
    kind = content_type.split(";")[0].strip().lower()
    markup = head.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] in (b"<", b"{")
    if head and not markup and kind.startswith(VIDEO_TYPES):
        return None
    if not head:
        what = "an empty reply"
    elif b"<html" in head[:1024].lower() or b"<!doctype html" in head[:1024].lower():
        what = "a web page"
    else:
        what = "something else"
    return "%s (%s), not a video" % (what, kind or "no Content-Type")


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
