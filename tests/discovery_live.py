# -*- coding: utf-8 -*-
"""真实组播发现验证：在真实网卡上起两个实例，检查它们能否自动互相发现。

（离线自检 run_checks.py 用 --no-discovery + 手动 register，覆盖不到组播这条路径。）

用法： python tests/discovery_live.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
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
TMP = os.path.join(HERE, "tmp_live")
PORT_A = 18121
PORT_B = 18122

def state(port):
    url = "http://127.0.0.1:%d/api/state" % port
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return {}

def start(home, port, alias):
    env = dict(os.environ)
    env["FILESHARE_HOME"] = home
    env["PYTHONIOENCODING"] = "utf-8"
    os.makedirs(home, exist_ok=True)
    log_path = os.path.join(TMP, "%s.log" % alias)
    handle = open(log_path, "wb")
    proc = subprocess.Popen([sys.executable, os.path.join(APP, "server.py"), "--port", str(port),
                             "--alias", alias, "--no-window"],
                            env=env, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
    return proc, log_path, handle

def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    procs = []
    try:
        pa = start(os.path.join(TMP, "A"), PORT_A, "LIVE-A")
        pb = start(os.path.join(TMP, "B"), PORT_B, "LIVE-B")
        procs = [pa, pb]

        for _ in range(40):
            if state(PORT_A) and state(PORT_B):
                break
            time.sleep(0.3)

        found_a = found_b = False
        deadline = time.time() + 25
        while time.time() < deadline:
            peers_a = [p.get("alias") for p in state(PORT_A).get("peers", [])]
            peers_b = [p.get("alias") for p in state(PORT_B).get("peers", [])]
            found_a = "LIVE-B" in peers_a
            found_b = "LIVE-A" in peers_b
            if found_a and found_b:
                break
            time.sleep(1.0)

        peers_a = state(PORT_A).get("peers", [])
        # 后台探测线程会把对方标记为 app（首次约 2 秒）
        kind_ok = False
        kind_deadline = time.time() + 20
        while time.time() < kind_deadline:
            peers_a = state(PORT_A).get("peers", [])
            if any(p.get("kind") == "app" for p in peers_a):
                kind_ok = True
                break
            time.sleep(0.5)
        kinds = [(p.get("alias"), p.get("kind"), p.get("ip"), p.get("port")) for p in peers_a]
        print("  %s  A 自动发现 B（组播 announce + register）" % ("PASS" if found_a else "FAIL"))
        print("  %s  B 自动发现 A（组播 announce + register）" % ("PASS" if found_b else "FAIL"))
        print("  %s  A 侧设备类型被识别为 app" % ("PASS" if kind_ok else "FAIL"))
        print("       A 侧看到：%s" % kinds)

        ok = found_a and found_b and kind_ok
        return 0 if ok else 1
    finally:
        for proc, log_path, handle in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        time.sleep(0.6)
        for proc, log_path, handle in procs:
            try:
                proc.kill()
            except Exception:
                pass
        for proc, log_path, handle in procs:
            try:
                handle.flush()
                handle.close()
            except Exception:
                pass
            try:
                with open(log_path, "rb") as fp:
                    text = fp.read().decode("utf-8", "replace").strip()
                if text:
                    print("----- %s 日志尾部 -----" % os.path.basename(log_path))
                    print(text[-800:])
            except Exception:
                pass

if __name__ == "__main__":
    raise SystemExit(main())
