# -*- coding: utf-8 -*-
"""FileShare 服务入口：路由、静态托管、安全校验、单实例。

安全模型（硬坑 #10）：
  1) Host 白名单     —— 只接受 127.0.0.1 / localhost / 本机局域网 IP / 本机名，挡 DNS rebinding
  2) Origin 校验     —— 写操作若带 Origin，必须是本机允许的来源，挡 CSRF
  3) 一次性 token    —— 写操作需要 X-FS-Token（由 index.html 注入页面，跨站脚本拿不到）
  4) X-FS-Peer       —— 仅 /api/fs/upload、/api/fs/mkdir 接受（服务端互推用），
                        且不放进 CORS 白名单，跨站脚本无法伪造
HTTP 纪律（硬坑 #1）：HTTP/1.1 + 每个响应都有精确 Content-Length，
                       且响应前必须读完请求体（否则 keep-alive 串包）。
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config          # noqa: E402
import fsapi           # noqa: E402
import store           # noqa: E402
import util            # noqa: E402

VERSION = "0.1"
CHUNK = 1024 * 1024
DRAIN_LIMIT = 1024 * 1024
JSON_LIMIT = 4 * 1024 * 1024
CORSMETHODS = "GET, HEAD, POST, PUT, OPTIONS"
# 注意：X-FS-Peer 刻意不放进 CORS 白名单，否则跨站脚本可通过预检伪造"服务端互推"身份
CORSHEADERS = "Content-Type, X-FS-Token, X-File-Name, X-File-Mtime, X-File-Sha256, X-Offset, Range, X-FS-Alias"
CORS_EXPOSE = "Content-Disposition, Content-Range, Content-Length, X-Received, X-Final-Name, X-File-Mtime, ETag"
TOKEN_EXEMPT = (
    "/api/localsend/",          # 官方 LocalSend 客户端没有 token，改由协议自身校验 token/PIN
)
PEER_HEADER_PATHS = ("/api/fs/upload", "/api/fs/mkdir")

TOKEN = ""
STATIC_EXT = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


def load_token() -> str:
    """读取/生成一次性 token（写操作鉴权用）。"""
    global TOKEN
    path = config.TOKEN_PATH
    try:
        with open(path, "r", encoding="utf-8") as fp:
            value = fp.read().strip()
        if value:
            TOKEN = value
            return TOKEN
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    TOKEN = secrets.token_hex(16)
    try:
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(TOKEN)
    except OSError:
        pass
    return TOKEN


def _host_names() -> set:
    names = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
    try:
        names.add(socket.gethostname().strip().lower())
        names.add((socket.gethostname() + ".local").strip().lower())
    except OSError:
        pass
    for ip in util.local_ips():
        names.add(ip.lower())
    for extra in config.load().get("extra_hosts") or []:
        names.add(str(extra).strip().lower())
    return {name for name in names if name}


def _is_peer_request(handler) -> bool:
    value = (handler.headers.get("X-FS-Peer") or "").strip()
    if not value:
        return False
    return 8 <= len(value) <= 128 and all(ch in "0123456789abcdefABCDEF-_" for ch in value)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # 硬坑 #1：必须配合精确 Content-Length
    server_version = "FileShare/" + VERSION
    sys_version = ""
    timeout = 600

    # ------------------------------------------------------------ 基础处理
    def log_message(self, fmt: str, *args) -> None:
        """写一行请求日志。

        注意：stderr 可能为 None（pythonw），编码也可能不是 UTF-8
        （终端/重定向到文件时按系统区域设置，中文 Windows 是 GBK、英文是 cp1252）。
        任何一种编不出来都不能让请求失败，所以这里把写日志整个包在 try 里。
        """
        stream = sys.stderr
        if stream is None:
            return
        try:
            line = "[%s] %s - %s" % (time.strftime("%H:%M:%S"),
                                     self.client_address[0], fmt % args)
        except Exception:
            line = "[log] <格式化失败>"
        try:
            stream.write(line + "\n")
        except UnicodeEncodeError:
            try:
                stream.write(line.encode("ascii", "replace").decode("ascii") + "\n")
            except Exception:
                pass
        except Exception:
            pass

    def log_error(self, fmt: str, *args) -> None:
        self.log_message(fmt, *args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:
        self._body_consumed = False
        if not self._host_allowed():
            self.send_json(403, {"error": "Host 不被允许"})
            return
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", CORSMETHODS)
        self.send_header("Access-Control-Allow-Headers", CORSHEADERS)
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method: str) -> None:
        self._body_consumed = False
        self._body_read = 0
        try:
            parts = urlsplit(self.path)
            self.path_only = unquote(parts.path)
            self.params = {key: values[0] for key, values in
                           parse_qs(parts.query, keep_blank_values=True).items()}
        except Exception:
            self.send_json(400, {"error": "无法解析请求"})
            return

        if not self._host_allowed():                      # 硬坑 #10：DNS rebinding
            self.send_json(403, {"error": "Host 不被允许"})
            self.close_connection = True
            return

        try:
            if self.path_only.startswith("/api/"):
                self._handle_api(method)
            elif method in ("GET", "HEAD"):
                self._handle_static(method)
            else:
                self.send_json(404, {"error": "未知路径"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:                          # 兜底：不让线程崩掉
            self.log_message("处理 %s %s 出错: %r", method, self.path_only, exc)
            try:
                self.send_json(500, {"error": "服务端异常: %s" % exc})
            except Exception:
                self.close_connection = True
        finally:
            self._drain_rest()

    # ------------------------------------------------------------ 安全检查
    def _host_allowed(self) -> bool:
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return True                                   # HTTP/1.0 无 Host
        if raw.startswith("["):
            name = raw[1:raw.find("]")] if "]" in raw else raw[1:]
        elif ":" in raw:
            name = raw.rsplit(":", 1)[0]
        else:
            name = raw
        name = name.strip().lower().rstrip(".")
        return name in _host_names()

    def _origin_allowed(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin or origin.lower() == "null":
            return True                                   # 非浏览器请求
        try:
            parts = urlsplit(origin)
        except Exception:
            return False
        if parts.scheme not in ("http", "https"):
            return False
        return (parts.hostname or "").lower().rstrip(".") in _host_names()

    def _token_ok(self) -> bool:
        supplied = (self.headers.get("X-FS-Token") or self.params.get("token") or "").strip()
        if not supplied or not TOKEN:
            return False
        return secrets.compare_digest(supplied, TOKEN)

    def _peer_header_ok(self) -> bool:
        for prefix in PEER_HEADER_PATHS:
            if self.path_only == prefix or self.path_only.startswith(prefix + "/"):
                return _is_peer_request(self)
        return False

    def _write_allowed(self) -> bool:
        if not self._origin_allowed():
            return False
        if self._peer_header_ok():
            return True
        if any(self.path_only.startswith(prefix) for prefix in TOKEN_EXEMPT):
            return True                                   # LocalSend 端点自行校验 token/pin
        return self._token_ok()

    # ------------------------------------------------------------ 响应封装
    def _cors_headers(self) -> None:
        """只在请求确实来自白名单来源时回显 CORS 头。

        之前这里是 `Access-Control-Allow-Origin: *`，等于允许"你浏览器里打开的任意网页"
        跨域读取共享目录（Host 白名单挡不住这种攻击，因为浏览器本来就把 127.0.0.1 当目标）。
        本工具的界面始终是同源访问（相对路径或 location.origin），同源请求不做 CORS 检查，
        跨机传输也早已改成后端对后端，所以没必要给出通配的来源许可。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if origin and origin.lower() != "null" and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Expose-Headers", CORS_EXPOSE)
            self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")

    def send_json(self, code: int, obj: dict, headers: "dict | None" = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if self.command == "HEAD":
            body = b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if self.path_only.startswith("/api/"):
            self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_empty(self, code: int, headers: "dict | None" = None) -> None:
        self.send_response(code)
        if self.path_only.startswith("/api/"):
            self._cors_headers()
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    # ------------------------------------------------------------ 请求体
    def body_length(self):
        """返回请求体长度；None 表示无法确定（chunked / 缺失 / 非法）。"""
        encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if "chunked" in encoding:                          # 硬坑 #7：不实现分块解码，明确拒绝
            return None
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value >= 0 else None

    def mark_body_read(self, count: int) -> None:
        """告诉框架"请求体已从 rfile 读取了 count 字节"。

        否则 _drain_rest() 会在同一条连接上重复读取已经不存在的字节而阻塞（硬坑 #1）。
        """
        self._body_read = max(getattr(self, "_body_read", 0), int(count))
        length = self.body_length()
        if length is None or self._body_read >= length:
            self._body_consumed = True

    def drain_body(self, limit: int = DRAIN_LIMIT) -> None:
        """丢弃剩余请求体，保持 keep-alive 同步；超过上限或无法确定长度时才关连接。"""
        if self._body_consumed:
            return
        encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if self.headers.get("Content-Length") is None and "chunked" not in encoding:
            # GET/HEAD 等本来就没有请求体：什么都不用读，更不能把连接关掉（硬坑 #1）
            self._body_consumed = True
            return
        length = self.body_length()
        if length is None:                      # chunked 或不合法长度：无法安全对齐，关连接
            self.close_connection = True
            return
        remaining = length - self._body_read
        if remaining <= 0:
            self._body_consumed = True
            return
        if remaining > limit:
            self.close_connection = True
            return
        try:
            while remaining > 0:
                block = self.rfile.read(min(65536, remaining))
                if not block:
                    break
                remaining -= len(block)
            self._body_read = length
            self._body_consumed = True
        except OSError:
            self.close_connection = True

    def _drain_rest(self) -> None:
        try:
            self.drain_body()
        except Exception:
            self.close_connection = True

    def read_json_body(self, limit: int = JSON_LIMIT) -> dict:
        length = self.body_length()
        if length is None:
            self._body_consumed = True
            raise ValueError("请求体必须带 Content-Length（不接受 chunked）")
        if length == 0:
            self._body_consumed = True
            return {}
        if length > limit:
            self._body_consumed = True
            raise ValueError("请求体过大")
        data = self.rfile.read(length)
        self._body_read = len(data)
        self._body_consumed = True
        if not data:
            return {}
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("JSON 解析失败")
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return parsed

    # ------------------------------------------------------------ 文件流
    def stream_file(self, abs_path: str, name: str, inline: bool = False,
                    range_value: "str | None" = None) -> None:
        """流式发送文件，支持 Range 断点续传；文件名用 RFC 5987 编码（硬坑 #2）。"""
        try:
            size = int(os.path.getsize(util.long_path(abs_path)))
            mtime = float(os.path.getmtime(util.long_path(abs_path)))
        except OSError as exc:
            self.send_json(404, {"error": "读取文件失败: %s" % exc})
            return
        etag = '"%x-%x"' % (int(mtime), size)
        if (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_empty(304, {"ETag": etag})
            return
        span = util.parse_range(range_value or "", size) if range_value else None
        start, end = span if span else (0, max(0, size - 1))
        partial = span is not None
        length = (end - start + 1) if size else 0

        self.send_response(206 if partial else 200)
        if self.path_only.startswith("/api/"):
            self._cors_headers()
        self.send_header("Content-Type", util.guess_mime(name))
        self.send_header("Content-Disposition", util.content_disposition(name, inline=inline))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", util.http_date(mtime))
        self.send_header("X-File-Mtime", str(int(mtime)))    # 硬坑 #8
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if self.command == "HEAD" or length == 0:
            return

        sent = 0
        try:
            with open(util.long_path(abs_path), "rb") as fp:
                if start:
                    fp.seek(start)
                while sent < length:
                    block = fp.read(min(CHUNK, length - sent))
                    if not block:
                        break
                    self.wfile.write(block)
                    sent += len(block)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            self.log_message("下载中断 %s (%d/%d 字节)", name, sent, length)
        except OSError as exc:
            self.log_message("读取文件出错 %s: %r", name, exc)
            self.close_connection = True

    # ------------------------------------------------------------ 静态资源
    def _handle_static(self, method: str) -> None:
        rel = self.path_only.lstrip("/") or "index.html"
        if rel == "favicon.ico":
            self.send_empty(204)
            return
        if rel == "index.html":
            self._send_index()
            return
        if rel.startswith("web/"):
            rel = rel[4:]
        root = os.path.realpath(config.WEB_DIR)
        target = os.path.realpath(os.path.join(root, rel))
        if not target.startswith(root + os.sep) or not os.path.isfile(target):
            self.send_json(404, {"error": "资源不存在"})
            return
        ext = os.path.splitext(target)[1].lower()
        try:
            with open(target, "rb") as fp:
                body = fp.read()
        except OSError:
            self.send_json(500, {"error": "读取资源失败"})
            return
        self.send_response(200)
        self.send_header("Content-Type", STATIC_EXT.get(ext, "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if method == "GET":
            self.wfile.write(body)

    def _send_index(self) -> None:
        """注入 token 与版本号（token 只出现在页面里，跨站脚本取不到）。"""
        path = os.path.join(config.WEB_DIR, "index.html")
        try:
            with open(path, "r", encoding="utf-8") as fp:
                html = fp.read()
        except OSError:
            self.send_json(500, {"error": "缺少 web/index.html"})
            return
        html = html.replace("__FS_TOKEN__", TOKEN).replace("__FS_VERSION__", VERSION)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command == "GET":
            self.wfile.write(body)

    # ------------------------------------------------------------ API 路由
    def _handle_api(self, method: str) -> None:
        import localsend
        import peer

        path = self.path_only
        if method in ("POST", "PUT", "DELETE") and not self._write_allowed():
            self.send_json(403, {"error": "写操作被拒绝（Origin / token 校验未通过）"})
            return

        table_get = {
            "/api/state": lambda: self.send_json(200, store.store.snapshot()),
            "/api/fs/list": lambda: fsapi.api_list(self, self.params),
            "/api/fs/stat": lambda: fsapi.api_stat(self, self.params),
            "/api/fs/raw": lambda: fsapi.api_raw(self, self.params),
            "/api/peer/list": lambda: peer.api_list(self, self.params),
            "/api/peer/raw": lambda: peer.api_raw(self, self.params),
            "/api/localsend/v2/info": lambda: localsend.api_info(self),
            "/api/localsend/v2/download": lambda: localsend.api_download(self, self.params),
        }
        table_head = {
            "/api/fs/raw": lambda: fsapi.api_raw(self, self.params),
            "/api/fs/upload": lambda: fsapi.api_upload_probe(self, self.params),
            "/api/peer/raw": lambda: peer.api_raw(self, self.params),
        }
        table_post = {
            "/api/fs/upload": lambda: fsapi.api_upload(self, self.params),
            "/api/fs/mkdir": lambda: fsapi.api_mkdir(self, self.params),
            "/api/fs/rename": lambda: fsapi.api_rename(self, self.params),
            "/api/fs/delete": lambda: fsapi.api_delete(self, self.params),
            "/api/peer/add": lambda: peer.api_add(self, self.params),
            "/api/peer/scan": lambda: peer.api_scan(self, self.params),
            "/api/peer/push": lambda: peer.api_push(self, self.params),
            "/api/peer/pull": lambda: peer.api_pull(self, self.params),
            "/api/peer/forget": lambda: peer.api_forget(self, self.params),
            "/api/transfer/cancel": lambda: self.api_cancel_transfer(),
            "/api/pending/decide": lambda: self.api_decide_pending(),
            "/api/settings": lambda: self.api_settings(),
            "/api/localsend/v2/register": lambda: localsend.api_register(self),
            "/api/localsend/v2/prepare-upload": lambda: localsend.api_prepare_upload(self, self.params),
            "/api/localsend/v2/upload": lambda: localsend.api_upload(self, self.params),
            "/api/localsend/v2/cancel": lambda: localsend.api_cancel(self, self.params),
            "/api/localsend/v2/prepare-download": lambda: localsend.api_prepare_download(self, self.params),
        }
        if method == "GET":
            route = table_get.get(path)
        elif method == "HEAD":
            route = table_head.get(path)
        else:
            route = table_post.get(path)
        if route is None:
            self.send_json(404, {"error": "未知接口: %s" % path})
            return
        route()

    # -------------------------------------------------------- 本机小接口
    def api_cancel_transfer(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        tid = str(body.get("id") or "")
        if tid == "*":
            count = store.store.cancel_all_active()
            self.send_json(200, {"ok": True, "cancelled": count})
            return
        item = store.store.get_transfer(tid)
        if not item:
            self.send_json(404, {"error": "传输不存在"})
            return
        store.store.update_transfer(tid, status="cancelled")
        self.send_json(200, {"ok": True})

    def api_decide_pending(self) -> None:
        """本机用户对"别人要发文件给我"的弹窗做出接受/拒绝。"""
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        session_id = str(body.get("sessionId") or "")
        decision = "accept" if body.get("accept") else "reject"
        if not store.store.decide_pending(session_id, decision):
            self.send_json(404, {"error": "请求不存在或已处理"})
            return
        self.send_json(200, {"ok": True, "decision": decision})

    def api_settings(self) -> None:
        """修改本机设置（别名、自动接收、保留副本、删除开关等）。"""
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        allowed = {"alias", "auto_accept", "pin", "keep_copy_on_dragout",
                   "allow_delete", "device_type", "share_dir", "inbox_dir",
                   "download_dir", "discovery_mode", "http_port", "max_parallel"}
        values = {key: body[key] for key in body if key in allowed}
        if "alias" in values:
            values["alias"] = str(values["alias"]).strip()[:64] or config.default_alias()
        if "pin" in values:
            values["pin"] = str(values["pin"]).strip()[:16]
        if "http_port" in values:
            self.send_json(400, {"error": "端口需重启后生效，请修改 data/config.json"})
            return
        changed_dir = False
        for key in ("share_dir", "inbox_dir", "download_dir"):
            if key in values:
                path = os.path.abspath(str(values[key]))
                try:
                    os.makedirs(path, exist_ok=True)
                except OSError as exc:
                    self.send_json(400, {"error": "目录不可用: %s" % exc})
                    return
                values[key] = path
                changed_dir = True
        config.update(values)
        self.send_json(200, {"ok": True, "state": store.store.snapshot(),
                             "dirs_changed": changed_dir})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 下 SO_REUSEADDR 会让"同端口二次绑定"成功（与 Linux 语义不同），
    # 因此这里关掉端口复用：重复启动时第二个实例会明确失败，由 main() 走"复用已有实例"分支。
    allow_reuse_address = False

    def handle_error(self, request, client_address) -> None:      # noqa: D102
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return
        sys.stderr.write("连接 %s 出错: %r\n" % (client_address, exc))


def probe_running(port: int, host: str = "127.0.0.1", timeout: float = 1.5):
    """探测同一端口上是否已有本工具实例（硬坑 #11 单实例）。"""
    import urllib.error
    import urllib.request

    url = "http://%s:%d/api/state" % (host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read(65536).decode("utf-8", "replace"))
        return (data.get("app") == "fileshare"), data
    except (urllib.error.URLError, OSError, ValueError):
        return False, {}


def open_window(url: str, prefer_browser: bool = False) -> str:
    """用 Edge/Chrome 的 --app 模式打开无地址栏窗口（失败则退回默认浏览器）。"""
    if not prefer_browser and os.name == "nt":
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for exe in candidates:
            if os.path.isfile(exe):
                try:
                    subprocess.Popen([exe, "--app=" + url, "--window-size=1400,900"],
                                     close_fds=True)
                    return "app:" + os.path.basename(exe)
                except OSError:
                    continue
    try:
        webbrowser.open(url)
        return "browser"
    except Exception:
        return "none"


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(prog="fileshare", description="局域网文件互传（LocalSend 协议兼容）")
    parser.add_argument("--port", type=int, help="HTTP 端口，默认 8099")
    parser.add_argument("--bind", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--share", help="共享目录（左面板）")
    parser.add_argument("--inbox", help="接收目录")
    parser.add_argument("--downloads", help="下载目录")
    parser.add_argument("--alias", help="本机显示名")
    parser.add_argument("--no-discovery", action="store_true", help="禁用组播发现")
    parser.add_argument("--scan-now", action="store_true", help="启动后立即扫描网段")
    parser.add_argument("--open", action="store_true", help="启动后用 Edge/Chrome 应用窗口打开界面")
    parser.add_argument("--browser", action="store_true", help="用默认浏览器打开界面")
    parser.add_argument("--no-window", action="store_true", help="只跑服务，不开窗口")
    return parser


def _configure_output() -> None:
    """尽量把标准输出/错误切到 UTF-8。

    中文 Windows 的控制台默认是 GBK，一旦输出里出现 GBK 编不出的字符
    （例如中文提示里的 ⚠），程序就会抛 UnicodeEncodeError。
    这里统一按 UTF-8 + errors="replace" 输出，保证任何区域设置下都不会因为
    "打印一句话" 而崩溃。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None) -> int:
    import discovery
    import peer

    _configure_output()
    args = build_parser().parse_args(argv)
    config.ensure_dirs()
    overrides = {}
    if args.port:
        overrides["http_port"] = int(args.port)
    if args.bind:
        overrides["bind_address"] = args.bind
    if args.share:
        overrides["share_dir"] = os.path.abspath(args.share)
    if args.inbox:
        overrides["inbox_dir"] = os.path.abspath(args.inbox)
    if args.downloads:
        overrides["download_dir"] = os.path.abspath(args.downloads)
    if args.alias:
        overrides["alias"] = args.alias
    if args.no_discovery:
        overrides["discovery_mode"] = "off"
    if overrides:
        # 命令行参数只对本次运行生效，不写进 config.json（避免"临时参数被永久记住"）
        config.apply_runtime(overrides)
    config.ensure_dirs()
    load_token()

    cfg = config.load()
    port = int(cfg["http_port"])
    host = cfg["bind_address"] or "0.0.0.0"
    url_local = "http://127.0.0.1:%d" % port

    running, _ = probe_running(port)
    if running:                                          # 硬坑 #11：复用已运行实例
        print("检测到已有实例在 %s 运行，直接打开窗口。" % url_local)
        if not args.no_window:
            open_window(url_local, prefer_browser=args.browser)
        return 0

    try:
        httpd = Server((host, port), Handler)
    except OSError as exc:
        print("端口 %d 无法绑定: %s" % (port, exc))
        print("可在 data/config.json 里改 http_port，或加参数 --port 8098")
        return 2

    store.store.start_autosave()
    if cfg["discovery_mode"] != "off":
        discovery.start()
    peer.start_kind_watcher()
    if args.scan_now:
        peer.scan_subnet_async()

    print("=" * 62)
    print(" FileShare %s 已启动" % VERSION)
    print(" 别名      : %s" % cfg["alias"])
    print(" 共享目录  : %s" % cfg["share_dir"])
    print(" 接收目录  : %s" % cfg["inbox_dir"])
    print(" 下载目录  : %s" % cfg["download_dir"])
    for ip in util.local_ips():
        print(" 访问地址  : http://%s:%d   （手机浏览器/扫码可直接下载共享文件）" % (ip, port))
    print(" 本机地址  : %s" % url_local)
    print(" 停止服务  : 关闭本窗口或按 Ctrl+C")
    print("=" * 62)

    if not args.no_window and (args.open or args.browser):
        open_window(url_local, prefer_browser=args.browser)

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        try:
            discovery.stop()
        except Exception:
            pass
        store.store.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
