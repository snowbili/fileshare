# -*- coding: utf-8 -*-
"""FileShare 端到端自检（只用标准库）。

同一台机器上起两个实例（各自独立 FILESHARE_HOME），模拟"两台电脑"，
覆盖 M0~M4 的关键路径：安全校验、上传下载、断点续传、中文名、路径穿越、
双机互发现、互推、代理下载、LocalSend 兼容流程。

用法：  python tests/run_checks.py
结果：  stdout + tests/run_checks_result.txt
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
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
TMP = os.path.join(HERE, "tmp")
PY = sys.executable
PORT_A = 18099
PORT_B = 18100
HOME_A = os.path.join(TMP, "A")
HOME_B = os.path.join(TMP, "B")

results = []
lines = []

def log(text: str) -> None:
    print(text)
    lines.append(text)

def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    log("  %s  %-52s %s" % ("PASS" if ok else "FAIL", name, detail))
    return bool(ok)

def section(title: str) -> None:
    log("")
    log("========== %s ==========" % title)

def request(url: str, method: str = "GET", data=None, headers=None, timeout: float = 30.0):
    """返回 (status, headers_dict, body_bytes)；HTTP 错误也正常返回。"""
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:
            pass
        return exc.code, dict(exc.headers or {}), body
    except Exception as exc:                      # 连接层错误
        return 0, {}, str(exc).encode("utf-8")

def raw_http(port: int, text: str) -> str:
    """用裸 socket 发原始请求，便于伪造 Host 头等用例。"""
    payload = text.encode("utf-8")
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(payload)
        chunks = []
        while True:
            try:
                block = sock.recv(65536)
            except socket.timeout:
                break
            if not block:
                break
            chunks.append(block)
    data = b"".join(chunks)
    return data.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")

def start_instance(home: str, port: int, alias: str) -> subprocess.Popen:
    """启动一个实例。

    注意：日志必须重定向到**文件**而不是 subprocess.PIPE。
    Windows 的匿名管道默认只有 4KB 缓冲，服务端请求日志写满后会把处理线程阻塞住，
    表现为"跑到某个点之后所有请求都超时"。
    """
    env = dict(os.environ)
    env["FILESHARE_HOME"] = home
    env["PYTHONIOENCODING"] = "utf-8"
    os.makedirs(home, exist_ok=True)
    log_path = os.path.join(TMP, "%s.log" % alias)
    handle = open(log_path, "wb")
    proc = subprocess.Popen(
        [PY, os.path.join(APP, "server.py"), "--port", str(port), "--alias", alias,
         "--no-discovery", "--no-window"],
        env=env, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
    proc._log_path = log_path        # noqa: SLF001  便于收尾时读取
    proc._log_handle = handle        # noqa: SLF001
    return proc

def wait_ready(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _, _ = request("http://127.0.0.1:%d/api/state" % port, timeout=2)
        if status == 200:
            return True
        time.sleep(0.3)
    return False

def read_token(home: str) -> str:
    with open(os.path.join(home, "data", "token.txt"), "r", encoding="utf-8") as fp:
        return fp.read().strip()

def wait_for(predicate, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False

def api(port: int, path: str, method: str = "GET", data=None, headers=None, token: str = "",
        timeout: float = 30.0):
    extra = dict(headers or {})
    if token:
        extra["X-FS-Token"] = token
    return request("http://127.0.0.1:%d%s" % (port, path), method=method, data=data,
                   headers=extra, timeout=timeout)

def jbody(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")

def jload(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8", "replace")) or {}
    except ValueError:
        return {}

def local_origin(port: int) -> str:
    """构造一个"本机来源地址"，用于测试 Origin 白名单。

    动态推导而不是硬编码作者机器的 IP：这个用例的本意是"任意一个指向本机的来源地址"。
    """
    candidates = []
    try:
        host = socket.gethostbyname(socket.gethostname())
        if host and not host.startswith("127."):
            candidates.append(host)
    except OSError:
        pass
    # 用 UDP connect 取默认出口地址（不会真的发包）
    for probe in ("8.8.8.8", "1.1.1.1"):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.2)
            sock.connect((probe, 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                candidates.append(ip)
        except OSError:
            pass
        finally:
            if sock is not None:
                sock.close()
    candidates.append("127.0.0.1")
    return "http://%s:%d" % (candidates[0], port)

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# --------------------------------------------------------------------------- #
def port_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1.0)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()

def main() -> int:
    for port in (PORT_A, PORT_B):
        if not port_free(port):
            log("端口 %d 已被占用：可能有上一轮自检实例没退出，请先结束它再运行。" % port)
            return 2
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    log("FileShare 自检开始：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    procs = []
    try:
        proc_a = start_instance(HOME_A, PORT_A, "PC-A")
        proc_b = start_instance(HOME_B, PORT_B, "PC-B")
        procs = [proc_a, proc_b]
        if not (wait_ready(PORT_A) and wait_ready(PORT_B)):
            log("启动失败：实例未就绪")
            for proc in procs:
                try:
                    proc._log_handle.flush()          # noqa: SLF001
                    with open(proc._log_path, "rb") as fp:   # noqa: SLF001
                        log(fp.read().decode("utf-8", "replace"))
                except Exception:
                    pass
            return 1

        token_a = read_token(HOME_A)
        token_b = read_token(HOME_B)
        share_a = os.path.join(HOME_A, "shared")
        share_b = os.path.join(HOME_B, "shared")
        inbox_a = os.path.join(HOME_A, "inbox")
        inbox_b = os.path.join(HOME_B, "inbox")
        for path in (share_a, share_b, inbox_a, inbox_b):
            os.makedirs(path, exist_ok=True)
        log("实例已就绪：A=127.0.0.1:%d   B=127.0.0.1:%d" % (PORT_A, PORT_B))

        # ---------------------------------------------------------- M0
        section("M0 服务骨架与安全校验")
        status, _, body = api(PORT_A, "/api/state")
        info_a = jload(body)
        check("GET /api/state 返回本机信息",
              status == 200 and info_a.get("app") == "fileshare",
              "alias=%s port=%s" % (info_a.get("alias"), info_a.get("port")))
        check("状态含共享/接收/下载目录",
              bool(info_a.get("share_dir") and info_a.get("inbox_dir") and info_a.get("download_dir")))

        head = raw_http(PORT_A, "GET /api/state HTTP/1.1\r\nHost: evil.example.com\r\n"
                                "Connection: close\r\n\r\n")
        check("Host 伪造（DNS rebinding）被拒 403", " 403" in head.split("\r\n")[0],
              head.split("\r\n")[0])

        status, _, _ = api(PORT_A, "/api/fs/mkdir?root=share&path=noToken", method="POST", data=b"{}")
        check("写操作缺 token 被拒 403", status == 403, "status=%s" % status)

        status, _, _ = api(PORT_A, "/api/fs/mkdir?root=share&path=badOrigin", method="POST",
                           data=b"{}", headers={"Origin": "http://evil.example.com"}, token=token_a)
        check("写操作错误 Origin 被拒 403", status == 403, "status=%s" % status)

        status, _, _ = api(PORT_A, "/api/fs/mkdir?root=share&path=crossSite", method="POST",
                           data=b"{}", headers={"Origin": local_origin(PORT_A)},
                           token=token_a)
        check("合法 Origin + token 通过", status == 200, "status=%s" % status)
        shutil.rmtree(os.path.join(share_a, "crossSite"), ignore_errors=True)

        status, _, _ = api(PORT_A, "/api/fs/mkdir?root=share&path=peerDir", method="POST",
                           data=b"{}", headers={"X-FS-Peer": "deadbeefdeadbeef"})
        check("X-FS-Peer 可建目录（服务端互推用）", status == 200, "status=%s" % status)

        status, _, _ = api(PORT_A, "/api/fs/delete?root=share&path=peerDir", method="POST",
                           data=b"{}", headers={"X-FS-Peer": "deadbeefdeadbeef"})
        check("X-FS-Peer 不能删除文件（最小权限）", status == 403, "status=%s" % status)

        status, _, body = api(PORT_A, "/api/localsend/v2/info")
        check("GET /api/localsend/v2/info 可用",
              status == 200 and jload(body).get("port") == PORT_A)

        options_head = raw_http(PORT_A, "OPTIONS /api/fs/upload HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                       "Origin: http://127.0.0.1\r\nConnection: close\r\n\r\n")
        check("CORS 允许头里没有 X-FS-Peer", "X-FS-Peer" not in options_head)

        # ---------------------------------------------- CORS 收紧（防跨域读取共享目录）
        same_origin = local_origin(PORT_A)
        status, headers, _ = api(PORT_A, "/api/fs/list?root=share&path=",
                                 headers={"Origin": same_origin})
        check("同源请求回显自己的 Origin（不再用通配 *）",
              status == 200 and headers.get("Access-Control-Allow-Origin") == same_origin
              and headers.get("Access-Control-Allow-Origin") != "*",
              "ACAO=%s" % headers.get("Access-Control-Allow-Origin"))

        status, headers, _ = api(PORT_A, "/api/fs/list?root=share&path=",
                                 headers={"Origin": "http://evil.example.com"})
        check("陌生 Origin 拿不到 Access-Control-Allow-Origin（防火网页偷读）",
              status == 200 and "Access-Control-Allow-Origin" not in headers,
              "ACAO=%s" % headers.get("Access-Control-Allow-Origin", "(无)"))

        status, headers, _ = api(PORT_A, "/api/fs/list?root=share&path=")
        check("非浏览器请求（无 Origin）不带 CORS 头",
              status == 200 and "Access-Control-Allow-Origin" not in headers,
              "ACAO=%s" % headers.get("Access-Control-Allow-Origin", "(无)"))

        evil_head = raw_http(PORT_A, "OPTIONS /api/fs/upload HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                     "Origin: http://evil.example.com\r\nConnection: close\r\n\r\n")
        check("陌生 Origin 的预检也不放行（无 ACAO；写操作仍会被 403）",
              "Access-Control-Allow-Origin" not in evil_head)

        # ---------------------------------------------- M0b 界面与 HTTP/1.1 帧
        section("M0b 界面资源与 HTTP/1.1 帧完整性")
        status, headers, body = api(PORT_A, "/")
        html = body.decode("utf-8", "replace")
        token_ok = False
        marker = 'data-token="'
        if marker in html:
            value = html.split(marker, 1)[1].split('"', 1)[0]
            token_ok = len(value) == 32 and value == token_a
        check("首页返回且注入了 token", status == 200 and token_ok,
              "status=%s bytes=%d" % (status, len(body)))
        check("首页引用前端三件套",
              "/app.js" in html and "/style.css" in html and "/qr.js" in html)
        check("首页没有残留占位符", "__FS_TOKEN__" not in html and "__FS_VERSION__" not in html)

        for asset, expect in (("/app.js", "javascript"), ("/style.css", "text/css"), ("/qr.js", "javascript")):
            status, headers, body = api(PORT_A, asset)
            check("静态资源 %s 正常" % asset,
                  status == 200 and expect in (headers.get("Content-Type") or "") and len(body) > 100,
                  "status=%s type=%s bytes=%d" % (status, headers.get("Content-Type"), len(body)))

        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", PORT_A, timeout=10)
        codes = []
        try:
            for _ in range(3):
                conn.request("GET", "/api/state")
                resp = conn.getresponse()
                resp.read()
                codes.append(resp.status)
            conn.request("GET", "/api/fs/list?root=share&path=")
            resp = conn.getresponse()
            resp.read()
            codes.append(resp.status)
            # 错误响应之后同一条连接仍能正常收发（keep-alive 未被破坏）
            conn.request("GET", "/api/fs/list?root=share&path=" + urllib.parse.quote("../../"))
            err = conn.getresponse()
            err.read()
            conn.request("GET", "/api/state")
            after = conn.getresponse()
            after.read()
            codes.append(err.status)
            codes.append(after.status)
        finally:
            conn.close()
        check("同一连接多次请求（keep-alive）帧正确",
              codes == [200, 200, 200, 200, 403, 200], "codes=%s" % codes)

        # ---------------------------------------------------------- M1
        section("M1 共享目录 API（硬坑 #2/#4/#6/#7/#8/#9/#12）")
        payload = "hello fileshare 你好".encode("utf-8")
        status, _, body = api(PORT_A, "/api/fs/upload?root=share&path=&name=hello.txt",
                              method="POST", data=payload, token=token_a)
        check("裸 body 上传成功", status == 200 and jload(body).get("name") == "hello.txt",
              "status=%s body=%s" % (status, body[:160].decode("utf-8", "replace")))
        stored = os.path.join(share_a, "hello.txt")
        check("文件落盘且内容一致",
              os.path.isfile(stored) and open(stored, "rb").read() == payload)

        mtime = 1600000000
        api(PORT_A, "/api/fs/upload?root=share&name=withtime.bin", method="POST",
            data=b"mtime-test", token=token_a, headers={"X-File-Mtime": str(mtime)})
        saved = os.path.join(share_a, "withtime.bin")
        check("保留原始 mtime（硬坑 #8）",
              os.path.isfile(saved) and abs(os.path.getmtime(saved) - mtime) < 2,
              "mtime=%s" % (int(os.path.getmtime(saved)) if os.path.exists(saved) else "无"))

        status, _, _ = api(PORT_A, "/api/fs/upload?root=share&name=" +
                           urllib.parse.quote("你好 世界.txt"), method="POST",
                           data=b"chinese", token=token_a)
        check("中文文件名上传正常",
              status == 200 and os.path.isfile(os.path.join(share_a, "你好 世界.txt")),
              "status=%s" % status)

        status, headers, _ = api(PORT_A, "/api/fs/raw?root=share&path=" +
                                 urllib.parse.quote("你好 世界.txt"))
        check("中文名下载用 RFC 5987 编码（硬坑 #2）",
              status == 200 and "filename*=UTF-8''" in headers.get("Content-Disposition", ""),
              headers.get("Content-Disposition", ""))

        api(PORT_A, "/api/fs/upload?root=share&name=CON.txt", method="POST",
            data=b"reserved", token=token_a)
        check("Windows 保留名 CON.txt 被改名（硬坑 #4）",
              os.path.isfile(os.path.join(share_a, "_CON.txt")))

        api(PORT_A, "/api/fs/upload?root=share&name=" + urllib.parse.quote("bad:name*?.txt"),
            method="POST", data=b"sanitize", token=token_a)
        check("非法字符文件名被清洗（硬坑 #4）",
              os.path.isfile(os.path.join(share_a, "bad_name__.txt")))

        status, _, body = api(PORT_A, "/api/fs/upload?root=share&name=hello.txt", method="POST",
                              data=b"dup", token=token_a)
        check("同名文件自动改名 hello (1).txt",
              os.path.isfile(os.path.join(share_a, "hello (1).txt")),
              "status=%s name=%s" % (status, jload(body).get("name")))

        status, _, _ = api(PORT_A, "/api/fs/upload?root=share&name=sha.txt", method="POST",
                           data=b"sha-payload", token=token_a,
                           headers={"X-File-Sha256": sha256_bytes(b"wrong")})
        check("sha256 不符返回 400 且不留残文件（硬坑 #12）",
              status == 400 and not os.path.exists(os.path.join(share_a, "sha.txt")),
              "status=%s" % status)

        status, _, _ = api(PORT_A, "/api/fs/upload?root=share&name=sha.txt", method="POST",
                           data=b"sha-payload", token=token_a,
                           headers={"X-File-Sha256": sha256_bytes(b"sha-payload")})
        check("sha256 相符时上传成功",
              status == 200 and os.path.isfile(os.path.join(share_a, "sha.txt")), "status=%s" % status)

        leftovers = [n for n in os.listdir(share_a) if n.endswith(".part") or n.endswith(".part.json")]
        check("上传中间态文件已清理（硬坑 #9）", not leftovers, "残留=%s" % leftovers)

        status, headers, body = api(PORT_A, "/api/fs/raw?root=share&path=withtime.bin",
                                    headers={"Range": "bytes=0-3"})
        check("Range 分段下载 206 且字节正确（硬坑 #1）",
              status == 206 and body == b"mtim" and headers.get("Content-Range") == "bytes 0-3/10",
              "status=%s body=%r range=%s" % (status, body, headers.get("Content-Range")))

        status, headers, body = api(PORT_A, "/api/fs/raw?root=share&path=withtime.bin")
        check("整文件下载内容一致且带 ETag/X-File-Mtime",
              status == 200 and body == b"mtime-test" and headers.get("ETag")
              and headers.get("X-File-Mtime"))

        status, _, _ = api(PORT_A, "/api/fs/upload?root=share&name=chunked.bin", method="POST",
                           data=b"", token=token_a, headers={"Transfer-Encoding": "chunked"})
        check("chunked 上传被拒 411（硬坑 #7）", status == 411, "status=%s" % status)

        status, _, _ = api(PORT_A, "/api/fs/list?root=share&path=" + urllib.parse.quote("../../"))
        check("路径穿越 ../../ 被拒 403（硬坑 #6）", status == 403, "status=%s" % status)

        status, _, _ = api(PORT_A, "/api/fs/raw?root=share&path=" +
                           urllib.parse.quote("C:/Windows/win.ini"))
        check("盘符绝对路径被拒", status in (400, 403), "status=%s" % status)

        status, headers, body = api(PORT_A, "/api/fs/upload?root=share&name=resume.bin", method="HEAD")
        check("续传探测 HEAD /api/fs/upload 返回 X-Received 头",
              status == 200 and headers.get("X-Received") == "0",
              "status=%s X-Received=%s" % (status, headers.get("X-Received")))

        status, _, body = api(PORT_A, "/api/fs/list?root=share&path=")
        names = [entry["name"] for entry in jload(body).get("entries", [])]
        check("目录列表接口返回条目", status == 200 and "hello.txt" in names,
              "%d 项" % len(names))

        # ---------------------------------------------------------- M2
        section("M2 双向发现与对方目录浏览")
        a_info = jload(api(PORT_A, "/api/localsend/v2/info")[2])
        b_info = jload(api(PORT_B, "/api/localsend/v2/info")[2])
        with open(os.path.join(share_b, "from_b.txt"), "w", encoding="utf-8") as fp:
            fp.write("B 的共享文件")

        status, _, body = api(PORT_B, "/api/localsend/v2/register", method="POST",
                              data=jbody(a_info), headers={"Content-Type": "application/json"})
        check("B 收到 A 的 register 并回礼", status == 200 and jload(body).get("port") == PORT_B,
              "status=%s" % status)
        api(PORT_A, "/api/localsend/v2/register", method="POST", data=jbody(b_info),
            headers={"Content-Type": "application/json"})
        peers_b = jload(api(PORT_B, "/api/state")[2]).get("peers", [])
        check("B 的设备表里出现 A", any(p.get("alias") == "PC-A" for p in peers_b),
              "peers=%s" % [p.get("alias") for p in peers_b])

        ok = wait_for(lambda: any(p.get("kind") == "app"
                                 for p in jload(api(PORT_B, "/api/state")[2]).get("peers", [])),
                      timeout=15)
        check("后台探测把 A 标记为可浏览（kind=app）", ok)

        peers_b = jload(api(PORT_B, "/api/state")[2]).get("peers", [])
        fp_a = peers_b[0].get("fingerprint") if peers_b else ""
        status, _, body = api(PORT_B, "/api/peer/list?device=%s&root=share&path=" % fp_a)
        got = [entry["name"] for entry in jload(body).get("entries", [])]
        check("B 能浏览 A 的共享目录", status == 200 and "hello.txt" in got,
              "status=%s entries=%s" % (status, got[:6]))

        status, _, _ = api(PORT_B, "/api/peer/list?device=nonexistent&root=share&path=")
        check("未知设备返回 404", status == 404, "status=%s" % status)

        # ---------------------------------------------------------- M3
        section("M3 推送 / 拉取 / 代理下载")
        status, _, body = api(PORT_B, "/api/peer/raw?device=%s&root=share&path=hello.txt" % fp_a)
        check("代理下载可用（拖出到资源管理器走这条）",
              status == 200 and body.decode("utf-8", "replace") == "hello fileshare 你好",
              "status=%s" % status)

        status, _, _ = api(PORT_B, "/api/peer/push?device=%s" % fp_a, method="POST",
                           data=jbody({"path": "from_b.txt", "root": "share"}),
                           headers={"Content-Type": "application/json"}, token=token_b)
        check("B 推送本机文件已入队", status == 202, "status=%s" % status)
        ok = wait_for(lambda: os.path.isfile(os.path.join(inbox_a, "from_b.txt")))
        check("推送的文件落到 A 的接收目录",
              ok and open(os.path.join(inbox_a, "from_b.txt"), "rb").read() == "B 的共享文件".encode("utf-8"))

        status, _, _ = api(PORT_B, "/api/peer/pull", method="POST",
                           data=jbody({"device": fp_a, "root": "share", "path": "你好 世界.txt",
                                       "target": "share"}), token=token_b)
        check("页内拖拽拉取已入队", status == 202, "status=%s" % status)
        ok = wait_for(lambda: os.path.isfile(os.path.join(share_b, "你好 世界.txt")))
        check("拉取的文件落到 B 的共享目录",
              ok and open(os.path.join(share_b, "你好 世界.txt"), "rb").read() == b"chinese")

        big = os.urandom(3 * 1024 * 1024 + 12345)
        with open(os.path.join(share_a, "big.bin"), "wb") as fp:
            fp.write(big)
        status, _, body = api(PORT_B, "/api/peer/raw?device=%s&root=share&path=big.bin" % fp_a,
                              timeout=180)
        check("跨实例代理下载 3MB 文件内容一致（1MB 分块循环）",
              status == 200 and sha256_bytes(body) == sha256_bytes(big),
              "%d 字节 status=%s" % (len(body), status))

        copies = os.listdir(os.path.join(HOME_B, "downloads"))
        check("拖出下载在本机 downloads 留了副本",
              any(name.startswith("big") for name in copies), "downloads=%s" % copies[:5])

        # ---------------------------------------------------------- M4
        section("M4 LocalSend 协议兼容")
        api(PORT_A, "/api/settings", method="POST", data=jbody({"auto_accept": True}), token=token_a)
        file_id = "fid-0001"
        prepare = {
            "info": b_info,
            "files": {file_id: {"id": file_id, "fileName": "来自LocalSend.txt", "size": 9,
                                "fileType": "text/plain", "sha256": None, "preview": None}},
        }
        status, _, body = api(PORT_A, "/api/localsend/v2/prepare-upload", method="POST",
                              data=jbody(prepare), headers={"Content-Type": "application/json"})
        result = jload(body)
        check("prepare-upload（自动接受）返回 sessionId 与 token",
              status == 200 and result.get("sessionId") and result.get("files", {}).get(file_id),
              "status=%s" % status)
        session_id = result.get("sessionId", "")
        file_token = (result.get("files") or {}).get(file_id, "")

        status, _, _ = api(PORT_A, "/api/localsend/v2/upload?sessionId=%s&fileId=%s&token=wrong"
                           % (session_id, file_id), method="POST", data=b"123456789")
        check("错误 token 上传被拒 403", status == 403, "status=%s" % status)

        status, _, _ = api(PORT_A, "/api/localsend/v2/upload?sessionId=bad&fileId=%s&token=%s"
                           % (file_id, file_token), method="POST", data=b"123456789")
        check("错误 sessionId 上传被拒 400", status == 400, "status=%s" % status)

        status, _, _ = api(PORT_A, "/api/localsend/v2/upload?sessionId=%s&fileId=%s&token=%s"
                           % (session_id, file_id, file_token), method="POST", data=b"123456789")
        check("正确 token 上传成功（裸 body）", status == 200, "status=%s" % status)
        target = os.path.join(inbox_a, "来自LocalSend.txt")
        ok = wait_for(lambda: os.path.isfile(target), timeout=10)
        check("LocalSend 收到的文件落在接收目录",
              ok and open(target, "rb").read() == b"123456789")

        status, _, _ = api(PORT_A, "/api/localsend/v2/cancel?sessionId=%s" % session_id,
                           method="POST", data=b"")
        check("cancel 会话返回 200", status == 200, "status=%s" % status)

        status, _, body = api(PORT_A, "/api/localsend/v2/prepare-download", method="POST", data=b"")
        files = jload(body).get("files", {})
        check("反向下载 API 暴露共享文件清单", status == 200 and "hello.txt" in files,
              "%d 个文件" % len(files))
        status, _, body = api(PORT_A, "/api/localsend/v2/download?fileId=hello.txt")
        check("反向下载取文件成功（手机浏览器直连下载）",
              status == 200 and body.decode("utf-8", "replace") == "hello fileshare 你好")

        api(PORT_A, "/api/settings", method="POST", data=jbody({"auto_accept": False}), token=token_a)
        try:
            api(PORT_A, "/api/localsend/v2/prepare-upload", method="POST",
                data=jbody(prepare), headers={"Content-Type": "application/json"}, timeout=4)
        except Exception:
            pass
        pending = jload(api(PORT_A, "/api/state")[2]).get("pending", [])
        check("关闭自动接收后进入待确认队列（界面弹窗）", len(pending) >= 1,
              "pending=%d" % len(pending))
        if pending:
            status, _, _ = api(PORT_A, "/api/pending/decide", method="POST",
                               data=jbody({"sessionId": pending[0]["sessionId"], "accept": False}),
                               token=token_a)
            check("待确认请求可被拒绝", status == 200, "status=%s" % status)
        else:
            check("待确认请求可被拒绝", False, "没有 pending")
    finally:
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        time.sleep(0.8)
        for proc in procs:
            try:
                proc.kill()
            except Exception:
                pass
        # 把两个实例的日志收进报告，便于定位服务端异常
        for index, proc in enumerate(procs):
            try:
                proc._log_handle.flush()          # noqa: SLF001
                proc._log_handle.close()          # noqa: SLF001
            except Exception:
                pass
            try:
                with open(proc._log_path, "rb") as fp:   # noqa: SLF001
                    output = fp.read().decode("utf-8", "replace")
            except Exception:
                output = ""
            if output.strip():
                log("")
                log("----- 实例 %s 日志（尾部 2500 字） -----" % ("A" if index == 0 else "B"))
                log(output.strip()[-2500:])

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    failures = [name for name, ok, _ in results if not ok]
    log("")
    log("=" * 58)
    log("自检结果：%d/%d 通过" % (passed, total))
    if failures:
        log("失败项：")
        for name in failures:
            log("  - " + name)
    log("=" * 58)
    with open(os.path.join(HERE, "run_checks_result.txt"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    return 0 if not failures else 1

if __name__ == "__main__":
    raise SystemExit(main())
