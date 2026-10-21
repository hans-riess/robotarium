# Storing your Robotarium credentials

`robotarium submit` logs in to the Robotarium with the same email address and password you use
on the website. This page covers where to keep them so you never type them on a command line and
never commit them.

## Pick one

| Where | Good for | Set up with |
|---|---|---|
| **System keyring** (macOS Keychain, Windows Credential Locker, Secret Service on Linux) | your own laptop or desktop: recommended | `pip install keyring`, then `robotarium login` |
| **`.env` file** beside `robotarium.toml` | one project, or a machine with no keyring | copy `.env.example` to `.env`, fill it in, `chmod 600 .env` |
| **Environment variables** | CI, direnv, a password manager's CLI | `ROBOTARIUM_USERNAME`, `ROBOTARIUM_PASSWORD` |
| **Per-user file** | a headless machine with no keyring | `robotarium login --store file` |
| **Prompt** | occasional use | nothing: `submit` asks when it finds nothing else and a terminal is attached |

To see which one `robotarium` will use, and to test the login without submitting anything:

```bash
robotarium credentials            # where each value comes from (the email is masked)
robotarium credentials --verify   # ... and log in once to check them
```

### 1. The system keyring (recommended)

```bash
pip install -e ".[keyring]"     # in your clone, venv active; or: pip install keyring
robotarium login --verify
```

`login` asks for your email address and password, checks them against the site
(`--verify`), and stores:

- the **password** in the system keyring, under the service `robotarium.gatech.edu` and your
  email address;
- the **email address**, which is not secret, in the per-user file
  (`~/.config/robotarium/credentials`, or `%APPDATA%\robotarium\credentials` on Windows).

Nothing is written into your project. `robotarium logout` removes both.

On Linux without a desktop session, `keyring` may have no backend. `robotarium login` then
offers the per-user file instead. See option 4.

### 2. A `.env` file in the project

`robotarium init` writes a `.env.example` and a `.gitignore` that already ignores `.env`:

```bash
cp .env.example .env
chmod 600 .env          # readable by you only
$EDITOR .env
```

```dotenv
ROBOTARIUM_USERNAME=you@example.edu
ROBOTARIUM_PASSWORD=your-password
```

The file is looked for beside `robotarium.toml`, then in the current directory. `--env-file
PATH` or `ROBOTARIUM_ENV_FILE=PATH` points at a different one, for example a single file outside
all your repositories:

```bash
export ROBOTARIUM_ENV_FILE=~/.secrets/robotarium.env
```

`robotarium` checks two things each time it reads a `.env`, and warns if either fails:

- that other users on the machine can't read it (fix with `chmod 600 .env`);
- that, inside a git repository, git ignores it (fix by adding `.env` to `.gitignore`). If it
  isn't ignored, the next `git add .` commits your password.

The format is plain `KEY=VALUE`, read as dotenv reads it. Blank lines, `# comments` and an
`export ` prefix are accepted. An unquoted value ends at a ` #` comment and loses surrounding
spaces, so **quote a password that has leading or trailing spaces or a ` #`** in it: inside
`"double quotes"` everything is kept (write `\"` and `\\` for a quote and a backslash), and
inside `'single quotes'` everything is kept literally. Nothing is interpolated. `robotarium
login --store file` quotes for you.

### 3. Environment variables

Any value in the environment overrides every file:

```bash
export ROBOTARIUM_USERNAME=you@example.edu
export ROBOTARIUM_PASSWORD="$(security find-generic-password -s robotarium -w)"   # macOS
export ROBOTARIUM_PASSWORD="$(op read op://Private/Robotarium/password)"           # 1Password
export ROBOTARIUM_PASSWORD="$(pass show robotarium)"                               # pass
```

With [direnv](https://direnv.net/), an `.envrc` that runs one of the lines above loads the
password when you `cd` into the project. Keep `.envrc` out of git if it holds the password
itself rather than a command that fetches it.

**In GitHub Actions**, store both values as repository secrets and pass them to the step:

```yaml
- name: Submit to the Robotarium
  env:
    ROBOTARIUM_USERNAME: ${{ secrets.ROBOTARIUM_USERNAME }}
    ROBOTARIUM_PASSWORD: ${{ secrets.ROBOTARIUM_PASSWORD }}
  run: robotarium submit --yes --no-simulate
```

`--yes` skips the confirmation prompt, since CI has no one to answer it. The plan is still
printed into the log, and every check still runs. `--strict` also refuses on warnings.

### 4. The per-user file

```bash
robotarium login --store file
```

This writes both values to `~/.config/robotarium/credentials` (honouring `$XDG_CONFIG_HOME`) or
`%APPDATA%\robotarium\credentials` on Windows. The file is created readable by you only (mode
600). It's a reasonable choice on a single-user headless machine. On a shared machine, prefer
environment variables from a password manager. `robotarium logout` deletes it.

## Precedence

Each of the two values is looked up separately, and the first place that has it wins:

1. the environment (`ROBOTARIUM_USERNAME`, `ROBOTARIUM_PASSWORD`)
2. the `.env` file: `--env-file`, else `$ROBOTARIUM_ENV_FILE`, else `.env` beside
   `robotarium.toml`, else `.env` in the current directory
3. the per-user file
4. the system keyring (password only, looked up under the username found above)
5. a prompt, if a terminal is attached

So you can, for instance, keep the email in `.env` and the password in the keyring.

## What `robotarium` does with them

- **Never takes the password as a command-line argument.** It would end up in your shell
  history and in `ps` output.
- **Never prints or logs either value.** `robotarium credentials` shows the email masked
  (`y***@e***.edu`). `-v` and `--log-file` log each request's method, path and status, never a
  body. The plan `submit` prints shows the account masked.
- **Never records them.** The submission ledger (`.robotarium/submissions.jsonl`) holds what was
  sent and how it went, but not who sent it, so it's safe to commit.
- **Sends the password only to the login endpoint, over HTTPS**, encrypted the way the site's
  own login page encrypts it (CryptoJS AES with a key in the site's public JavaScript). That
  scheme is obfuscation rather than protection. HTTPS is what protects the password in transit,
  as it does in the browser.
- **Keeps the session token in memory only**, for the length of one command.

## If a password leaks

Change it on the Robotarium website first, then update wherever you stored it:
`robotarium login` again, or edit `.env` or your secret store. If a `.env` was committed,
removing it in a new commit doesn't remove it from the history. Treat the old password as
public.
