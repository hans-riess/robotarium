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

`robotarium fetch` makes the calls the experiment's own page (`/experiment/<id>`) makes to show
what came back:

| Call | Body | Reply | Page equivalent |
|---|---|---|---|
| `GET experiment/<id>` | | `{"experiment": {"title", "status", "createdAt", "finishedAt", "videoUrls", "returnMessages", "scripts", "logFiles", "dataFiles", ...}}`; the last three are lists of file ids | the page itself |
| `GET experiment/get-queue/<id>` | | `{"queue_position", "queue_length"}` | the queue position shown while it waits |
| `POST files/id_list` | `{"ids", "type"}`, `type` being `"scripts"`, `"logFiles"` or `"dataFiles"` | `[{"fileId", "name", "uploadDate", "isMain"}, ...]` | the **Scripts**, **Data Files** and **Log Files** tables |
| `GET files/preview/<type>/<file id>` | | the file | a table's **Download** button |
| `GET <videoUrls>/download` | | the video (MP4) | **Experiment Video** |

The page plays the video from `<videoUrls>/download` without the session token, so `fetch` sends
the token there only when the address is under the API root. `videoUrls` is `"-"` for a run that
made no video and `""` for one that hasn't run, and `fetch` asks for neither. The site answers
any address outside the API with its single-page app (HTML, HTTP 200), so `fetch` keeps a video
only when the reply is one: an MP4's `ftyp` box at offset 4, or a `video/*` or
`application/octet-stream` reply that doesn't read as a web page. An experiment queued again
after it ran is back to `"submitted"`, with the earlier run's `finishedAt` (before
`lastUpdatedAt`) and empty `logFiles` and `dataFiles`. The page only checks whether
`returnMessages` is present and never shows its shape, so `fetch` writes it out as text whatever
it holds. A download that redirects elsewhere (to storage, say) never carries the token with it.

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
(`token`, `file_id`, `experimentId`, `experiment`). To re-read the API:

1. Open https://www.robotarium.gatech.edu, log in, and open the browser's developer tools on the
   **Network** tab.
2. Create a draft experiment by hand: upload two files, tick **Main File**, **Save Experiment**.
   For `fetch`, open a completed experiment's page from the dashboard and download a log file.
3. Compare the requests (paths, bodies, field names) with the table above and with
   `src/robotarium/client.py`.

`ROBOTARIUM_API_URL` points the client somewhere else, for example a local stand-in like the
one in `tests/conftest.py`.

## The limits checked offline

`src/robotarium/limits.py` holds what's checked before any network call: 1-18 robots (the arena
has 18 GRITSBots, though the simulator accepts 50), a duration of at most 600 s, and the upload
extensions. After logging in, `submit` also checks the site's live `settings/` and refuses if
they disagree. `robotarium limits` prints both.
