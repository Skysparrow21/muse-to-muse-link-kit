#!/usr/bin/env python3
"""muse-files: tiny tailnet-only HTTP file server for the shared Muse projects.

Serves the shared project tree over HTTP so both Muses can read and write
project files. FTP port 21 does not work for either Muse (their egress
proxies do not carry it), so this HTTP server is the only path for both.

Standard library only. No auth: the tailnet is the boundary, same as the
relay. Binds to the host PC's tailnet IP only and refuses any other address
except loopback test mode (--bind 127.0.0.1).

Endpoints (all under the root):
  GET  /path/        directory listing (HTML, or JSON with ?format=json)
  GET  /path/file    download the file (streams, no size cap, Range resume supported)
  GET  /path/?format=zip   download a whole folder as a zip (max 2 GB total, Range resume supported)
  PUT  /path/file    create a new file (409 if it already exists; never overwrites)
  PUT  /path/file?replace=1   replace an existing file (or create it if missing)
Uploads stream to disk (max 1 GB per file).
Resumable uploads: PUT with a Content-Range header (bytes start-end/total)
sends one chunk; the server appends it to a .part file and acks how much it
has. A dropped upload resumes by re-sending from the acked offset, so a
big file can finish across drops on a slow link. The final chunk moves the
file into place atomically.
Replacements are atomic (temp file + rename) and the previous version is
kept under .replaced/ (latest only, hidden from listings and zips).
_talk/ notes are append-only: replace is refused for them (403).
The log rotates at 10 MB (keeps one old copy). Backups under .replaced/
older than 30 days and stale upload parts older than 7 days are pruned.
No DELETE. Nothing outside the root is reachable.
Logs tracebacks and a 5-minute heartbeat to the log file, so a silent death leaves evidence.
"""

import argparse
import html
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, parse_qs

MAX_BYTES = 1024 * 1024 * 1024
ZIP_MAX_BYTES = 2 * 1024 * 1024 * 1024
LOG_MAX_BYTES = 10 * 1024 * 1024
REPLACED_KEEP_DAYS = 30
PART_KEEP_DAYS = 7
CHUNK = 65536

ROOT = None
LOG_PATH = None
_log_lock = threading.Lock()


def rotate_log():
    """Rotate the log file at LOG_MAX_BYTES, keeping one old copy."""
    if LOG_PATH is None:
        return
    try:
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            bak = LOG_PATH + ".1"
            try:
                os.unlink(bak)
            except OSError:
                pass
            os.replace(LOG_PATH, bak)
    except OSError:
        pass


def log(line):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = "%s %s\n" % (stamp, line)
    with _log_lock:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:
            pass
        if LOG_PATH is not None:
            try:
                rotate_log()
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                pass


def prune_old_files():
    """Delete .replaced/ backups older than REPLACED_KEEP_DAYS and stale
    upload temp files older than PART_KEEP_DAYS. Called at startup and on
    each replace."""
    if ROOT is None:
        return
    now = time.time()
    try:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            rel_dir = Path(dirpath).relative_to(ROOT)
            in_replaced = bool(rel_dir.parts) and rel_dir.parts[0] == ".replaced"
            for name in filenames:
                fp = Path(dirpath) / name
                try:
                    age = now - fp.stat().st_mtime
                except OSError:
                    continue
                try:
                    if in_replaced and age > REPLACED_KEEP_DAYS * 86400:
                        fp.unlink()
                    elif name.startswith(".upload-") and age > PART_KEEP_DAYS * 86400:
                        fp.unlink()
                except OSError:
                    pass
        # Drop empty folders left under .replaced/ (deepest first).
        base = ROOT / ".replaced"
        if base.is_dir():
            for dirpath, dirnames, filenames in os.walk(base, topdown=False):
                if Path(dirpath) == base:
                    continue
                try:
                    Path(dirpath).rmdir()
                except OSError:
                    pass
    except OSError:
        pass


def detect_tailnet_ip():
    try:
        out = subprocess.check_output(
            ["tailscale", "ip", "-4"], text=True, timeout=15
        ).strip()
    except Exception:
        return None
    for line in out.splitlines():
        ip = line.strip()
        if ip.startswith("100."):
            return ip
    return None


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    return ips


def resolve(rel):
    """Resolve a URL-relative path inside ROOT. Return a Path or None."""
    rel = rel.lstrip("/\\")
    target = (ROOT / rel).resolve()
    if target == ROOT or ROOT in target.parents:
        return target
    return None


def is_talk_path(rel):
    """True if any segment of the relative path is a _talk folder."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    return any(p.lower() == "_talk" for p in parts)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    def handle_error(self, request, client_address):
        log("handler error from %s:\n%s" % (client_address, traceback.format_exc()))

    def send_bytes(self, status, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        rel = unquote(parts.path).lstrip("/")
        query = parse_qs(parts.query)
        target = resolve(rel)
        if target is None:
            self.send_bytes(400, "bad path\n")
            return
        if target.is_dir():
            self.list_dir(target, parts.path, query)
        elif target.is_file():
            self.send_file(target)
        else:
            self.send_bytes(404, "not found\n")

    def list_dir(self, target, url_path, query):
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            self.send_bytes(500, "cannot list\n")
            return
        # Hide the replace-backup folder and in-progress upload temp files.
        entries = [e for e in entries
                   if e.name != ".replaced" and not e.name.startswith(".upload-")]
        if query.get("format") == ["zip"]:
            self.send_zip(target, url_path)
            return
        if query.get("format") == ["json"]:
            items = []
            for e in entries:
                items.append({
                    "name": e.name,
                    "type": "dir" if e.is_dir() else "file",
                    "size": e.stat().st_size if e.is_file() else 0,
                })
            body = json.dumps({"path": url_path, "entries": items}, indent=2)
            self.send_bytes(200, body, "application/json")
            return
        rows = []
        if url_path not in ("", "/"):
            parent = url_path.rstrip("/").rsplit("/", 1)[0] + "/"
            rows.append('<p><a href="%s">.. up</a></p>' % html.escape(parent))
        rows.append("<ul>")
        for e in entries:
            name = e.name + ("/" if e.is_dir() else "")
            href = quote(url_path.rstrip("/") + "/" + e.name) + ("/" if e.is_dir() else "")
            rows.append('<li><a href="%s">%s</a></li>' % (html.escape(href), html.escape(name)))
        rows.append("</ul>")
        page = "<html><head><title>muse-files %s</title></head><body><h1>%s</h1>%s</body></html>" % (
            html.escape(url_path), html.escape(url_path), "".join(rows))
        self.send_bytes(200, page, "text/html; charset=utf-8")

    def send_zip(self, target, url_path):
        files = []
        total = 0
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = [d for d in dirnames if d != ".replaced"]
            dirnames.sort()
            for name in sorted(filenames):
                if name.startswith(".upload-"):
                    continue
                fp = Path(dirpath) / name
                try:
                    total += fp.stat().st_size
                except OSError:
                    continue
                if total > ZIP_MAX_BYTES:
                    self.send_bytes(413, "folder too large to zip\n")
                    return
                files.append(fp)
        name = (target.name or "files") + ".zip"
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                for fp in files:
                    zf.write(fp, fp.relative_to(target).as_posix())
            size = os.path.getsize(tmp)
            start = 0
            end = size - 1
            status = 200
            range_header = self.headers.get("Range")
            if range_header:
                parsed = self.parse_range(range_header, size)
                if parsed == "unsat":
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parsed != "ignore":
                    start, end = parsed
                    status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(tmp, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            self.send_bytes(500, "cannot zip\n")
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def send_file(self, target):
        ctype, _ = mimetypes.guess_type(str(target))
        if ctype is None:
            ctype = "application/octet-stream"
        try:
            size = target.stat().st_size
            start = 0
            end = size - 1
            status = 200
            range_header = self.headers.get("Range")
            if range_header:
                parsed = self.parse_range(range_header, size)
                if parsed == "unsat":
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parsed != "ignore":
                    start, end = parsed
                    status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(target, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            self.send_bytes(500, "cannot read\n")

    def parse_range(self, header, size):
        # Supports "bytes=start-", "bytes=start-end", "bytes=-suffix".
        # Returns (start, end), "ignore" (malformed, serve whole file),
        # or "unsat" (range starts past the end of the file).
        if not header.startswith("bytes="):
            return "ignore"
        spec = header[len("bytes="):].strip()
        if "," in spec:
            return "ignore"
        if spec.startswith("-"):
            try:
                suffix = int(spec[1:])
            except ValueError:
                return "ignore"
            if suffix <= 0:
                return "ignore"
            return (max(0, size - suffix), size - 1)
        parts = spec.split("-")
        if len(parts) != 2:
            return "ignore"
        try:
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else size - 1
        except ValueError:
            return "ignore"
        if start < 0:
            return "ignore"
        if start >= size:
            return "unsat"
        if end < start:
            return "ignore"
        return (start, min(end, size - 1))

    def backup(self, target, rel):
        """Keep the previous version under .replaced/ (latest only)."""
        try:
            dest = (ROOT / ".replaced" / rel).resolve()
            if dest == ROOT or ROOT not in dest.parents:
                return
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
        except OSError:
            log("backup failed for %s" % rel)
        prune_old_files()

    def part_path(self, target):
        """Resume file for a chunked upload of target."""
        return target.parent / (".upload-" + target.name + ".part")

    def parse_content_range(self, header):
        """Parse 'bytes start-end/total'. Returns (start, end, total)."""
        if not header.startswith("bytes "):
            raise ValueError("bad range")
        spec = header[len("bytes "):].strip()
        if "/" not in spec:
            raise ValueError("bad range")
        span, total = spec.split("/", 1)
        if "-" not in span:
            raise ValueError("bad range")
        a, b = span.split("-", 1)
        start = int(a)
        end = int(b)
        total = int(total)
        if start < 0 or end < start or end >= total or total <= 0:
            raise ValueError("bad range")
        return start, end, total

    def do_PUT(self):
        parts = urlsplit(self.path)
        rel = unquote(parts.path).lstrip("/")
        query = parse_qs(parts.query)
        replace = query.get("replace") == ["1"]
        target = resolve(rel)
        if target is None:
            self.send_bytes(400, "bad path\n")
            return
        length = self.headers.get("Content-Length")
        if length is None:
            self.send_bytes(411, "length required\n")
            return
        try:
            nbytes = int(length)
        except ValueError:
            self.send_bytes(400, "bad length\n")
            return
        if nbytes < 0 or nbytes > MAX_BYTES:
            self.send_bytes(413, "too large\n")
            return
        range_header = self.headers.get("Content-Range")
        chunked = None
        if range_header:
            try:
                chunked = self.parse_content_range(range_header)
            except ValueError:
                self.send_bytes(400, "bad content-range\n")
                return
            start, end, total = chunked
            if total > MAX_BYTES:
                self.send_bytes(413, "too large\n")
                return
            if nbytes != end - start + 1:
                self.send_bytes(400, "length does not match range\n")
                return
        if target.is_dir():
            self.send_bytes(409, "already exists\n")
            return
        exists = target.exists()
        if exists and not replace:
            self.send_bytes(409, "already exists\n")
            return
        if exists and replace and is_talk_path(rel):
            self.send_bytes(403, "talk notes are append-only\n")
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if chunked is not None:
                self.put_chunked(target, rel, chunked, exists, replace)
                return
            fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=str(target.parent))
            try:
                remaining = nbytes
                with os.fdopen(fd, "wb") as f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(CHUNK, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                if remaining > 0:
                    os.unlink(tmp)
                    self.send_bytes(400, "short upload\n")
                    return
                if exists:
                    self.backup(target, rel)
                os.replace(tmp, target)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            self.send_bytes(500, "cannot write\n")
            return
        verb = "replaced" if exists else "created"
        log("%s %s" % (verb, rel))
        self.send_bytes(200 if exists else 201, "%s %s\n" % (verb, rel))

    def put_chunked(self, target, rel, chunked, exists, replace):
        """One chunk of a resumable upload. Chunks arrive in order; the
        server appends to a .part file and acks how much it has. The final
        chunk moves the file into place atomically."""
        start, end, total = chunked
        part = self.part_path(target)
        if start == 0:
            try:
                part.unlink()
            except OSError:
                pass
        else:
            try:
                if part.stat().st_size != start:
                    self.send_bytes(409, "offset mismatch, restart upload\n")
                    return
            except OSError:
                self.send_bytes(409, "offset mismatch, restart upload\n")
                return
        try:
            remaining = end - start + 1
            with open(part, "ab") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            if remaining > 0:
                try:
                    os.truncate(part, start)
                except OSError:
                    pass
                self.send_bytes(400, "short upload\n")
                return
            received = part.stat().st_size
            if received != end + 1:
                self.send_bytes(400, "short upload\n")
                return
            if end < total - 1:
                self.send_bytes(200, "received %d/%d\n" % (received, total))
                return
            if exists:
                self.backup(target, rel)
            os.replace(part, target)
        except OSError:
            self.send_bytes(500, "cannot write\n")
            return
        verb = "replaced" if exists else "created"
        log("%s %s (chunked)" % (verb, rel))
        self.send_bytes(200 if exists else 201, "%s %s\n" % (verb, rel))

    def do_DELETE(self):
        self.send_bytes(405, "no delete\n")

    def do_POST(self):
        self.send_bytes(405, "use PUT\n")


def main():
    global ROOT, LOG_PATH
    ap = argparse.ArgumentParser(description="muse-files HTTP server")
    ap.add_argument("--bind", default=None,
                    help="tailnet IP to bind (default: auto-detect via tailscale)")
    ap.add_argument("--port", type=int, default=8788, help="port (default 8788)")
    ap.add_argument("--root", default=r"C:\muse-relay\data\ftp", help="folder to serve")
    ap.add_argument("--log", default=r"C:\muse-relay\muse_files.log", help="log file")
    args = ap.parse_args()

    ROOT = Path(args.root).resolve()
    LOG_PATH = args.log
    ROOT.mkdir(parents=True, exist_ok=True)
    prune_old_files()

    if args.bind == "0.0.0.0":
        log("refusing to bind 0.0.0.0: tailnet IP only")
        return 2
    bind = args.bind
    if bind is None:
        bind = detect_tailnet_ip()
    if not bind:
        log("could not detect a tailnet IP; pass --bind <tailnet-ip>")
        return 2
    if bind != "127.0.0.1" and bind not in local_ips():
        log("bind IP %s is not one of this machine's addresses" % bind)
        return 2

    try:
        server = ThreadingHTTPServer((bind, args.port), Handler)
    except OSError:
        log("cannot bind http://%s:%d/:\n%s" % (bind, args.port, traceback.format_exc()))
        return 1
    server.daemon_threads = True
    log("muse-files up on http://%s:%d/ serving %s (pid %d)" % (bind, args.port, ROOT, os.getpid()))
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(300):
            log("alive (pid %d)" % os.getpid())

    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("muse-files stopping (keyboard interrupt)")
    except BaseException:
        log("muse-files FATAL:\n%s" % traceback.format_exc())
        raise
    finally:
        stop.set()
    server.server_close()
    log("muse-files stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
