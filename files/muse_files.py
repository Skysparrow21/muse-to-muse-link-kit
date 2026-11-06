#!/usr/bin/env python3
"""muse-files v6.0 - tailnet-only HTTP file server for the Muse-to-Muse share.

Serves one root folder over plain HTTP. No auth: the tailnet is the boundary.
Only ever bind a local address (loopback or a tailnet IP), never 0.0.0.0.

Endpoints (all paths relative to the root):
  GET  /dir/                 directory listing (HTML, or JSON with ?format=json)
  GET  /dir/?format=zip      whole-folder zip download (max 2 GB total)
  GET  /file                 streamed download (Range supported)
  PUT  /file                 create only (409 if it exists)
  PUT  /file?replace=1       replace (atomic; old version kept under .replaced/)
  PUT  /file  + Content-Range  resumable chunked upload (see README)
  DELETE /file               move to .deleted/ trash (pruned after 30 days)
  MOVE /old?to=/new          rename / move a file (?replace=1 overwrites)
  POST /dir/?mkdir=1         create a directory (parents too)
  POST /file?append=1        append one line to a file (atomic)

v6 additions (Unified Workspace):
  changes.json is server-maintained: every successful write (PUT, replace,
  MOVE, DELETE, mkdir, append) appends an entry {ts, path, action, by} to
  /changes.json. Clients read it, never write it directly. It is the
  single sync index; each Muse polls it and GETs only what changed.
  inbox.jsonl is the append-only message log (one line per message).
  When a state.json file is created or replaced, the server regenerates
  the matching STATUS.md from it (same directory).

Rules:
  _talk/ folders are append-only: no replace, no delete, no move in or out,
  and no append to an existing _talk/ note (add a new note instead).
  changes.json and inbox.jsonl are system files: no PUT, no MOVE, no DELETE
  on them. APPEND to changes.json is refused (server-only); APPEND to
  inbox.jsonl is how messages are added. GET on both is allowed.
  .replaced/ and .deleted/ are hidden from listings and zips; a GET on the
  exact path still downloads the file (recovery). They are pruned after
  30 days. Stale .part / .tmp files are pruned after 7 days.
"""
import argparse
import html
import json
import mimetypes
import os
import re
import socket
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, parse_qs

VERSION = "6.0"
PUT_MAX = 1024 * 1024 * 1024          # 1 GB per file
ZIP_MAX = 2 * 1024 * 1024 * 1024      # 2 GB per folder zip
APPEND_MAX = 1024 * 1024              # 1 MB per append (one line)
CHUNK = 64 * 1024                     # 64 KB stream chunks
LOG_ROTATE_BYTES = 10 * 1024 * 1024   # 10 MB, one old copy
REPLACED_KEEP_DAYS = 30
DELETED_KEEP_DAYS = 30
PART_KEEP_DAYS = 7
HEARTBEAT_SECS = 300

ROOT = None        # Path, set in main() before anything else runs
LOG_PATH = None    # Path, set in main() before anything else runs


def log(msg):
    """Write one line to stdout and to the log file. Never raise."""
    try:
        line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        print(line, flush=True)
        if LOG_PATH is not None:
            try:
                if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_ROTATE_BYTES:
                    old = Path(str(LOG_PATH) + ".1")
                    try:
                        if old.exists():
                            old.unlink()
                    except OSError:
                        pass
                    try:
                        LOG_PATH.rename(old)
                    except OSError:
                        pass
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
    except Exception:
        pass


def _prune_dir_tree(base, keep_days):
    """Delete files under base older than keep_days; remove empty dirs."""
    if not base.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for dirpath, dirnames, filenames in os.walk(base, topdown=False):
        for name in filenames:
            p = Path(dirpath) / name
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        for name in dirnames:
            d = Path(dirpath) / name
            try:
                d.rmdir()  # only succeeds if empty
            except OSError:
                pass
    return removed


def prune():
    """Prune old backups, trash, and stale partial uploads. Never raise."""
    try:
        n = 0
        if ROOT is not None:
            n += _prune_dir_tree(ROOT / ".replaced", REPLACED_KEEP_DAYS)
            n += _prune_dir_tree(ROOT / ".deleted", DELETED_KEEP_DAYS)
            cutoff = time.time() - PART_KEEP_DAYS * 86400
            for dirpath, dirnames, filenames in os.walk(ROOT, topdown=True):
                dirnames[:] = [d for d in dirnames if d not in (".replaced", ".deleted")]
                for name in filenames:
                    if name.startswith(".") and (name.endswith(".part") or name.endswith(".tmp")):
                        p = Path(dirpath) / name
                        try:
                            if p.stat().st_mtime < cutoff:
                                p.unlink()
                                n += 1
                        except OSError:
                            pass
        if n:
            log("prune: removed %d old file(s)" % n)
    except Exception:
        pass


def _resolve(url_path):
    """Map a URL path to (absolute Path, rel posix string). Raises ValueError."""
    raw = unquote(urlsplit(url_path).path)
    rel = raw.lstrip("/")
    abs_path = (ROOT / rel).resolve()
    if abs_path != ROOT and ROOT not in abs_path.parents:
        raise ValueError("outside root")
    if abs_path == ROOT:
        return abs_path, ""
    return abs_path, abs_path.relative_to(ROOT).as_posix()


def _is_talk(rel_posix):
    return "_talk" in Path(rel_posix).parts if rel_posix else False


def _is_reserved(rel_posix):
    if not rel_posix:
        return False
    parts = Path(rel_posix).parts
    if ".replaced" in parts or ".deleted" in parts:
        return True
    last = parts[-1]
    return last.startswith(".") and (last.endswith(".part") or last.endswith(".tmp"))


def _backup_to_replaced(abs_path, rel_posix):
    dest = ROOT / ".replaced" / rel_posix
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    os.replace(abs_path, dest)


def _trash_to_deleted(abs_path, rel_posix):
    dest = ROOT / ".deleted" / rel_posix
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    os.replace(abs_path, dest)


CHANGES_LOCK = threading.Lock()
APPEND_LOCK = threading.Lock()


def _is_system(rel_posix):
    """True for the server-maintained files at the root (changes.json, inbox.jsonl)."""
    return rel_posix in ("changes.json", "inbox.jsonl")


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log_change(path, action, by, extra=None):
    """Append one entry to /changes.json (the sync index). Never raise.

    Entry: {ts, path, action, by} plus any extra fields (e.g. "from" for MOVE).
    changes.json itself is never logged (it is the log).
    """
    try:
        entry = {"ts": _utc_now(), "path": path, "action": action, "by": by}
        if extra:
            entry.update(extra)
        with CHANGES_LOCK:
            f = ROOT / "changes.json"
            entries = []
            if f.exists():
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    if isinstance(data, list):
                        entries = data
                except (OSError, ValueError):
                    entries = []
            entries.append(entry)
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(dir=str(ROOT), prefix=".changes.", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(entries, fh)
                    fh.write("\n")
                os.replace(tmp, f)
                tmp = None
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
    except Exception:
        try:
            log("changes.json update failed:\n" + traceback.format_exc())
        except Exception:
            pass


def _render_status(state):
    """Render a state.json dict as a short human-readable STATUS.md."""
    lines = []
    project = state.get("project", "Project")
    lines.append("# %s" % project)
    lines.append("")
    status = state.get("status")
    if status:
        lines.append("**Status:** %s" % status)
        lines.append("")
    summary = state.get("summary")
    if summary:
        lines.append(summary)
        lines.append("")
    last_updated = state.get("last_updated")
    if last_updated:
        lines.append("**Last updated:** %s" % last_updated)
    last_change = state.get("last_change")
    if isinstance(last_change, dict):
        by = last_change.get("by", "")
        at = last_change.get("at", "")
        what = last_change.get("what", "")
        parts = [p for p in (by, at) if p]
        head = " ".join(parts)
        if head and what:
            lines.append("**Last change:** %s - %s" % (head, what))
        elif what:
            lines.append("**Last change:** %s" % what)
        elif head:
            lines.append("**Last change:** %s" % head)
    if last_updated or isinstance(last_change, dict):
        lines.append("")
    open_items = state.get("open_items")
    if isinstance(open_items, list) and open_items:
        lines.append("## Open items")
        lines.append("")
        for item in open_items:
            if isinstance(item, dict):
                text = item.get("item", "")
                st = item.get("status", "")
                owner = item.get("owner", "")
                bits = []
                if st:
                    bits.append(st)
                if owner:
                    bits.append("owner: %s" % owner)
                suffix = " (%s)" % ", ".join(bits) if bits else ""
                lines.append("- %s%s" % (text, suffix))
            else:
                lines.append("- %s" % item)
        lines.append("")
    key_facts = state.get("key_facts")
    if isinstance(key_facts, dict) and key_facts:
        lines.append("## Key facts")
        lines.append("")
        for k, v in key_facts.items():
            lines.append("- %s: %s" % (k, v))
        lines.append("")
    return "\n".join(lines)


def _maybe_generate_status(abs_path, rel_posix):
    """If abs_path is a state.json, regenerate the matching STATUS.md.

    Returns True if STATUS.md was written. Never raises.
    """
    try:
        if Path(rel_posix).name != "state.json":
            return False
        try:
            with open(abs_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            log("state.json %s is not valid JSON; STATUS.md left unchanged" % rel_posix)
            return False
        if not isinstance(state, dict):
            log("state.json %s is not an object; STATUS.md left unchanged" % rel_posix)
            return False
        content = _render_status(state)
        status_rel = str(Path(rel_posix).parent / "STATUS.md")
        status_abs = ROOT / status_rel
        tmp = None
        try:
            status_abs.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(status_abs.parent), prefix=".STATUS.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            if status_abs.exists():
                _backup_to_replaced(status_abs, status_rel)
            os.replace(tmp, status_abs)
            tmp = None
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        _log_change(status_rel, "generate", "server")
        return True
    except Exception:
        try:
            log("STATUS.md generation failed for %s:\n%s" % (rel_posix, traceback.format_exc()))
        except Exception:
            pass
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "muse-files/" + VERSION

    def version_string(self):
        return "muse-files/" + VERSION

    # -- response helpers -------------------------------------------------
    def _send_text(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _request_by(self, qs):
        """Who is making this write: ?by= param, X-By header, or 'unknown'."""
        if qs.get("by"):
            return qs["by"][0]
        hdr = self.headers.get("X-By")
        if hdr:
            return hdr
        return "unknown"

    # -- listings and downloads -------------------------------------------
    def _list_dir(self, abs_dir, rel_posix, as_json):
        try:
            entries = []
            with os.scandir(abs_dir) as it:
                for e in it:
                    name = e.name
                    if name in (".replaced", ".deleted"):
                        continue
                    if name.startswith(".") and (name.endswith(".part") or name.endswith(".tmp")):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        entries.append({"name": name, "size": 0, "type": "dir"})
                    else:
                        entries.append({"name": name, "size": e.stat().st_size, "type": "file"})
        except OSError:
            self._send_text(500, "cannot list directory")
            return
        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        if as_json:
            self._send_json(200, {"version": VERSION, "path": rel_posix, "entries": entries})
            return
        lines = []
        lines.append("<!doctype html><html><head><meta charset='utf-8'>")
        lines.append("<title>muse-files %s</title></head><body>" % html.escape(rel_posix or "/"))
        lines.append("<h1>%s</h1><ul>" % html.escape("/" + rel_posix if rel_posix else "/"))
        if rel_posix:
            parent = "/".join(rel_posix.split("/")[:-1])
            lines.append('<li><a href="/%s">../</a></li>' % quote(parent + "/" if parent else ""))
        for e in entries:
            href = "/" + quote(rel_posix + "/" + e["name"]) if rel_posix else "/" + quote(e["name"])
            if e["type"] == "dir":
                href += "/"
            lines.append('<li><a href="%s">%s</a></li>' % (href, html.escape(e["name"] + ("/" if e["type"] == "dir" else ""))))
        lines.append("</ul><hr><p>muse-files v%s</p></body></html>" % VERSION)
        body = "\n".join(lines).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, abs_path):
        try:
            size = abs_path.stat().st_size
        except OSError:
            self._send_text(404, "not found")
            return
        ctype, _enc = mimetypes.guess_type(str(abs_path))
        if not ctype:
            ctype = "application/octet-stream"
        start, end = 0, size - 1
        status = 200
        range_hdr = self.headers.get("Range")
        if range_hdr:
            m = re.match(r"bytes=(\d*)-(\d*)$", range_hdr.strip())
            if m:
                a, b = m.group(1), m.group(2)
                if a == "" and b == "":
                    pass  # malformed, ignore -> full file
                elif a == "":
                    n = int(b)
                    start = max(0, size - n)
                    end = size - 1
                    status = 206
                else:
                    start = int(a)
                    end = int(b) if b != "" else size - 1
                    status = 206
                if status == 206:
                    if start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if end >= size:
                        end = size - 1
                    if end < start:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(abs_path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                data = f.read(min(CHUNK, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)

    def _zip_dir(self, abs_dir, rel_posix):
        files = []
        total = 0
        for dirpath, dirnames, filenames in os.walk(abs_dir):
            dirnames[:] = [d for d in dirnames if d not in (".replaced", ".deleted")]
            for name in filenames:
                if name.startswith(".") and (name.endswith(".part") or name.endswith(".tmp")):
                    continue
                p = Path(dirpath) / name
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                total += sz
                if total > ZIP_MAX:
                    self._send_text(413, "folder too large for zip")
                    return
                files.append((p, p.relative_to(abs_dir).as_posix()))
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                for p, arc in files:
                    zf.write(p, arc)
            self._serve_file(Path(tmp))
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        try:
            abs_path, rel = _resolve(self.path)
        except ValueError:
            self._send_text(400, "bad path")
            return
        qs = parse_qs(urlsplit(self.path).query)
        if _is_reserved(rel):
            # exact .replaced/ or .deleted/ file: allow download (recovery)
            parts = Path(rel).parts if rel else ()
            if (".replaced" in parts or ".deleted" in parts) and abs_path.is_file():
                self._serve_file(abs_path)
            else:
                self._send_text(404, "not found")
            return
        if not abs_path.exists():
            self._send_text(404, "not found")
            return
        if abs_path.is_dir():
            if qs.get("format") == ["zip"]:
                self._zip_dir(abs_path, rel)
            else:
                self._list_dir(abs_path, rel, qs.get("format") == ["json"])
            return
        self._serve_file(abs_path)

    # -- PUT ---------------------------------------------------------------
    def _read_exact(self, n):
        """Read exactly n bytes from the request body (or fewer on EOF)."""
        buf = bytearray()
        while len(buf) < n:
            data = self.rfile.read(min(CHUNK, n - len(buf)))
            if not data:
                break
            buf.extend(data)
        return bytes(buf)

    def do_PUT(self):
        try:
            abs_path, rel = _resolve(self.path)
        except ValueError:
            self._send_text(400, "bad path")
            return
        if _is_reserved(rel):
            self._send_text(403, "reserved path")
            return
        if _is_system(rel):
            self._send_text(403, "server-maintained file (use APPEND for inbox.jsonl)")
            return
        if abs_path.is_dir() or urlsplit(self.path).path.endswith("/"):
            self._send_text(400, "cannot PUT a directory")
            return
        length_hdr = self.headers.get("Content-Length")
        if length_hdr is None:
            self._send_text(411, "length required")
            return
        try:
            length = int(length_hdr)
        except ValueError:
            self._send_text(400, "bad content-length")
            return
        if length < 0:
            self._send_text(400, "bad content-length")
            return
        qs = parse_qs(urlsplit(self.path).query)
        by = self._request_by(qs)
        replace = qs.get("replace") == ["1"]
        range_hdr = self.headers.get("Content-Range")
        if range_hdr:
            self._put_chunked(abs_path, rel, length, replace, range_hdr, by)
        else:
            self._put_regular(abs_path, rel, length, replace, by)

    def _put_regular(self, abs_path, rel, length, replace, by):
        if length > PUT_MAX:
            self._send_text(413, "file too large")
            return
        exists = abs_path.exists()
        if exists and not replace:
            self._send_text(409, "already exists (use ?replace=1)")
            return
        if exists and replace and _is_talk(rel):
            self._send_text(403, "_talk/ is append-only")
            return
        if exists and abs_path.is_dir():
            self._send_text(400, "cannot PUT a directory")
            return
        parent = abs_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._send_text(500, "cannot create directory")
            return
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(parent), prefix="." + abs_path.name + ".", suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                remaining = length
                while remaining > 0:
                    data = self.rfile.read(min(CHUNK, remaining))
                    if not data:
                        break
                    f.write(data)
                    remaining -= len(data)
                if remaining > 0:
                    raise IOError("short upload")
            if exists:
                _backup_to_replaced(abs_path, rel)
            os.replace(tmp, abs_path)
            tmp = None
            prune()
            _log_change(rel, "replace" if exists else "put", by)
            _maybe_generate_status(abs_path, rel)
            self._send_text(200 if exists else 201, "ok")
        except (OSError, IOError):
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            self._send_text(400, "upload failed")

    def _put_chunked(self, abs_path, rel, length, replace, range_hdr, by):
        m = re.match(r"bytes (\d+)-(\d+)/(\d+)$", range_hdr.strip())
        if not m:
            self._send_text(400, "bad content-range")
            return
        start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if total > PUT_MAX:
            self._send_text(413, "file too large")
            return
        if total <= 0 or start < 0 or end < start or end >= total:
            self._send_text(400, "bad content-range")
            return
        if length != end - start + 1:
            self._send_text(400, "content-length does not match content-range")
            return
        exists = abs_path.exists()
        if start == 0:
            if exists and not replace:
                self._send_text(409, "already exists (use ?replace=1)")
                return
            if exists and replace and _is_talk(rel):
                self._send_text(403, "_talk/ is append-only")
                return
        part = abs_path.parent / ("." + abs_path.name + ".part")
        if start == 0:
            try:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self._send_text(500, "cannot create directory")
                return
            mode = "wb"
        else:
            if not part.exists() or part.stat().st_size != start:
                self._send_text(409, "wrong offset, restart from zero")
                return
            mode = "ab"
        try:
            data = self._read_exact(length)
        except OSError:
            self._send_text(400, "upload failed")
            return
        if len(data) != length:
            self._send_text(400, "short upload")
            return
        try:
            with open(part, mode) as f:
                f.write(data)
            received = part.stat().st_size
        except OSError:
            self._send_text(500, "cannot write part")
            return
        if received < total:
            self._send_text(200, "received %d/%d" % (received, total))
            return
        if received > total:
            try:
                part.unlink()
            except OSError:
                pass
            self._send_text(400, "too much data")
            return
        try:
            if exists:
                _backup_to_replaced(abs_path, rel)
            os.replace(part, abs_path)
        except OSError:
            self._send_text(500, "cannot finish upload")
            return
        prune()
        _log_change(rel, "replace" if exists else "put", by)
        _maybe_generate_status(abs_path, rel)
        self._send_text(200 if exists else 201, "ok")

    # -- DELETE ------------------------------------------------------------
    def do_DELETE(self):
        try:
            abs_path, rel = _resolve(self.path)
        except ValueError:
            self._send_text(400, "bad path")
            return
        if _is_reserved(rel):
            self._send_text(403, "reserved path")
            return
        if _is_system(rel):
            self._send_text(403, "server-maintained file")
            return
        if _is_talk(rel):
            self._send_text(403, "_talk/ is append-only")
            return
        if not abs_path.exists():
            self._send_text(404, "not found")
            return
        if abs_path.is_dir():
            self._send_text(400, "cannot delete a directory")
            return
        try:
            _trash_to_deleted(abs_path, rel)
        except OSError:
            self._send_text(500, "cannot delete")
            return
        prune()
        qs = parse_qs(urlsplit(self.path).query)
        _log_change(rel, "delete", self._request_by(qs))
        self._send_text(200, "deleted %s (moved to .deleted/%s)" % (rel, rel))

    # -- MOVE --------------------------------------------------------------
    def do_MOVE(self):
        try:
            abs_path, rel = _resolve(self.path)
        except ValueError:
            self._send_text(400, "bad path")
            return
        qs = parse_qs(urlsplit(self.path).query)
        dest_raw = None
        if qs.get("to"):
            dest_raw = qs["to"][0]
        elif self.headers.get("Destination"):
            dest_raw = self.headers.get("Destination")
            if "://" in dest_raw:
                dest_raw = urlsplit(dest_raw).path
        if not dest_raw:
            self._send_text(400, "missing ?to= destination")
            return
        try:
            dest_abs, dest_rel = _resolve(dest_raw)
        except ValueError:
            self._send_text(400, "bad destination")
            return
        if _is_reserved(rel) or _is_reserved(dest_rel):
            self._send_text(403, "reserved path")
            return
        if _is_system(rel) or _is_system(dest_rel):
            self._send_text(403, "server-maintained file")
            return
        if _is_talk(rel) or _is_talk(dest_rel):
            self._send_text(403, "_talk/ is append-only")
            return
        if not abs_path.exists():
            self._send_text(404, "not found")
            return
        if abs_path.is_dir():
            self._send_text(400, "cannot move a directory")
            return
        replace = qs.get("replace") == ["1"]
        dest_exists = dest_abs.exists()
        if dest_exists and not replace:
            self._send_text(409, "destination exists (use ?replace=1)")
            return
        if dest_exists and dest_abs.is_dir():
            self._send_text(400, "destination is a directory")
            return
        try:
            dest_abs.parent.mkdir(parents=True, exist_ok=True)
            if dest_exists:
                _backup_to_replaced(dest_abs, dest_rel)
            os.replace(abs_path, dest_abs)
        except OSError:
            self._send_text(500, "cannot move")
            return
        prune()
        _log_change(dest_rel, "move", self._request_by(qs), {"from": rel})
        _maybe_generate_status(dest_abs, dest_rel)
        self._send_text(200, "moved %s -> %s" % (rel, dest_rel))

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        try:
            abs_path, rel = _resolve(self.path)
        except ValueError:
            self._send_text(400, "bad path")
            return
        qs = parse_qs(urlsplit(self.path).query)
        if qs.get("mkdir") == ["1"]:
            self._do_mkdir(abs_path, rel, qs)
            return
        if qs.get("append") == ["1"]:
            self._do_append(abs_path, rel, qs)
            return
        self._send_text(405, "use PUT for files, POST ?mkdir=1 for directories, POST ?append=1 to append a line")

    def _do_mkdir(self, abs_path, rel, qs):
        if _is_reserved(rel):
            self._send_text(403, "reserved path")
            return
        if abs_path.exists() and abs_path.is_file():
            self._send_text(409, "a file exists at that path")
            return
        if abs_path.exists() and abs_path.is_dir():
            self._send_text(200, "already exists")
            return
        try:
            abs_path.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._send_text(500, "cannot create directory")
            return
        _log_change(rel, "mkdir", self._request_by(qs))
        self._send_text(201, "created %s" % rel)

    def _do_append(self, abs_path, rel, qs):
        if _is_reserved(rel):
            self._send_text(403, "reserved path")
            return
        if rel == "changes.json":
            self._send_text(403, "changes.json is server-maintained")
            return
        if _is_talk(rel):
            self._send_text(403, "_talk/ is append-only")
            return
        if abs_path.is_dir() or urlsplit(self.path).path.endswith("/"):
            self._send_text(400, "cannot append to a directory")
            return
        length_hdr = self.headers.get("Content-Length")
        if length_hdr is None:
            self._send_text(411, "length required")
            return
        try:
            length = int(length_hdr)
        except ValueError:
            self._send_text(400, "bad content-length")
            return
        if length < 0:
            self._send_text(400, "bad content-length")
            return
        if length > APPEND_MAX:
            self._send_text(413, "line too large")
            return
        try:
            data = self._read_exact(length)
        except OSError:
            self._send_text(400, "upload failed")
            return
        if len(data) != length:
            self._send_text(400, "short upload")
            return
        if not data.endswith(b"\n"):
            data = data + b"\n"
        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._send_text(500, "cannot create directory")
            return
        try:
            with APPEND_LOCK:
                offset = abs_path.stat().st_size if abs_path.exists() else 0
                with open(abs_path, "ab") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                with open(abs_path, "rb") as f:
                    total = f.read()
                lines = total.count(b"\n")
                size = len(total)
        except OSError:
            self._send_text(500, "cannot append")
            return
        _log_change(rel, "append", self._request_by(qs))
        self._send_json(200, {"appended": True, "path": rel, "offset": offset, "lines": lines, "bytes": size})

    # -- error handling ----------------------------------------------------
    def handle_error(self, request, client_address):
        log("error handling %s from %s:\n%s" % (getattr(self, "requestline", "?"), client_address, traceback.format_exc()))

    def log_message(self, format, *args):
        log(format % args)


def _heartbeat():
    while True:
        time.sleep(HEARTBEAT_SECS)
        log("alive (pid %d)" % os.getpid())


def _is_local_ip(ip):
    if ip in ("0.0.0.0", "::"):
        return False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
            return True
        finally:
            s.close()
    except OSError:
        return False


def main(argv=None):
    global ROOT, LOG_PATH
    p = argparse.ArgumentParser(description="muse-files v%s - tailnet-only HTTP file server" % VERSION)
    p.add_argument("--bind", default="100.69.224.71")
    p.add_argument("--port", type=int, default=8788)
    p.add_argument("--root", default=r"C:\muse-relay\data\ftp")
    p.add_argument("--log", default=r"C:\muse-relay\muse_files.log")
    args = p.parse_args(argv)
    ROOT = Path(args.root).resolve()
    LOG_PATH = Path(args.log)
    try:
        ip = socket.gethostbyname(args.bind)
    except OSError:
        log("cannot resolve bind address %s" % args.bind)
        return 2
    if not _is_local_ip(ip):
        log("refusing to bind %s (not a local address)" % args.bind)
        return 2
    try:
        server = ThreadingHTTPServer((ip, args.port), Handler)
    except OSError as e:
        log("cannot bind %s:%d: %s" % (ip, args.port, e))
        return 1
    log("muse-files v%s up on http://%s:%d/ serving %s (pid %d)" % (VERSION, args.bind, args.port, ROOT, os.getpid()))
    prune()
    t = threading.Thread(target=_heartbeat, daemon=True)
    t.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("muse-files stopped")
    except BaseException:
        log("muse-files FATAL:\n" + traceback.format_exc())
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
