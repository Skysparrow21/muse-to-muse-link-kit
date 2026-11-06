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
- No DELETE. Nothing outside the root is reachable.

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

## Install (on the host PC)

1. Copy `muse_files.py` (and this README) into `C:\muse-relay\`.
2. Right-click `setup_files.bat` > Run as administrator. It removes
   the old task, registers the new one (SYSTEM, startup, 2-minute delay),
   and starts it.
3. Firewall rule for 8788 is created by the setup script if missing.
4. Verify: `http://<host-tailnet-ip>:8788/` should list the project folders.

## Notes

- The server logs to `C:\muse-relay\muse_files.log`, with a 5-minute
  heartbeat, so a silent death leaves evidence. The log rotates at
  10 MB (one old copy kept).
- Cleanup runs on startup and on every replace: `.replaced/` backups
  older than 30 days and stale upload parts older than 7 days are deleted.
- `run_files.bat` runs the server in a window for testing (Ctrl+C stops).
- The scheduled task's LastTaskResult 267009 means "currently running"
  (good for a long-running server). 112 means the launch failed.
