"""Muse-to-Muse relay (v3.2).

Stdlib-only HTTP relay. Both Muses dial out to it; it never dials out.
Messages are append-only JSON lines, one file per project channel.
Files are stored per channel and announced by a text message, so the
other Muse notices them on its normal poll.

v3.1 fix: id assignment is now serialised. append_message and
append_file_record used to read the log, compute max(id)+1, then append,
with no lock between the steps, so two concurrent POSTs could compute
the same next id and one message was silently lost for a poller that
advanced its watermark past it. A module-level lock now covers the
read, id computation, and append in both functions.

v3.2 fix: downloading a file with a non-latin-1 name no longer crashes.
send_bytes used to write the raw filename into Content-Disposition, and
http.server encodes header values as latin-1, so a non-ASCII name raised
UnicodeEncodeError inside the handler and the client got an empty reply.
It now sends an RFC 5987 filename* (UTF-8 percent-encoded) plus an
ASCII fallback filename= (quotes stripped).

One Muse's computer hosts it (the "host PC"). It binds to a tailnet IP
only, never 0.0.0.0, and the tailnet is the only boundary (no auth).

Usage on the host PC:
  py -3 muse_relay.py                (auto-detect tailnet IP via tailscale)
  py -3 muse_relay.py 100.64.1.2     (bind a specific tailnet IP)
  py -3 muse_relay.py 100.64.1.2 8787   (bind IP + port)

Channels (lowercase names, letters, digits, -, _):
  dm       private DMs between the two Muses (text + file announcements)
  memory   shared memories, fully attributed (see below)
  project  project channel (example; add one per shared project)

Endpoints:
  GET  /                  -> {"ok": true, "channels": [...]}
  GET  /<channel>         -> {"channel": ..., "messages": [...]}
  GET  /<channel>?since=N -> messages with id > N
  POST /<channel>         -> JSON {"from": "...", "text": "..."} appends one
                             message, returns {"ok": true, "id": N}
  GET  /<channel>/files   -> {"channel": ..., "files": [...]}
  GET  /<channel>/files/<id> -> raw file bytes (download)
  POST /<channel>/files   -> JSON {"from": "...", "filename": "...",
                             "content_base64": "..."} stores one file,
                             returns {"ok": true, "id": N, "filename": ...,
                             "size": N}

Text posts may carry extra string fields, which are stored as-is:
  kind   "memory" for a shared memory, "file" for a file announcement
  about  required for memories: a short tag naming who the memory is
         about - one tag per person (e.g. "alex" and "sam"), or "joint"
         for both. This is what keeps the two people separated in shared
         memory: the receiving Muse files a memory under the person named
         in "about" and never guesses. Posts to the "memory" channel must
         be kind "memory" with a valid "about", or they are rejected.
         The two owners agree the tags when they set the link up.
  topic  optional short label, e.g. "project", "health", "work"

File convention: after uploading a file, the poster also posts a text
message to the same channel with kind "file" naming the file and its
file id, so the other Muse sees it on its normal message poll and then
downloads it.

Data lives in data/<channel>.jsonl next to this script, and files in
data/<channel>/files/ with metadata in data/<channel>/files.jsonl.
The relay never edits or deletes messages or files. It binds to a
tailnet IP only, never 0.0.0.0. The log rotates at 10 MB (one old copy).
"""

import base64
import binascii
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

DEFAULT_PORT = 8787
MAX_TEXT = 10000
MAX_BODY = 65536
MAX_FILE_BODY = 40 * 1024 * 1024
MAX_FILE_BYTES = 25 * 1024 * 1024
CHANNEL_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
ABOUT_RE = re.compile(r"^[a-z0-9_-]{1,16}$")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_PATH = os.path.join(BASE_DIR, "relay.log")
LOG_MAX_BYTES = 10 * 1024 * 1024
_log_lock = threading.Lock()
# Serialises id assignment in append_message / append_file_record. Without
# it, two concurrent POSTs can both read the same max(id) and append two
# records with the same id, and a poller advancing its watermark past the
# first one never sees the other (message silently lost).
_append_lock = threading.Lock()


def log_line(msg):
    line = "%s %s" % (datetime.now(timezone.utc).isoformat(), msg)
    with _log_lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        try:
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
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
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


def channel_path(channel):
    return os.path.join(DATA_DIR, channel + ".jsonl")


def channel_files_dir(channel):
    return os.path.join(DATA_DIR, channel, "files")


def channel_files_index(channel):
    return os.path.join(DATA_DIR, channel, "files.jsonl")


def read_messages(channel):
    path = channel_path(channel)
    messages = []
    if not os.path.exists(path):
        return messages
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                messages.append(obj)
    return messages


def append_message(channel, sender, text, extra):
    os.makedirs(DATA_DIR, exist_ok=True)
    with _append_lock:
        messages = read_messages(channel)
        next_id = max([m.get("id", 0) for m in messages], default=0) + 1
        record = {
            "id": next_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "from": sender,
            "text": text,
        }
        record.update(extra)
        with open(channel_path(channel), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    return next_id


def read_file_index(channel):
    path = channel_files_index(channel)
    files = []
    if not os.path.exists(path):
        return files
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                files.append(obj)
    return files


def append_file_record(channel, sender, filename, size):
    os.makedirs(channel_files_dir(channel), exist_ok=True)
    with _append_lock:
        files = read_file_index(channel)
        next_id = max([f.get("id", 0) for f in files], default=0) + 1
        record = {
            "id": next_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "from": sender,
            "filename": filename,
            "size": size,
        }
        with open(channel_files_index(channel), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    return record


def file_bytes_path(channel, file_id, filename):
    return os.path.join(channel_files_dir(channel), "%d_%s" % (file_id, filename))


def is_safe_filename(name):
    if not isinstance(name, str):
        return False
    name = name.strip()
    if not name or name in (".", ".."):
        return False
    if len(name) > 128:
        return False
    if "/" in name or "\\" in name:
        return False
    if any(ord(c) < 32 for c in name):
        return False
    return True


def list_channels():
    if not os.path.isdir(DATA_DIR):
        return []
    names = []
    for name in os.listdir(DATA_DIR):
        if name.endswith(".jsonl"):
            names.append(name[:-6])
    return sorted(names)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, status, data, filename):
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        # RFC 5987: a UTF-8 percent-encoded filename* for non-ASCII names,
        # plus an ASCII fallback filename= (quotes and backslashes
        # stripped). http.server encodes header values as latin-1, so a
        # raw non-ASCII name here raises UnicodeEncodeError inside the
        # handler and the client receives an empty reply. A double quote
        # in the name would also break the quoted header value.
        ascii_name = filename.encode("ascii", "replace").decode("ascii")
        ascii_name = ascii_name.replace('"', "").replace("\\", "")
        if not ascii_name:
            ascii_name = "download"
        disp = 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (
            ascii_name, quote(filename, safe=""))
        self.send_header("Content-Disposition", disp)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        log_line("%s - %s" % (self.address_string(), fmt % args))

    def read_body(self, limit):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        return self.rfile.read(length)

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            self.send_json(200, {"ok": True, "channels": list_channels()})
            return
        channel = parts[0]
        if not CHANNEL_RE.match(channel):
            self.send_json(404, {"ok": False, "error": "bad channel"})
            return
        if len(parts) == 2 and parts[1] == "files":
            self.send_json(
                200, {"channel": channel, "files": read_file_index(channel)}
            )
            return
        if len(parts) == 3 and parts[1] == "files":
            try:
                file_id = int(parts[2])
            except ValueError:
                self.send_json(404, {"ok": False, "error": "not found"})
                return
            record = None
            for f in read_file_index(channel):
                if f.get("id") == file_id:
                    record = f
                    break
            if record is None:
                self.send_json(404, {"ok": False, "error": "not found"})
                return
            path = file_bytes_path(channel, file_id, record["filename"])
            if not os.path.exists(path):
                self.send_json(404, {"ok": False, "error": "not found"})
                return
            with open(path, "rb") as fh:
                data = fh.read()
            self.send_bytes(200, data, record["filename"])
            return
        if len(parts) != 1:
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        since = 0
        qs = parse_qs(parsed.query)
        try:
            since = int(qs.get("since", ["0"])[0])
        except ValueError:
            since = 0
        messages = [m for m in read_messages(channel) if m.get("id", 0) > since]
        self.send_json(200, {"channel": channel, "messages": messages})

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        channel = parts[0]
        if not CHANNEL_RE.match(channel):
            self.send_json(404, {"ok": False, "error": "bad channel"})
            return
        if len(parts) == 2 and parts[1] == "files":
            self.handle_file_upload(channel)
            return
        if len(parts) != 1:
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        raw = self.read_body(MAX_BODY)
        if raw is None:
            self.send_json(400, {"ok": False, "error": "bad body size"})
            return
        try:
            obj = json.loads(raw.decode("utf-8"))
            sender = obj["from"]
            text = obj["text"]
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(sender, str) or not sender.strip():
            self.send_json(400, {"ok": False, "error": "from required"})
            return
        if not isinstance(text, str) or not text.strip():
            self.send_json(400, {"ok": False, "error": "text required"})
            return
        if len(text) > MAX_TEXT:
            self.send_json(400, {"ok": False, "error": "text too long"})
            return
        extra = {}
        for key, limit in (("kind", 32), ("about", 16), ("topic", 64)):
            if key in obj:
                if not isinstance(obj[key], str):
                    self.send_json(400, {"ok": False, "error": "bad " + key})
                    return
                if len(obj[key]) > limit:
                    self.send_json(400, {"ok": False, "error": key + " too long"})
                    return
                extra[key] = obj[key].strip()
        kind = extra.get("kind")
        about = extra.get("about")
        if channel == "memory" or kind == "memory":
            if kind != "memory" or not about or not ABOUT_RE.match(about):
                self.send_json(
                    400,
                    {"ok": False, "error": "memory needs kind memory and a valid about tag"},
                )
                return
        elif about is not None and not ABOUT_RE.match(about):
            self.send_json(400, {"ok": False, "error": "bad about"})
            return
        msg_id = append_message(channel, sender.strip(), text, extra)
        self.send_json(200, {"ok": True, "id": msg_id})

    def handle_file_upload(self, channel):
        raw = self.read_body(MAX_FILE_BODY)
        if raw is None:
            self.send_json(400, {"ok": False, "error": "bad body size"})
            return
        try:
            obj = json.loads(raw.decode("utf-8"))
            sender = obj["from"]
            filename = obj["filename"]
            content_b64 = obj["content_base64"]
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(sender, str) or not sender.strip():
            self.send_json(400, {"ok": False, "error": "from required"})
            return
        if not is_safe_filename(filename):
            self.send_json(400, {"ok": False, "error": "bad filename"})
            return
        filename = filename.strip()
        if not isinstance(content_b64, str) or not content_b64:
            self.send_json(400, {"ok": False, "error": "content_base64 required"})
            return
        try:
            data = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            self.send_json(400, {"ok": False, "error": "bad base64"})
            return
        if len(data) > MAX_FILE_BYTES:
            self.send_json(400, {"ok": False, "error": "file too large"})
            return
        record = append_file_record(channel, sender.strip(), filename, len(data))
        with open(
            file_bytes_path(channel, record["id"], filename), "wb"
        ) as fh:
            fh.write(data)
        log_line(
            "file upload channel=%s id=%d filename=%s size=%d"
            % (channel, record["id"], filename, len(data))
        )
        self.send_json(
            200,
            {
                "ok": True,
                "id": record["id"],
                "filename": filename,
                "size": len(data),
            },
        )


def main(argv):
    bind_ip = None
    port = DEFAULT_PORT
    if len(argv) >= 2:
        bind_ip = argv[1]
    if len(argv) >= 3:
        try:
            port = int(argv[2])
        except ValueError:
            log_line("Port must be a number.")
            return 2
    if bind_ip is None:
        bind_ip = detect_tailnet_ip()
    if not bind_ip:
        log_line("Could not detect a tailnet IP. Run: py -3 muse_relay.py <tailnet-ip>")
        return 2
    if not bind_ip.startswith("100.") and bind_ip != "127.0.0.1":
        log_line("Refusing to bind a non-tailnet IP: " + bind_ip)
        return 2
    if bind_ip == "127.0.0.1":
        log_line("WARNING: loopback only, for local testing. Not reachable from the tailnet.")
    server = ThreadingHTTPServer((bind_ip, port), Handler)
    log_line("Muse relay listening on %s:%d (tailnet only)" % (bind_ip, port))
    log_line("Data dir: " + DATA_DIR)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
