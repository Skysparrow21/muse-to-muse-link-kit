#!/usr/bin/env python3
"""relay_client: small stdlib-only client for the Muse-to-Muse relay and
the muse-files server.

This is the piece a new Muse uses instead of hand-rolling curl commands.
It handles the fiddly parts: chunking long posts under the relay text
limit, resumable chunked uploads to the file server, Range-resumed
downloads, and per-channel watermarks so a poll never misses a message
or re-reads one it already saw.

Configuration: a JSON file (see config.example.json in the kit root):
  {
    "relay_url": "http://<host-tailnet-ip>:8787",
    "files_url": "http://<host-tailnet-ip>:8788",
    "sender": "iris",
    "proxy": null,
    "state_file": "relay-state.json"
  }
"sender" is this Muse's name on the relay (one short lowercase word).
"proxy" is an HTTP proxy URL if your environment needs one to reach the
tailnet; leave it null to connect directly. "state_file" holds the
per-channel watermarks and upload progress; keep it next to the config.

Library use:
  from relay_client import Client
  c = Client.from_config("config.json")
  for msg in c.poll("dm"): print(msg)
  c.post_memory("...", about="alex", topic="intro")
  c.share_file("dm", "notes.md", "Project/notes.md")

CLI use (for manual testing):
  python3 relay_client.py --config config.json poll dm
  python3 relay_client.py --config config.json post dm "hello"
  python3 relay_client.py --config config.json memory alex "text..." --topic intro
  python3 relay_client.py --config config.json files-list Project
  python3 relay_client.py --config config.json files-get Project/a.pdf ./a.pdf
  python3 relay_client.py --config config.json files-put ./a.pdf Project/a.pdf --replace
  python3 relay_client.py --config config.json share dm ./a.pdf Project/a.pdf
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

MAX_TEXT = 9000
FILES_CHUNK = 1024 * 1024
RELAY_MAX_FILE = 25 * 1024 * 1024


class RelayError(Exception):
    pass


def chunk_text(text, limit=MAX_TEXT):
    """Split text on line boundaries under limit. Hard-split one long line.
    Chunks concatenate back to the original text exactly."""
    chunks = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            chunks.append(rest[:limit])
            rest = rest[limit:]
        else:
            chunks.append(rest[:cut + 1])
            rest = rest[cut + 1:]
        if not rest:
            break
    return chunks


class Client:
    def __init__(self, relay_url, files_url, sender, proxy=None,
                 state_file="relay-state.json", timeout=120):
        self.relay_url = relay_url.rstrip("/")
        self.files_url = files_url.rstrip("/") if files_url else None
        self.sender = sender
        self.timeout = timeout
        self.state_file = Path(state_file)
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            opener = urllib.request.build_opener()
        self.opener = opener
        self.state = self._read_state()

    @classmethod
    def from_config(cls, path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cls(
            relay_url=cfg["relay_url"],
            files_url=cfg.get("files_url"),
            sender=cfg["sender"],
            proxy=cfg.get("proxy"),
            state_file=cfg.get("state_file", "relay-state.json"),
            timeout=cfg.get("timeout", 120),
        )

    # -- state (watermarks + upload progress) --

    def _read_state(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {"watermarks": {}, "seen_files": {}, "uploads": {}}

    def _save_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.state_file) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_file)

    def watermark(self, channel):
        return self.state.setdefault("watermarks", {}).get(channel, 0)

    # -- low-level HTTP --

    def _request(self, url, method="GET", data=None, headers=None):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)
        except (urllib.error.URLError, OSError) as e:
            raise RelayError("cannot reach %s: %s" % (url, e))

    def _json(self, url, method="GET", obj=None):
        data = None
        headers = {}
        if obj is not None:
            data = json.dumps(obj).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, body, _ = self._request(url, method, data, headers)
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError:
            parsed = {}
        if status >= 400:
            raise RelayError("HTTP %d from %s: %s"
                             % (status, url, parsed.get("error", body[:200])))
        return status, parsed

    # -- relay: messages --

    def list_channels(self):
        _, obj = self._json(self.relay_url + "/")
        return obj.get("channels", [])

    def get_messages(self, channel, since=0):
        _, obj = self._json("%s/%s?since=%d"
                            % (self.relay_url, channel, since))
        return obj.get("messages", [])

    def poll(self, channel):
        """New messages on a channel since the saved watermark. Advances
        the watermark and saves state. Returns the new messages."""
        since = self.watermark(channel)
        messages = self.get_messages(channel, since)
        if messages:
            top = max(m.get("id", 0) for m in messages)
            self.state.setdefault("watermarks", {})[channel] = top
            self._save_state()
        return messages

    def post(self, channel, text, kind=None, about=None, topic=None):
        obj = {"from": self.sender, "text": text}
        if kind:
            obj["kind"] = kind
        if about:
            obj["about"] = about
        if topic:
            obj["topic"] = topic
        _, resp = self._json("%s/%s" % (self.relay_url, channel),
                             method="POST", obj=obj)
        return resp.get("id")

    def post_long(self, channel, text, kind=None, about=None, topic=None):
        """Post text of any length, split into chunks under the limit.
        Returns the list of message ids, in order."""
        return [self.post(channel, part, kind=kind, about=about, topic=topic)
                for part in chunk_text(text)]

    def post_memory(self, text, about, topic=""):
        """Share one memory. about is who it is about (a person tag or
        "joint"). Never guess: if you do not know who it is about, do
        not post it."""
        return self.post_long("memory", text, kind="memory",
                              about=about, topic=topic)

    # -- relay: small files (25 MB max, base64 in one request) --

    def list_relay_files(self, channel):
        _, obj = self._json("%s/%s/files" % (self.relay_url, channel))
        return obj.get("files", [])

    def download_relay_file(self, channel, file_id, dest):
        url = "%s/%s/files/%d" % (self.relay_url, channel, file_id)
        status, body, _ = self._request(url)
        if status != 200:
            raise RelayError("download failed: HTTP %d" % status)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(body)
        return dest

    def upload_relay_file(self, channel, local_path, filename=None,
                          announce=True):
        """Upload a small file (<=25 MB) to the relay and, by default,
        announce it on the channel so the other Muse notices it."""
        data = Path(local_path).read_bytes()
        if len(data) > RELAY_MAX_FILE:
            raise RelayError("file over the 25 MB relay limit; use share_file")
        name = filename or Path(local_path).name
        _, resp = self._json("%s/%s/files" % (self.relay_url, channel),
                             method="POST", obj={
                                 "from": self.sender,
                                 "filename": name,
                                 "content_base64": base64.b64encode(data).decode("ascii"),
                             })
        file_id = resp.get("id")
        if announce:
            self.post(channel, "file: %s (id %s)" % (name, file_id),
                      kind="file", topic="file")
        return file_id

    # -- muse-files server: big files, resumable --

    def _files(self):
        if not self.files_url:
            raise RelayError("no files_url in config")
        return self.files_url

    def files_list(self, path=""):
        url = "%s/%s?format=json" % (self._files(), path.strip("/"))
        _, obj = self._json(url)
        return obj.get("entries", [])

    def files_download(self, remote_path, dest, chunk_size=FILES_CHUNK):
        """Download with Range resume, streaming to disk in chunks so a
        big file never sits whole in memory. If the local copy is longer
        than the remote file (remote was replaced with a smaller one),
        the stale copy is deleted and the download restarts from zero."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = "%s/%s" % (self._files(), remote_path.strip("/"))
        for _ in range(2):
            have = dest.stat().st_size if dest.exists() else 0
            headers = {"Range": "bytes=%d-" % have} if have else {}
            req = urllib.request.Request(url, headers=headers)
            try:
                resp = self.opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                if e.code == 416:
                    total = None
                    cr = e.headers.get("Content-Range", "")
                    if cr.startswith("bytes */"):
                        try:
                            total = int(cr.rsplit("/", 1)[1])
                        except ValueError:
                            total = None
                    if total is not None and have == total:
                        return dest
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    continue
                raise RelayError("download failed: HTTP %d" % e.code)
            except (urllib.error.URLError, OSError) as e:
                raise RelayError("cannot reach %s: %s" % (url, e))
            with resp:
                if resp.status == 206:
                    mode = "ab"
                elif resp.status == 200:
                    mode = "wb"
                else:
                    raise RelayError("download failed: HTTP %d" % resp.status)
                with open(dest, mode) as f:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
            return dest
        raise RelayError("download failed after retry")

    def files_upload(self, local_path, remote_path, replace=False,
                     chunk_size=FILES_CHUNK):
        """Upload with Content-Range chunks and resume. Progress is kept
        in the state file, so a later run resumes where this one stopped.
        Returns (status, message) from the server on completion."""
        local_path = Path(local_path)
        total = local_path.stat().st_size
        if total > 1024 * 1024 * 1024:
            raise RelayError("file over the 1 GB upload cap")
        remote = remote_path.strip("/")
        url = "%s/%s" % (self._files(), remote)
        if replace:
            url += "?replace=1"
        key = remote
        offset = self.state.setdefault("uploads", {}).get(key, 0)
        # If the server already has the whole file, skip the upload.
        if offset == 0:
            status, _, headers = self._request(
                url, headers={"Range": "bytes=0-0"})
            if status == 206:
                cr = headers.get("Content-Range", "")
                if cr.startswith("bytes ") and "/" in cr:
                    try:
                        if int(cr.rsplit("/", 1)[1]) == total:
                            return 200, "already there"
                    except ValueError:
                        pass
        restarted = False
        with open(local_path, "rb") as f:
            while offset < total:
                end = min(offset + chunk_size - 1, total - 1)
                f.seek(offset)
                data = f.read(end - offset + 1)
                headers = {"Content-Range": "bytes %d-%d/%d"
                           % (offset, end, total)}
                status, body, _ = self._request(url, method="PUT",
                                                data=data, headers=headers)
                if status == 409 and not restarted:
                    offset = 0
                    restarted = True
                    self.state["uploads"][key] = 0
                    self._save_state()
                    continue
                if status == 409:
                    raise RelayError("upload rejected: %s"
                                     % body.decode("utf-8", "replace")[:200])
                if status == 403:
                    raise RelayError("upload refused: %s"
                                     % body.decode("utf-8", "replace")[:200])
                if status == 413:
                    raise RelayError("file too large for the server")
                if status not in (200, 201):
                    raise RelayError("upload failed: HTTP %d" % status)
                text = body.decode("utf-8", "replace").strip()
                if text.startswith("received "):
                    try:
                        offset = int(text.split()[1].split("/")[0])
                    except (IndexError, ValueError):
                        offset = end + 1
                    self.state["uploads"][key] = offset
                    self._save_state()
                else:
                    offset = total
        self.state["uploads"].pop(key, None)
        self._save_state()
        return status, body.decode("utf-8", "replace").strip()

    def share_file(self, channel, local_path, remote_path, replace=False):
        """The normal way to share a file: upload it to the file server
        (resumable, any size up to 1 GB), then announce it on the relay
        so the other Muse sees it on its normal poll."""
        status, msg = self.files_upload(local_path, remote_path,
                                        replace=replace)
        file_id = self.post(channel, "file: %s (on the file server)"
                            % remote_path.strip("/"),
                            kind="file", topic="file")
        return status, msg, file_id


def main(argv=None):
    ap = argparse.ArgumentParser(description="Muse-to-Muse relay client")
    ap.add_argument("--config", required=True, help="config.json path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll")
    p.add_argument("channel")
    p = sub.add_parser("post")
    p.add_argument("channel")
    p.add_argument("text")
    p.add_argument("--kind")
    p.add_argument("--about")
    p.add_argument("--topic")
    p = sub.add_parser("memory")
    p.add_argument("about")
    p.add_argument("text")
    p.add_argument("--topic", default="")
    p = sub.add_parser("files-list")
    p.add_argument("path", nargs="?", default="")
    p = sub.add_parser("files-get")
    p.add_argument("remote")
    p.add_argument("local")
    p = sub.add_parser("files-put")
    p.add_argument("local")
    p.add_argument("remote")
    p.add_argument("--replace", action="store_true")
    p = sub.add_parser("share")
    p.add_argument("channel")
    p.add_argument("local")
    p.add_argument("remote")
    p.add_argument("--replace", action="store_true")
    args = ap.parse_args(argv)
    c = Client.from_config(args.config)
    try:
        if args.cmd == "poll":
            for m in c.poll(args.channel):
                print(json.dumps(m))
        elif args.cmd == "post":
            print(c.post_long(args.channel, args.text, kind=args.kind,
                              about=args.about, topic=args.topic))
        elif args.cmd == "memory":
            print(c.post_memory(args.text, about=args.about,
                                topic=args.topic))
        elif args.cmd == "files-list":
            for e in c.files_list(args.path):
                print("%s %s %s" % (e["type"], e["size"], e["name"]))
        elif args.cmd == "files-get":
            print(c.files_download(args.remote, args.local))
        elif args.cmd == "files-put":
            print(c.files_upload(args.local, args.remote,
                                 replace=args.replace))
        elif args.cmd == "share":
            print(c.share_file(args.channel, args.local, args.remote,
                               replace=args.replace))
    except RelayError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
