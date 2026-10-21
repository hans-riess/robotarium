"""
Where the Robotarium login comes from, so it never has to be typed on a command line or written
into a file that gets committed.

The username (the email address the site logs in with) and the password are each looked up in
this order, the first place that has it winning:

    1. the environment            ROBOTARIUM_USERNAME, ROBOTARIUM_PASSWORD
    2. a dotenv file              --env-file, else $ROBOTARIUM_ENV_FILE, else .env beside the
                                  robotarium.toml, else .env in the current directory
    3. the user credentials file  ~/.config/robotarium/credentials (%APPDATA% on Windows),
                                  written by `robotarium login --store file`
    4. the system keyring         the password only, under the username; written by
                                  `robotarium login` when the `keyring` package is installed
    5. a prompt                   when a terminal is attached and nothing above had it

The password is never accepted as a command-line argument (it would land in the shell history
and in `ps`), never printed, never logged, and never written anywhere by this module except the
two stores `robotarium login` is explicitly asked to use. docs/credentials.md is the guide.
"""

import getpass
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

USERNAME_VARIABLE = "ROBOTARIUM_USERNAME"
PASSWORD_VARIABLE = "ROBOTARIUM_PASSWORD"
ENV_FILE_VARIABLE = "ROBOTARIUM_ENV_FILE"

#: The keyring service the password is stored under, one entry per username.
KEYRING_SERVICE = "robotarium.gatech.edu"


class CredentialsError(RuntimeError):
    """No username or password could be found. Names where to put them; never carries them."""


@dataclass(frozen=True)
class Credentials:
    """
    A username and password, and where each came from. Neither value appears in repr() or
    str(), so a Credentials can be logged or printed by accident without leaking.

    Args:
        username (str): The account's email address.
        password (str): Its password, in the clear.
        username_source (str): Where the username was found, in words.
        password_source (str): Where the password was found, in words.
        warnings (tuple): Anything worth fixing about where they were found.
    """

    username: str = field(repr=False)
    password: str = field(repr=False)
    username_source: str = ""
    password_source: str = ""
    warnings: tuple = ()

    def __str__(self) -> str:
        return "username from %s, password from %s" % (self.username_source,
                                                       self.password_source)


def user_credentials_file() -> Path:
    """The per-user credentials file: under %APPDATA% on Windows, $XDG_CONFIG_HOME elsewhere."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "robotarium" / "credentials"


def read_env_file(path: Path, inline_comments: bool = True) -> dict:
    """
    KEY=VALUE pairs from a dotenv file, read as python-dotenv reads them: blank lines and #
    comments skipped, an `export ` prefix allowed. A value in double quotes keeps everything
    inside them, with the escapes \\\\ \\" \\' \\n \\t \\r \\a \\b \\f \\v decoded and any other
    backslash kept as it is; in single quotes, everything literally. An unquoted value has its
    surrounding whitespace stripped and, with inline_comments, ends at a ` #` comment. Nothing
    is interpolated.

    Args:
        path (Path): The file.
        inline_comments (bool): Treat ` #` in an unquoted value as the start of a comment. The
            per-user file is read without, since `robotarium login` once wrote values unquoted.
    """
    values = {}
    # Lines end at \n alone (and a \r before it): a password may hold any other character
    # str.splitlines() would also break at.
    for line in _read_text(path).split("\n"):
        line = line.removesuffix("\r").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        values[key] = _env_value(value.lstrip(), inline_comments)
    return values


def _read_text(path: Path) -> str:
    """A credentials file's text. One that is not UTF-8 is refused by name: the decoding
    error would quote the offending byte, which may be part of the password."""
    try:
        return Path(path).read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise CredentialsError("%s is not UTF-8 text; save it as UTF-8." % path) from None


#: The first line of a per-user file written by this version of `robotarium login`. A file
#: without it was written by the first version, which did not quote values: it is read with that
#: version's rules, so a password stored then still reads back as it was.
USER_FILE_HEADER = "# Written by `robotarium login` (format 2). Remove with `robotarium logout`."


def read_user_file(path: Path) -> dict:
    """The per-user credentials file, in whichever format wrote it."""
    text = _read_text(path)
    if text.split("\n", 1)[0].strip() == USER_FILE_HEADER:
        return read_env_file(path, inline_comments=False)
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


#: The escapes python-dotenv decodes inside double quotes; any other backslash is kept.
_ESCAPES = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "t": "\t", "r": "\r", "a": "\a",
            "b": "\b", "f": "\f", "v": "\v"}


def _env_value(text: str, inline_comments: bool = True) -> str:
    """One value, as read_env_file describes."""
    if text[:1] == '"':
        value, escaped = [], False
        for character in text[1:]:
            if escaped:
                value.append(_ESCAPES.get(character, "\\" + character))
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                return "".join(value)
            else:
                value.append(character)
    elif text[:1] == "'" and "'" in text[1:]:
        return text[1:text.index("'", 1)]
    if inline_comments:
        text = re.split(r"\s#", text, maxsplit=1)[0]
    return text.strip()


def _quote(value: str) -> str:
    """A value written so that _env_value reads it back exactly."""
    return '"%s"' % (value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                     .replace("\r", "\\r"))


def env_file_candidates(project_root: Path = None, env_file: Path = None) -> list:
    """
    The dotenv files to look in, in order. An explicitly named one is the only candidate.

    Args:
        project_root (Path): The directory holding robotarium.toml, if any.
        env_file (Path): A file named on the command line.
    """
    if env_file:
        return [Path(env_file).expanduser()]
    if os.environ.get(ENV_FILE_VARIABLE):
        return [Path(os.environ[ENV_FILE_VARIABLE]).expanduser()]
    candidates = []
    for directory in (project_root, Path.cwd()):
        if directory is not None:
            candidate = (Path(directory) / ".env").resolve()
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def resolve(project_root: Path = None, env_file: Path = None, use_keyring: bool = True,
            interactive: bool = None) -> Credentials:
    """
    The username and password, from the first place that has each; see the module docstring.

    Args:
        project_root (Path): The directory holding robotarium.toml, whose .env is looked in.
        env_file (Path): A dotenv file to use instead of the default search.
        use_keyring (bool): Look in the system keyring for the password.
        interactive (bool): Prompt for whatever is still missing; default whether stdin is a
            terminal.
    """
    warnings = []
    username = password = None
    username_source = password_source = None

    def take(values: dict, where: str):
        nonlocal username, password, username_source, password_source
        if username is None and values.get(USERNAME_VARIABLE):
            username, username_source = values[USERNAME_VARIABLE], where
        if password is None and values.get(PASSWORD_VARIABLE):
            password, password_source = values[PASSWORD_VARIABLE], where

    take(os.environ, "the environment")

    explicit = env_file is not None or bool(os.environ.get(ENV_FILE_VARIABLE))
    for candidate in env_file_candidates(project_root, env_file):
        if not candidate.is_file():
            if explicit:
                raise CredentialsError("The credentials file %s does not exist." % candidate)
            continue
        before = (username, password)
        take(read_env_file(candidate), str(candidate))
        if (username, password) != before:
            warnings.extend(file_warnings(candidate, in_project=True))

    user_file = user_credentials_file()
    if (username is None or password is None) and user_file.is_file():
        before = (username, password)
        take(read_user_file(user_file), str(user_file))
        if (username, password) != before:
            warnings.extend(file_warnings(user_file, in_project=False))

    if password is None and username is not None and use_keyring:
        stored = keyring_password(username)
        if stored:
            password, password_source = stored, "the system keyring"

    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    if interactive and username is None:
        username = input("Robotarium email: ").strip() or None
        username_source = "the prompt"
    if interactive and password is None and username is not None:
        password = getpass.getpass("Robotarium password: ") or None
        password_source = "the prompt"

    missing = [name for name, value in ((USERNAME_VARIABLE, username),
                                        (PASSWORD_VARIABLE, password)) if not value]
    if missing:
        raise CredentialsError(
            "No %s found. Set %s in the environment or in a .env file beside robotarium.toml, "
            "or run `robotarium login` to keep them in the system keyring. docs/credentials.md "
            "has the details." % (" or ".join(missing), " and ".join(missing)))
    check_text(**{"%s from %s" % (USERNAME_VARIABLE, username_source): username,
                  "%s from %s" % (PASSWORD_VARIABLE, password_source): password})

    return Credentials(username, password, username_source, password_source, tuple(warnings))


def check_text(**values) -> None:
    """
    Refuses a value that is not valid text -- bytes that are not UTF-8, from a mis-encoded
    environment, file or terminal -- by name only: the value, and the character, stay out of
    the message.
    """
    for name, value in values.items():
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise CredentialsError("%s is not valid text (it holds bytes that are not UTF-8); "
                                   "set it again." % name) from None


def file_warnings(path: Path, in_project: bool) -> list:
    """
    What is unsafe about keeping credentials in `path`: readable by other users, or inside a git
    repository without being ignored -- that is, about to be committed.

    Args:
        path (Path): A file credentials were read from.
        in_project (bool): Whether it is a project's .env, rather than the per-user file.
    """
    warnings = []
    if os.name == "posix":
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            warnings.append("%s can be read by other users of this machine; run: chmod 600 %s"
                            % (path, path))
    if in_project:
        try:
            ignored = subprocess.run(["git", "check-ignore", "-q", path.name], cwd=path.parent,
                                     capture_output=True, timeout=10).returncode
        except (OSError, subprocess.SubprocessError):
            ignored = None
        # 0: ignored; 1: in a repository and NOT ignored; 128: not in a repository.
        if ignored == 1:
            warnings.append("%s is inside a git repository and not ignored, so the password in "
                            "it will be committed. Add %s to .gitignore." % (path, path.name))
    return warnings


# ---------------------------------------------------------------------------
# storing and forgetting: `robotarium login` / `robotarium logout`
# ---------------------------------------------------------------------------

def keyring_available() -> bool:
    """Whether the `keyring` package is installed and has a usable backend."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return False
    return not isinstance(keyring.get_keyring(), fail.Keyring)


def keyring_password(username: str):
    """The password the system keyring holds for `username`, or None."""
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:  # noqa: BLE001 -- no keyring, no backend, a locked one: all mean "not here"
        return None


def store(username: str, password: str, where: str = "keyring") -> str:
    """
    Keeps the credentials for later runs, and says where they went.

    "keyring" puts the password in the system keyring (macOS Keychain, Windows Credential
    Locker, Secret Service on Linux) and the username, which is not secret, in the user
    credentials file. "file" puts both in that file, readable by this user only.

    Args:
        username (str): The account's email address.
        password (str): Its password.
        where (str): "keyring" or "file".
    """
    path = user_credentials_file()
    check_text(username=username, password=password)
    values = read_user_file(path) if path.is_file() else {}
    values[USERNAME_VARIABLE] = username

    if where == "keyring":
        if not keyring_available():
            raise CredentialsError("No usable system keyring: pip install keyring, or store "
                                   "with --store file.")
        import keyring
        keyring.set_password(KEYRING_SERVICE, username, password)
        values.pop(PASSWORD_VARIABLE, None)
        destination = "the system keyring (password) and %s (username)" % path
    elif where == "file":
        values[PASSWORD_VARIABLE] = password
        destination = str(path)
    else:
        raise ValueError("where must be 'keyring' or 'file', not %r" % where)

    _write_private(path, values)
    return destination


def forget() -> list:
    """Removes what store() kept, and says what was removed."""
    removed = []
    path = user_credentials_file()
    try:
        values = read_user_file(path) if path.is_file() else {}
    except CredentialsError:
        values = {}  # unreadable; it is removed all the same
    username = values.get(USERNAME_VARIABLE) or os.environ.get(USERNAME_VARIABLE)
    if username and keyring_password(username):
        import keyring
        keyring.delete_password(KEYRING_SERVICE, username)
        removed.append("the password in the system keyring")
    if path.is_file():
        path.unlink()
        removed.append(str(path))
    return removed


def _write_private(path: Path, values: dict) -> None:
    """Writes a dotenv file that only this user can read, creating it that way from the start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = USER_FILE_HEADER + "\n" + "".join(
        "%s=%s\n" % (key, _quote(value)) for key, value in values.items())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.name == "posix":
        os.chmod(path, 0o600)


def mask(username: str) -> str:
    """An email address shown only well enough to tell two accounts apart: h***@g***.com."""
    name, _, domain = str(username).partition("@")
    host, _, tld = domain.rpartition(".")
    return "%s***@%s***.%s" % (name[:1], host[:1], tld) if host else "%s***" % name[:1]
