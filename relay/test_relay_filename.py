#!/usr/bin/env python3
"""Test for relay issue #2: downloading a file with a non-latin-1 name.

is_safe_filename accepts non-ASCII names, so a Japanese-named PDF uploads
fine. But send_bytes used to write the raw name into a Content-Disposition
header, and http.server encodes header values as latin-1, raising
UnicodeEncodeError inside the handler so the client got an empty reply.
A double quote in the name also broke the quoted header value.

The fix sends an RFC 5987 filename* (UTF-8 percent-encoded) plus an
ASCII fallback filename= (quotes stripped).

Usage:
  python3 test_relay_filename.py <path-to-muse_relay.py>
"""
import base64
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

PORT = 18788


def req(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    c.request(method, path, body=body,
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    hdrs = dict(r.getheaders())
    c.close()
    return r.status, data, hdrs


def main():
    src = Path(sys.argv[1])
    tmp = Path(tempfile.mkdtemp(prefix="relay-filename-test-"))
    script = tmp / "muse_relay.py"
    shutil.copy(src, script)
    proc = subprocess.Popen(
        [sys.executable, str(script), "127.0.0.1", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = []
    try:
        up = False
        for _ in range(50):
            try:
                s, _, _ = req("GET", "/")
                if s == 200:
                    up = True
                    break
            except OSError:
                time.sleep(0.1)
        if not up:
            print("FAIL: relay did not start")
            return 1

        cases = [
            # Japanese filename, written as escapes so this file stays ASCII.
            ("\u30ec\u30dd\u30fc\u30c8.pdf", b"japanese pdf bytes"),
            ('a"b.txt', b"quoted name bytes"),
            ("plain.txt", b"plain bytes"),
        ]
        ok_all = True
        for name, content in cases:
            payload = base64.b64encode(content).decode()
            s, data, _ = req("POST", "/dm/files", body=json.dumps(
                {"from": "tester", "filename": name,
                 "content_base64": payload}).encode())
            if s != 200:
                print("FAIL upload %r: HTTP %d" % (name, s))
                ok_all = False
                continue
            file_id = json.loads(data)["id"]
            s, data, hdrs = req("GET", "/dm/files/%d" % file_id)
            disp = hdrs.get("Content-Disposition", "")
            expected_star = "filename*=UTF-8''" + quote(name, safe="")
            ok = (
                s == 200
                and data == content
                and expected_star in disp
                and 'filename="' in disp
                and disp.count('"') == 2
            )
            print(("PASS " if ok else "FAIL ") + "download %r -> %d, disposition=%s" % (
                name, s, disp))
            if not ok:
                ok_all = False
        print("\n%s" % ("ALL PASS" if ok_all else "FAILED"))
        return 0 if ok_all else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
