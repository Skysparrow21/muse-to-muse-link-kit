#!/usr/bin/env python3
"""Full regression harness for muse-files v5. Starts the server on loopback,
runs the v4 regression battery plus the new v5 DELETE / MOVE / mkdir /
chunked / rotation / prune tests, and reports pass/fail."""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
SERVER = HERE / "muse_files.py"
PORT = 18788
BASE = ("127.0.0.1", PORT)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ((" -- " + detail) if detail and not ok else ""))


def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection(*BASE, timeout=15)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    hdrs = dict(r.getheaders())
    c.close()
    return r.status, data, hdrs


def main():
    tmp = Path(tempfile.mkdtemp(prefix="musefiles-v5-test-"))
    root = tmp / "root"
    (root / "sub").mkdir(parents=True)
    (root / "_talk").mkdir(parents=True)
    (root / "Violet").mkdir(parents=True)
    (root / "A.txt").write_bytes(b"hello world")
    (root / "sub" / "B.txt").write_bytes(b"bee")
    (root / "_talk" / "note.md").write_text("talk note")
    (root / "Violet" / "STATUS.md").write_text("violet status")
    (root / "range.txt").write_bytes(b"0123456789ABCDEF")
    # pre-seed old files for the startup prune test
    old_rep = root / ".replaced" / "old.txt"
    old_rep.parent.mkdir(parents=True, exist_ok=True)
    old_rep.write_text("old backup")
    old_part = root / ".stale.part"
    old_part.write_text("stale part")
    ancient = time.time() - 40 * 86400
    os.utime(old_rep, (ancient, ancient))
    os.utime(old_part, (ancient, ancient))
    logpath = tmp / "test.log"

    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--bind", "127.0.0.1", "--port", str(PORT),
         "--root", str(root), "--log", str(logpath)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # wait for the server
        up = False
        for _ in range(50):
            try:
                s, _, _ = req("GET", "/")
                if s == 200:
                    up = True
                    break
            except OSError:
                time.sleep(0.1)
        check("server starts and answers", up)
        if not up:
            return 1

        # --- v4 / v2.1 regression -------------------------------------
        s, body, h = req("GET", "/")
        check("GET / HTML 200", s == 200 and b"muse-files v5.0" in body and b"A.txt" in body)
        s, body, h = req("GET", "/?format=json")
        try:
            d = json.loads(body)
            ok = s == 200 and d.get("version") == "5.0" and any(
                e["name"] == "A.txt" and e["type"] == "file" for e in d["entries"])
            ok = ok and not any(e["name"] in (".replaced", ".deleted") for e in d["entries"])
            ok = ok and any(e["name"] == "sub" and e["type"] == "dir" for e in d["entries"])
        except Exception as e:
            ok = False
        check("GET / JSON listing (version, entries, reserved hidden)", ok)
        check("Server header is muse-files/5.0", h.get("Server") == "muse-files/5.0", str(h.get("Server")))
        s, body, h = req("GET", "/sub/")
        check("GET /sub/ HTML 200", s == 200 and b"B.txt" in body)
        s, body, h = req("GET", "/A.txt")
        check("GET file 200 + content", s == 200 and body == b"hello world")
        s, body, h = req("GET", "/missing.txt")
        check("GET missing 404", s == 404)
        s, body, h = req("GET", "/../secret")
        check("GET traversal 400", s == 400)
        s, body, h = req("GET", "/%2e%2e/secret")
        check("GET encoded traversal 400", s == 400)
        s, body, h = req("PUT", "/new.txt", body=b"abc")
        check("PUT new file 201", s == 201)
        s, body, h = req("GET", "/new.txt")
        check("GET new file back", s == 200 and body == b"abc")
        s, body, h = req("PUT", "/A.txt", body=b"nope")
        check("PUT existing 409", s == 409)
        s, body, h = req("GET", "/A.txt")
        check("PUT 409 leaves content alone", s == 200 and body == b"hello world")
        s, body, h = req("PUT", "/A.txt?replace=1", body=b"HELLO")
        check("PUT ?replace=1 200", s == 200)
        s, body, h = req("GET", "/A.txt")
        check("replace updates content", s == 200 and body == b"HELLO")
        s, body, h = req("GET", "/.replaced/A.txt")
        check("old version kept under .replaced/", s == 200 and body == b"hello world")
        s, body, h = req("GET", "/?format=json")
        try:
            d = json.loads(body)
            ok = not any(e["name"] == ".replaced" for e in d["entries"])
        except Exception:
            ok = False
        check(".replaced/ hidden from listing", ok)
        s, body, h = req("PUT", "/_talk/note.md?replace=1", body=b"x")
        check("PUT ?replace=1 in _talk/ 403", s == 403)
        s, body, h = req("PUT", "/empty.txt", body=b"")
        check("PUT empty file 201 (v5 allows 0-byte)", s == 201)
        s, body, h = req("GET", "/empty.txt")
        check("GET empty file 200, 0 bytes", s == 200 and body == b"")
        s, body, h = req("PUT", "/sub/", body=b"x")
        check("PUT to a directory path 400", s == 400)
        # PUT with no Content-Length at all (raw socket: http.client adds 0)
        import socket as _socket
        _s = _socket.create_connection(BASE, timeout=15)
        _s.sendall(b"PUT /nocl.txt HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        _resp = _s.recv(4096).decode(errors="replace")
        _s.close()
        _status = _resp.splitlines()[0] if _resp else ""
        check("PUT no Content-Length 411", " 411 " in _status, _status)
        # Range
        s, body, h = req("GET", "/range.txt", headers={"Range": "bytes=0-9"})
        check("Range 0-9 -> 206", s == 206 and body == b"0123456789", str(s))
        s, body, h = req("GET", "/range.txt", headers={"Range": "bytes=5-3"})
        check("Range past-end 416", s == 416, str(s))
        s, body, h = req("GET", "/range.txt", headers={"Range": "bytes=100-200"})
        check("Range beyond size 416", s == 416, str(s))
        s, body, h = req("GET", "/range.txt", headers={"Range": "bytes=-5"})
        check("Range suffix -5 -> 206", s == 206 and body == b"BCDEF", str(s))
        s, body, h = req("GET", "/range.txt", headers={"Range": "bytes=abc"})
        check("Range malformed -> 200 full", s == 200 and body == b"0123456789ABCDEF", str(s))
        s, body, h = req("GET", "/range.txt", headers={"Range": "bytes=10-"})
        check("Range open-ended 206", s == 206 and body == b"ABCDEF", str(s))
        # Zip
        s, body, h = req("GET", "/sub/?format=zip")
        ok = s == 200 and h.get("Content-Type") == "application/zip"
        if ok:
            zp = tmp / "sub.zip"
            zp.write_bytes(body)
            with zipfile.ZipFile(zp) as zf:
                ok = zf.read("B.txt") == b"bee"
        check("GET folder zip 200 + correct contents", ok, str(s))
        # Prune at startup
        check("startup prune removes old .replaced/ file", not old_rep.exists())
        check("startup prune removes old .part file", not old_part.exists())

        # --- v5: DELETE --------------------------------------------------
        s, body, h = req("DELETE", "/new.txt")
        check("DELETE file 200", s == 200 and b".deleted/" in body, str(s))
        s, body, h = req("GET", "/new.txt")
        check("DELETE removes the file", s == 404)
        s, body, h = req("GET", "/.deleted/new.txt")
        check("deleted file recoverable under .deleted/", s == 200 and body == b"abc")
        s, body, h = req("GET", "/?format=json")
        try:
            d = json.loads(body)
            ok = not any(e["name"] == ".deleted" for e in d["entries"])
        except Exception:
            ok = False
        check(".deleted/ hidden from listing", ok)
        s, body, h = req("DELETE", "/missing.txt")
        check("DELETE missing 404", s == 404)
        s, body, h = req("DELETE", "/_talk/note.md")
        check("DELETE in _talk/ 403", s == 403)
        s, body, h = req("GET", "/_talk/note.md")
        check("_talk/ note survives DELETE attempt", s == 200 and body == b"talk note")
        s, body, h = req("DELETE", "/sub")
        check("DELETE a directory 400", s == 400)

        # --- v5: MOVE -----------------------------------------------------
        s, body, h = req("MOVE", "/range.txt?to=/moved.txt")
        check("MOVE file 200", s == 200, str(s))
        s, body, h = req("GET", "/range.txt")
        check("MOVE removes the source", s == 404)
        s, body, h = req("GET", "/moved.txt")
        check("MOVE creates the destination", s == 200 and body == b"0123456789ABCDEF")
        s, body, h = req("MOVE", "/moved.txt?to=/A.txt")
        check("MOVE to existing dest 409", s == 409)
        s, body, h = req("GET", "/A.txt")
        check("MOVE 409 leaves dest alone", s == 200 and body == b"HELLO")
        s, body, h = req("MOVE", "/moved.txt?to=/A.txt&replace=1")
        check("MOVE ?replace=1 200", s == 200)
        s, body, h = req("GET", "/A.txt")
        check("MOVE replace updates dest", s == 200 and body == b"0123456789ABCDEF")
        s, body, h = req("GET", "/.replaced/A.txt")
        check("MOVE replace keeps old dest under .replaced/", s == 200 and body == b"HELLO")
        s, body, h = req("MOVE", "/missing.txt?to=/x.txt")
        check("MOVE missing source 404", s == 404)
        s, body, h = req("MOVE", "/_talk/note.md?to=/note2.md")
        check("MOVE from _talk/ 403", s == 403)
        s, body, h = req("MOVE", "/A.txt?to=/_talk/note2.md")
        check("MOVE to _talk/ 403", s == 403)
        s, body, h = req("MOVE", "/A.txt")
        check("MOVE without ?to= 400", s == 400)
        s, body, h = req("MOVE", "/sub?to=/sub2")
        check("MOVE a directory 400", s == 400)

        # --- v5: mkdir ----------------------------------------------------
        s, body, h = req("POST", "/newdir/?mkdir=1")
        check("POST mkdir 201", s == 201, str(s))
        s, body, h = req("GET", "/?format=json")
        try:
            d = json.loads(body)
            ok = any(e["name"] == "newdir" and e["type"] == "dir" for e in d["entries"])
        except Exception:
            ok = False
        check("mkdir appears in listing", ok)
        s, body, h = req("POST", "/newdir/?mkdir=1")
        check("POST mkdir again 200", s == 200, str(s))
        s, body, h = req("POST", "/A.txt?mkdir=1")
        check("POST mkdir where a file is 409", s == 409)
        s, body, h = req("POST", "/x/")
        check("POST without ?mkdir=1 405", s == 405)
        s, body, h = req("PUT", "/newdir/nested/deep.txt", body=b"deep")
        check("PUT creates parent dirs", s == 201)
        s, body, h = req("GET", "/newdir/nested/deep.txt")
        check("PUT nested file readable", s == 200 and body == b"deep")

        # --- v5: chunked upload -------------------------------------------
        s, body, h = req("PUT", "/big.bin", body=b"01234",
                          headers={"Content-Range": "bytes 0-4/10"})
        check("chunked first chunk 200 received", s == 200 and body == b"received 5/10", str(s) + " " + body.decode(errors="replace"))
        s, body, h = req("PUT", "/big.bin", body=b"56789",
                          headers={"Content-Range": "bytes 5-9/10"})
        check("chunked final chunk 201", s == 201, str(s))
        s, body, h = req("GET", "/big.bin")
        check("chunked file complete", s == 200 and body == b"0123456789")
        s, body, h = req("PUT", "/big2.bin", body=b"xxxxx",
                          headers={"Content-Range": "bytes 3-7/10"})
        check("chunked wrong offset 409", s == 409, str(s))
        s, body, h = req("PUT", "/big3.bin", body=b"xxxxx",
                          headers={"Content-Range": "bytes 0-4/2000000000"})
        check("chunked total over 1 GB 413", s == 413, str(s))
        s, body, h = req("GET", "/?format=json")
        try:
            d = json.loads(body)
            ok = not any(e["name"].endswith(".part") for e in d["entries"])
        except Exception:
            ok = False
        check(".part files hidden from listing", ok)

        # --- bind safety ----------------------------------------------------
        for bind in ("0.0.0.0", "8.8.8.8"):
            r = subprocess.run(
                [sys.executable, str(SERVER), "--bind", bind, "--port", "18789",
                 "--root", str(root), "--log", str(tmp / "bind.log")],
                capture_output=True, timeout=15)
            check("bind %s refused (exit 2)" % bind, r.returncode == 2, str(r.returncode))

        # --- log rotation (unit) ---------------------------------------------
        sys.path.insert(0, str(HERE))
        import muse_files
        rot_log = tmp / "rot.log"
        muse_files.LOG_PATH = rot_log
        muse_files.LOG_ROTATE_BYTES = 100
        rot_log.write_text("x" * 200)
        muse_files.log("rotation trigger line")
        muse_files.log("after rotation")
        check("log rotation creates .1 copy", (tmp / "rot.log.1").exists())
        check("log rotation keeps writing the live log", rot_log.exists() and b"after rotation" in rot_log.read_bytes())

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in RESULTS if not r[1]]
    print("\n%d checks, %d passed, %d failed" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    for name, ok, detail in failed:
        print("FAILED: " + name + (" -- " + detail if detail else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
