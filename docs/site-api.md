# How submission talks to the Robotarium

The Robotarium has no documented public API. Its website is an Angular application over a JSON
API at `https://www.robotarium.gatech.edu/api/`, and `robotarium.client` makes the same calls
the website's own JavaScript makes, in the order a person clicking through **New Experiment**
causes. They were read from the site's main JavaScript bundle (first for
[robosheaf](https://github.com/hans-riess/robosheaf)'s `robosheaf submit`), not guessed.

| Call | Body | Reply | Form equivalent |
|---|---|---|---|
| `POST auth/login` | `{"email", "password"}`, password encrypted as below | `{"token", ...}` | logging in |
| `GET settings/` | | `{"allowedFileExt", "maxDuration", "minRbt", "maxRbt"}` | the limits the form validates against |
| `POST files/upload` | multipart, one file in the field `file` | `{"file_id"}` | dropping a file on the form, once per file |
| `POST experiment/create` | `title`, `estimatedDuration`, `description`, `numRobots`, `demo`, `scripts` (the file ids), `status` (`"submitted"` or `"draft"`), `exp_type` (`"ground"`) | `{"experimentId", "status", "message"}` | **Submit Experiment** / **Save Experiment** |
| `PUT files/update_metadata/<main id>` | `{"metadata": [{"fileId", "experimentId", "isMain"}, ...]}` | | ticking **Main File** |

Every call after login sends `Authorization: Bearer <token>`. Numbers in the experiment form are
sent as strings, as the form's text inputs send them.

**The password** is encrypted in the browser before it's posted:
`CryptoJS.AES.encrypt(password, Utf8.parse("robotarium-secret-key"), {iv: 0, mode: CBC,
padding: Pkcs7})`. The 21-byte key isn't a valid AES key length. CryptoJS accepts it anyway with
a fractional key schedule that no standard AES library reproduces, so `robotarium._cryptojs` is
a port of CryptoJS's own algorithm. The tests pin it to vectors produced by crypto-js itself.
Because the key is public, this is obfuscation, not security. HTTPS protects the password in
transit.

## When the site changes

Because the API is undocumented, the site can change it at any time. The symptoms would be a
`RobotariumError` naming an HTTP status, or a reply missing the field this client expects
(`token`, `file_id`, `experimentId`). To re-read the API:

1. Open https://www.robotarium.gatech.edu, log in, and open the browser's developer tools on the
   **Network** tab.
2. Create a draft experiment by hand: upload two files, tick **Main File**, **Save Experiment**.
3. Compare the requests (paths, bodies, field names) with the table above and with
   `src/robotarium/client.py`.

`ROBOTARIUM_API_URL` points the client somewhere else, for example a local stand-in like the
one in `tests/conftest.py`.

## The limits checked offline

`src/robotarium/limits.py` holds what's checked before any network call: 1-18 robots (the arena
has 18 GRITSBots, though the simulator accepts 50), a duration of at most 600 s, and the upload
extensions. After logging in, `submit` also checks the site's live `settings/` and refuses if
they disagree. `robotarium limits` prints both.
