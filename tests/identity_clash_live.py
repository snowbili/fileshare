# -*- coding: utf-8 -*-
"""复现并验证"两台 FileShare 互相看不见（指纹撞车）"的修复。

场景：把程序目录整体拷到另一台电脑时，data/fingerprint.txt 也被一起带走，
于是两台设备指纹相同 —— 修复前双方都把对方当成"自己"丢弃，谁也看不到谁。

本测试用回环别名 127.0.0.2 模拟"另一台电脑"（来源 IP 不同、指纹相同），
走的是与真实环境完全相同的代码路径：/api/peer/add -> register -> upsert_peer。

用法： python tests/identity_clash_live.py
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
TMP = os.path.join(HERE, "tmp_clash")
HOME_A = os.path.join(TMP, "A")
HOME_B = os.path.join(TMP, "B")
PORT_A = 18131
PORT_B = 18132
ALIAS_B_HOST = "127.0.0.2"

def get(port, path, host="127.0.0.1", timeout=15):
    try:
        with urllib.request.urlopen("http://%s:%d%s" % (host, port, path), timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        return 0, ("%s: %s" % (type(exc).__name__, exc)).encode("utf-8")

def state(port, host="127.0.0.1"):
    status, body = get(port, "/api/state", host=host, timeout=5)
    if status != 200:
        return {}
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return {}

def post(port, path, payload, token="", timeout=20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-FS-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        return 0, ("%s: %s" % (type(exc).__name__, exc)).encode("utf-8")

def token_of(home):
    with open(os.path.join(home, "data", "token.txt"), encoding="utf-8") as fp:
        return fp.read().strip()

def start(home, port, alias, fresh=True, bind="0.0.0.0"):
    env = dict(os.environ)
    env["FILESHARE_HOME"] = home
    env["PYTHONIOENCODING"] = "utf-8"
    if fresh:
        shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, exist_ok=True)
    log_path = os.path.join(TMP, "%s.log" % alias)
    handle = open(log_path, "wb")
    proc = subprocess.Popen([sys.executable, os.path.join(APP, "server.py"), "--port", str(port),
                             "--bind", bind, "--alias", alias, "--no-window"],
                            env=env, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
    return proc, log_path, handle

def wait_state(port, host="127.0.0.1", timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if state(port, host).get("port") == port:
            return True
        time.sleep(0.3)
    return False

def wait_until(predicate, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False

def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    procs = []
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        print("  %s  %-50s %s" % ("PASS" if ok else "FAIL", name, detail))

    try:
        pa = start(HOME_A, PORT_A, "CLASH-A")
        procs.append(pa)
        if not wait_state(PORT_A):
            print("A 启动失败")
            return 1
        token_a = token_of(HOME_A)
        fp_a = state(PORT_A).get("fingerprint", "")
        print("A：%s 指纹=%s" % ("127.0.0.1:%d" % PORT_A, fp_a))

        # 把 A 的整个 data 目录复制给 B —— 等价于"把程序目录拷到另一台电脑"
        shutil.rmtree(HOME_B, ignore_errors=True)
        os.makedirs(HOME_B, exist_ok=True)
        shutil.copytree(os.path.join(HOME_A, "data"), os.path.join(HOME_B, "data"))
        cfg_path = os.path.join(HOME_B, "data", "config.json")
        with open(cfg_path, encoding="utf-8") as fp:
            cfg = json.load(fp)
        cfg["extra_hosts"] = [ALIAS_B_HOST]      # 仅测试需要：允许用回环别名访问
        cfg["discovery_mode"] = "off"
        # 复制过来的 config.json 里是 A 的绝对路径，这里改回 B 自己的目录
        # （真实环境里两台电脑的路径本来就不一样，这一步只是让测试各用各的目录）
        cfg["share_dir"] = os.path.join(HOME_B, "shared")
        cfg["inbox_dir"] = os.path.join(HOME_B, "inbox")
        cfg["download_dir"] = os.path.join(HOME_B, "downloads")
        with open(cfg_path, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=2)

        pb = start(HOME_B, PORT_B, "CLASH-B", fresh=False, bind=ALIAS_B_HOST)
        procs.append(pb)
        if not wait_state(PORT_B, host=ALIAS_B_HOST):
            print("B 启动失败（本机可能不支持回环别名 %s）" % ALIAS_B_HOST)
            return 1
        fp_b = state(PORT_B, host=ALIAS_B_HOST).get("fingerprint", "")
        print("B：%s 指纹=%s" % ("%s:%d" % (ALIAS_B_HOST, PORT_B), fp_b))
        check("已复现指纹撞车（两台设备指纹相同）", bool(fp_a) and fp_a == fp_b)

        # A 通过"添加设备"认识 B —— 与真实环境收到对方 announce 后的代码路径一致
        status, body = post(PORT_A, "/api/peer/add", {"addr": "%s:%d" % (ALIAS_B_HOST, PORT_B)},
                            token=token_a)
        peers_a = state(PORT_A).get("peers", [])
        check("指纹相同但来源 IP 不同的设备被登记（修复点）",
              any(p.get("alias") == "CLASH-B" for p in peers_a),
              "status=%s peers=%s" % (status, [(p.get("alias"), p.get("ip")) for p in peers_a]))
        clash_peer = None
        for peer in peers_a:
            if peer.get("alias") == "CLASH-B":
                clash_peer = peer
        check("该设备被标记 same_fingerprint（界面显示 ⚠）",
              bool(clash_peer) and clash_peer.get("same_fingerprint") is True)
        check("内部 key 用 IP 区分，不会与其它同指纹设备互相顶掉",
              bool(clash_peer) and clash_peer.get("fingerprint", "").endswith("@" + ALIAS_B_HOST),
              (clash_peer or {}).get("fingerprint", "")[-16:])

        share_b = os.path.join(HOME_B, "shared")
        os.makedirs(share_b, exist_ok=True)
        with open(os.path.join(share_b, "clash.txt"), "w", encoding="utf-8") as fp:
            fp.write("指纹撞车也能传")

        kind_ok = wait_until(lambda: any(p.get("alias") == "CLASH-B" and p.get("kind") == "app"
                                        for p in state(PORT_A).get("peers", [])), timeout=20)
        check("A 侧把 B 识别为可浏览（HTTP 互通正常）", kind_ok)

        key = (clash_peer or {}).get("fingerprint", "")
        status, body = get(PORT_A, "/api/peer/list?device=%s&root=share&path=" % key)
        names = []
        if status == 200:
            names = [e.get("name") for e in json.loads(body.decode("utf-8", "replace")).get("entries", [])]
        check("A 能浏览 B 的共享目录", status == 200 and "clash.txt" in names,
              "status=%s entries=%s" % (status, names))

        status, body = get(PORT_A, "/api/peer/raw?device=%s&root=share&path=clash.txt" % key)
        check("A 能代理下载 B 的文件（拖出到资源管理器走这条）",
              status == 200 and body.decode("utf-8", "replace") == "指纹撞车也能传",
              "status=%s" % status)

        os.makedirs(os.path.join(HOME_A, "shared"), exist_ok=True)
        with open(os.path.join(HOME_A, "shared", "from_a.txt"), "w", encoding="utf-8") as fp:
            fp.write("A 推给 B")
        status, _ = post(PORT_A, "/api/peer/push?device=%s" % key,
                         {"path": "from_a.txt", "root": "share"}, token=token_a)
        check("A 能把文件推给指纹相同的 B", status == 202, "status=%s" % status)
        ok = wait_until(lambda: os.path.isfile(os.path.join(HOME_B, "inbox", "from_a.txt")), timeout=20)
        check("文件确实落到 B 的接收目录", ok)

        status, _ = post(PORT_A, "/api/settings", {"alias": "CLASH-A"}, token=token_a)
        check("A 全程无异常（写操作可用）", status == 200, "status=%s" % status)
    finally:
        for proc, log_path, handle in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        time.sleep(0.8)
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

    failed = [name for name, ok in results if not ok]
    print("")
    print("指纹撞车场景校验：%d/%d 通过" % (len(results) - len(failed), len(results)))
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if not failed else 1

if __name__ == "__main__":
    raise SystemExit(main())
