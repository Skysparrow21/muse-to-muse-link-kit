# muse-files - tailnet-only HTTP file server

Serves the shared Muse project tree (`C:\muse-relay\data\ftp`) over HTTP so
both Muses can read and write project files. FTP port 21 does not work for
either Muse (their egress proxies do not carry it), so this HTTP server is
the only file path for both.

Standard library only. No auth: the tailnet is the boundary, same as the
relay. Binds to the host PC's tailnet IP only (loopback test mode with
`--bind 127.0.0.1`).

## Versions

- v1 (2026-11-05): listing, download, create-only PUT, zip.
- v2 (2026-11-05): streaming PUT up to 200 MB, Range resume, whole-folder
  zip up to 500 MB.
- v3 (2026-11-05): replace route (`PUT ?replace=1`), atomic writes, previous
  version kept under `.replaced/`, `_talk/` notes stay append-only.
- v4 (2026-11-05): caps raised (1 GB upload, 2 GB zip); resumable chunked
  uploads via Content-Range; Range resume on zip downloads; log rotation
  (10 MB, one old copy); automatic prune of old `.replaced/` backups
  (30 days) and stale upload parts (7 days).
- v5 (2026-11-05): full file operations. DELETE moves a file to a
  `.deleted/` trash (pruned after 30 days) instead of removing it for
  good; MOVE renames a file or moves it to another folder; POST
  `?mkdir=1` creates a directory. Empty (0-byte) files are allowed.
  Server header reports the version.
- v6 (2026-11-05): Unified Workspace. APPEND (`POST ?append=1`) appends
  one line to a file atomically; every successful write is logged to a
  server-maintained `/changes.json` sync index (`{ts, path, action, by}`);
  when a `state.json` is created or replaced the server regenerates the
  matching `STATUS.md` from it. `/inbox.jsonl` is the append-only message
  log. Server header reports `muse-files/6.0`.

## Endpoints (all under the root)

- `GET /path/` - directory listing (HTML, or JSON with `?format=json`).
- `GET /path/file` - download (streams, no size cap, Range resume).
- `GET /path/?format=zip` - whole folder as a zip (max 2 GB total, Range
  resume supported).
- `PUT /path/file` - create a new file. 409 if it already exists; never
  overwrites. Max 1 GB per file, streamed to disk.
- `PUT /path/file?replace=1` - replace an existing file (or create it if
  missing). Replaces are atomic: the upload streams to a temp file first,
  then renames over the old one, so a failed upload never corrupts the
  file in place. Returns 200 on replace, 201 on create.
- `DELETE /path/file` - remove a file by moving it to `.deleted/` trash
  (mirrors the project path, pruned after 30 days). 200 on success.
- `MOVE /old?to=/new` - rename a file or move it to another folder.
  409 if the destination exists (`&replace=1` overwrites it, the old
  destination goes to `.replaced/`). A `Destination:` header works too.
- `POST /path/?mkdir=1` - create a directory (parents too). 201 on
  create, 200 if it already exists.
- `POST /path/file?append=1` - append one line to a file, atomically.
  Creates the file (and parents) if missing. The body is the line; a
  trailing newline is added if missing. Max 1 MB per append. Returns
  JSON: `{"appended": true, "path": ..., "offset": ..., "lines": ...,
  "bytes": ...}` (offset = byte position where the line starts, lines =
  total line count after the append).
- Nothing outside the root is reachable.

## Resumable uploads (for slow links)

Send the file in chunks with a Content-Range header:

  PUT /path/file HTTP/1.1
  Content-Range: bytes 0-999999/10000000
  Content-Length: 1000000
  <first 1 MB>

The server appends each chunk to a hidden `.part` file and answers
`received <n>/<total>`. If a chunk fails, re-send it from the last acked
offset; a wrong offset gets 409 and the upload restarts from zero. The
final chunk (end == total - 1) moves the file into place atomically and
answers 200/201 as a normal PUT. `?replace=1` works the same way for
chunked uploads. A declared total over 1 GB gets 413 before any bytes
are read.

Why this matters on a slow link: at 2G speeds (~100-200 kbps) a 1 GB
upload takes roughly 12-15 hours of transfer time. With resume it can
finish across drops and pauses instead of restarting from zero every
time the connection breaks.

## Replace rules

- The previous version is kept under `.replaced/` (mirrors the project
  path, latest version only). It is hidden from listings and zips, but a
  GET on the exact `.replaced/...` path still downloads it for recovery.
- `_talk/` notes are append-only: `PUT ?replace=1` on any path under a
  `_talk/` folder is refused with 403, and the note is left untouched.
  Corrections to a talk note go in a new note, as always.
- Plain `PUT` without `?replace=1` still refuses to overwrite (409).

## Delete, move, and mkdir

- DELETE never removes a file for good on the spot: the file is moved to
  `.deleted/` (mirrors the project path, latest version only). It is
  hidden from listings and zips, but a GET on the exact `.deleted/...`
  path still downloads it for recovery. Files older than 30 days are
  pruned automatically.
- MOVE renames or moves a file. It is atomic (one rename on the same
  disk). Moving to an existing destination is refused with 409 unless
  `?replace=1` is given, in which case the old destination is kept under
  `.replaced/` first.
- POST `?mkdir=1` creates a directory, including any missing parents.
- `_talk/` notes are append-only for all three: DELETE, MOVE in or out,
  and replace are all refused with 403 there. Add a new note instead.

## Append and the sync index (changes.json)

- `POST ?append=1` appends one line to a file. It is used for the
  message log (`/inbox.jsonl`, one line per message) and for any other
  append-only file. Appends are serialised, so concurrent appends never
  interleave. `_talk/` notes refuse APPEND (403) - add a new note.
- Every successful write (PUT, replace, MOVE, DELETE, mkdir, append)
  appends an entry to `/changes.json`:
  `{ts, path, action, by}` (MOVE also carries `from`). `ts` is UTC ISO
  8601, `action` is one of put / replace / move / delete / mkdir /
  append / generate, `by` comes from the `?by=` query parameter or the
  `X-By:` header (default `unknown`). Each Muse polls `/changes.json`
  (one GET) and then GETs only the paths that changed since its
  watermark. This is the single sync index.
- `changes.json` is server-maintained: PUT, APPEND, MOVE, and DELETE on
  it are refused (403). GET is allowed (read-only).
- `/inbox.jsonl` is append-only: PUT, MOVE, and DELETE on it are refused
  (403). Messages are added with `POST /inbox.jsonl?append=1`. GET is
  allowed.

## state.json and STATUS.md

- Each project (and each Violet subcategory) keeps a machine-readable
  `state.json`: `{project, status, summary, last_updated, last_change:
  {by, at, what}, open_items: [{item, status, owner}], key_facts: {k: v}}`.
- When a `state.json` is created (PUT) or replaced (`PUT ?replace=1`),
  or moved into place (MOVE), the server regenerates the matching
  `STATUS.md` in the same directory from it (short human-readable
  summary: title, status, summary, last change, open items, key facts).
  The old STATUS.md is kept under `.replaced/`. The regeneration is
  logged in changes.json as action `generate`, by `server`.
- If a `state.json` is not valid JSON, the PUT still succeeds but
  STATUS.md is left unchanged (and a warning is logged).

## Install (on the host PC)

1. Copy `muse_files.py` (and this README) into `C:\muse-relay\`.
2. Right-click `setup_files.bat` > Run as administrator. It removes
   the old task, registers the new one (SYSTEM, startup, 2-minute delay),
   and starts it.
3. Firewall rule for 8788 is created by the setup script if missing.
4. Verify: `http://<host-tailnet-ip>:8788/` should list the project folders,
   and the Server header should say `muse-files/6.0`.

## Notes

- The server logs to `C:\muse-relay\muse_files.log`, with a 5-minute
  heartbeat, so a silent death leaves evidence. The log rotates at
  10 MB (one old copy kept).
- Cleanup runs on startup and after every change: `.replaced/` backups
  and `.deleted/` trash older than 30 days and stale upload parts older
  than 7 days are deleted.
- `run_files.bat` runs the server in a window for testing (Ctrl+C stops).
- The scheduled task's LastTaskResult 267009 means "currently running"
  (good for a long-running server). 112 means the launch failed.
- Known gaps carried over from v5: a 0-byte PUT returns 411 (likely the
  egress proxy stripping Content-Length: 0), and DELETE on a directory
  returns 400.
