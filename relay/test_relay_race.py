#!/usr/bin/env python3
"""Concurrency test for the relay id-assignment race (issue #1).

Spawns many threads that POST to the same channel at the same moment and
checks every returned id is unique with no gaps. Without the lock, two
threads can both compute the same next_id and one message is silently
lost for a poller that advances its watermark past it.

Usage:
  python3 test_relay_race.py <path-to-muse_relay.py>
"""
import base64
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PORT = 18787
N_MSG = 30
N_FILE = 20


def req(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    c.request(method, path, body=body,
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def main():
    src = Path(sys.argv[1])
    tmp = Path(tempfile.mkdtemp(prefix="relay-race-test-"))
    script = tmp / "muse_relay.py"
    shutil.copy(src, script)
    proc = subprocess.Popen(
        [sys.executable, str(script), "127.0.0.1", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(50):
            try:
                s, _ = req("GET", "/")
                if s == 200:
                    up = True
                    break
            except OSError:
                time.sleep(0.1)
        if not up:
            print("FAIL: relay did not start")
            return 1

        # --- concurrent message POSTs -------------------------------------
        msg_ids = []
        msg_lock = threading.Lock()

        def post_msg(i):
            s, data = req("POST", "/dm", body=json.dumps(
                {"from": "tester", "text": "message %d" % i}).encode())
            if s == 200:
                with msg_lock:
                    msg_ids.append(json.loads(data)["id"])

        threads = [threading.Thread(target=post_msg, args=(i,)) for i in range(N_MSG)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dupes = len(msg_ids) - len(set(msg_ids))
        gaps = sorted(set(range(1, N_MSG + 1)) - set(msg_ids))
        print("messages: %d posted, %d unique ids, %d duplicates, gaps=%s" % (
            len(msg_ids), len(set(msg_ids)), dupes, gaps))
        ok_msg = len(msg_ids) == N_MSG and dupes == 0 and not gaps
        print(("PASS " if ok_msg else "FAIL ") + "concurrent message ids unique, no gaps")

        s, data = req("GET", "/dm")
        try:
            stored = json.loads(data)["messages"]
            ok_read = len(stored) == N_MSG and len({m["id"] for m in stored}) == N_MSG
        except Exception:
            ok_read = False
        print(("PASS " if ok_read else "FAIL ") + "all %d messages readable via GET" % N_MSG)

        # --- concurrent file uploads --------------------------------------
        file_ids = []
        file_lock = threading.Lock()

        def post_file(i):
            payload = base64.b64encode(b"file content %d" % i).decode()
            s, data = req("POST", "/dm/files", body=json.dumps(
                {"from": "tester", "filename": "f%d.txt" % i,
                 "content_base64": payload}).encode())
            if s == 200:
                with file_lock:
                    file_ids.append(json.loads(data)["id"])

        threads = [threading.Thread(target=post_file, args=(i,)) for i in range(N_FILE)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dupes = len(file_ids) - len(set(file_ids))
        gaps = sorted(set(range(1, N_FILE + 1)) - set(file_ids))
        print("files: %d posted, %d unique ids, %d duplicates, gaps=%s" % (
            len(file_ids), len(set(file_ids)), dupes, gaps))
        ok_file = len(file_ids) == N_FILE and dupes == 0 and not gaps
        print(("PASS " if ok_file else "FAIL ") + "concurrent file ids unique, no gaps")

        ok = ok_msg and ok_read and ok_file
        print("\n%s" % ("ALL PASS - no duplicate ids" if ok else "FAILED - duplicate ids found"))
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
