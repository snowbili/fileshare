# -*- coding: utf-8 -*-
"""聚焦复现：LocalSend 流程之后服务是否还能接受新连接。

用法： python tests/repro_localsend.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
# ?????? Windows / ?????????????? GBK ????????
# ?? print ?? UnicodeEncodeError????????????????
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")
HOME = os.path.join(HERE, "tmp_repro")
PORT = 18111

def call(path, method="GET", data=None, headers=None, timeout=10):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        return -1, ("%s: %s" % (type(exc).__name__, exc)).encode("utf-8")

def main() -> int:
    shutil.rmtree(HOME, ignore_errors=True)
    os.makedirs(HOME, exist_ok=True)
    env = dict(os.environ)
    env["FILESHARE_HOME"] = HOME
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen([sys.executable, os.path.join(APP, "server.py"), "--port", str(PORT),
                             "--no-discovery", "--no-window"], env=env, cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            status, _ = call("/api/state")
            if status == 200:
                break
            time.sleep(0.3)
        token = open(os.path.join(HOME, "data", "token.txt"), encoding="utf-8").read().strip()
        print("1) state           ->", call("/api/state")[0])
        call("/api/settings", "POST", json.dumps({"auto_accept": True}).encode(),
             {"Content-Type": "application/json", "X-FS-Token": token})
        print("2) auto_accept     ->", call("/api/state")[0])

        info = {"alias": "PC-X", "version": "2.0", "deviceModel": "Windows", "deviceType": "desktop",
                "fingerprint": "f" * 32, "port": PORT, "protocol": "http", "download": True}
        prepare = {"info": info, "files": {"f1": {"id": "f1", "fileName": "t.txt", "size": 3,
                                                  "fileType": "text/plain", "sha256": None, "preview": None}}}
        status, body = call("/api/localsend/v2/prepare-upload", "POST",
                            json.dumps(prepare).encode(), {"Content-Type": "application/json"})
        print("3) prepare-upload  ->", status, body[:120].decode("utf-8", "replace"))
        result = json.loads(body.decode("utf-8"))
        session_id = result["sessionId"]
        file_token = result["files"]["f1"]
        print("4) state alive     ->", call("/api/state")[0])

        status, body = call("/api/localsend/v2/upload?sessionId=%s&fileId=f1&token=%s"
                            % (session_id, file_token), "POST", b"abc")
        print("5) upload          ->", status, body[:80].decode("utf-8", "replace"))
        print("6) state alive     ->", call("/api/state")[0])

        status, body = call("/api/localsend/v2/cancel?sessionId=%s" % session_id, "POST", b"")
        print("7) cancel          ->", status, body[:200].decode("utf-8", "replace"))

        status, body = call("/api/localsend/v2/prepare-download", "POST", b"")
        print("8) prepare-down    ->", status, body[:120].decode("utf-8", "replace"))
        status, body = call("/api/localsend/v2/download?fileId=t.txt")
        print("9) download        ->", status, body[:80].decode("utf-8", "replace"))
        print("10) state alive    ->", call("/api/state")[0])
    except Exception:
        traceback.print_exc()
    finally:
        time.sleep(0.5)
        try:
            proc.terminate()
        except Exception:
            pass
        time.sleep(0.5)
        try:
            proc.kill()
        except Exception:
            pass
        output = b""
        try:
            output = proc.stdout.read()
        except Exception:
            pass
        print("----- 服务端输出 -----")
        print(output.decode("utf-8", "replace").strip()[:3000])
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
