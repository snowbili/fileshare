# -*- coding: utf-8 -*-
"""与对方设备通信（服务端互推 / 浏览 / 下载）。

跨机一律走"后端对后端"：
  浏览器 --> 本机后端(/api/peer/*) --> 对方后端(/api/fs/*)
好处：不受 CORS 限制、可携带 token、进度与取消都能在队列里体现。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

import config
import store
import util

CHUNK = 1024 * 1024
PEER_TIMEOUT = 10
_kind_thread = None
_stop = threading.Event()
_scan_state = {"running": False, "done": 0, "total": 0, "found": 0, "last": 0.0}


def peer_base(peer: dict) -> str:
    return "http://%s:%s" % (peer.get("ip"), peer.get("port") or 53317)


def request(url: str, method: str = "GET", data=None, headers=None,
            timeout: float = PEER_TIMEOUT):
    """发起一次后端到后端的请求（自动带上 X-FS-Peer 身份头）。"""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-FS-Peer", util.fingerprint())
    req.add_header("X-FS-Alias", config.get("alias", ""))
    req.add_header("User-Agent", "FileShare/0.1")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    return urllib.request.urlopen(req, timeout=timeout)


def register_remote(ip: str, port: int, timeout: float = 5) -> bool:
    """向对方 POST /api/localsend/v2/register 做双向发现。"""
    payload = json.dumps(util.device_info(), ensure_ascii=False).encode("utf-8")
    url = "http://%s:%d/api/localsend/v2/register" % (ip, int(port))
    try:
        with request(url, "POST", data=payload,
                     headers={"Content-Type": "application/json"}, timeout=timeout) as resp:
            body = resp.read(65536)
        info = json.loads(body.decode("utf-8", "replace")) if body else {}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False
    if isinstance(info, dict) and info.get("alias"):
        store.store.upsert_peer(info, ip)
    return True


def detect_kind(peer: dict) -> str:
    """判断对方是本工具（可浏览）还是官方 LocalSend（只能推/收）。"""
    url = peer_base(peer) + "/api/state"
    try:
        with urllib.request.urlopen(url, timeout=2.5) as resp:
            data = json.loads(resp.read(65536).decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("app") == "fileshare":
            return store.KIND_APP
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        pass
    return store.KIND_LOCALSEND


def _kind_watch_loop() -> None:
    first = True
    while not _stop.wait(2.0 if first else 6.0):
        first = False
        now = time.time()
        for peer in store.store.list_peers():
            if now - peer.get("last_seen", 0) > 300:
                continue
            checked = peer.get("kind_checked") or 0
            if peer.get("kind") in (store.KIND_APP, store.KIND_LOCALSEND) and now - checked < 120:
                continue
            kind = detect_kind(peer)
            store.store.set_kind(peer["fingerprint"], kind)
            with store.store._lock:                       # 记录探测时间，避免频繁探测
                entry = store.store.peers.get(peer["fingerprint"])
                if entry:
                    entry["kind_checked"] = now


def start_kind_watcher() -> None:
    global _kind_thread
    if _kind_thread is not None and _kind_thread.is_alive():
        return
    _stop.clear()
    _kind_thread = threading.Thread(target=_kind_watch_loop, name="peer-kind", daemon=True)
    _kind_thread.start()


def scan_subnet_async() -> dict:
    """向本网段逐个 IP 发 register（协议 3.2 的兜底发现）。"""
    if _scan_state["running"]:
        return dict(_scan_state)
    hosts = []
    for ip in util.local_ips():
        hosts.extend(util.subnet_hosts(ip))
    hosts = sorted(set(hosts))
    port = int(config.get("http_port", 8099))
    _scan_state.update({"running": True, "done": 0, "total": len(hosts), "found": 0,
                        "last": time.time()})

    def worker() -> None:
        import concurrent.futures

        def probe(host: str) -> bool:
            if host == util.primary_ip():
                return False
            ok = register_remote(host, port, timeout=0.8)
            _scan_state["done"] += 1
            if ok:
                _scan_state["found"] += 1
            return ok

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
                list(pool.map(probe, hosts))
        finally:
            _scan_state["running"] = False
            _scan_state["last"] = time.time()

    threading.Thread(target=worker, name="peer-scan", daemon=True).start()
    return dict(_scan_state)


def scan_status() -> dict:
    return dict(_scan_state)


# --------------------------------------------------------------------------- #
# 本机 API：设备管理
# --------------------------------------------------------------------------- #
def api_add(handler, params: dict) -> None:
    """手动添加设备：body {"addr": "192.168.1.100" 或 "192.168.1.100:8099"}"""
    try:
        body = handler.read_json_body()
    except ValueError as exc:
        handler.send_json(400, {"error": str(exc)})
        return
    addr = str(body.get("addr") or "").strip()
    if not addr:
        handler.send_json(400, {"error": "请填写对方 IP[:端口]"})
        return
    port = int(config.get("http_port", 8099))
    if ":" in addr:
        host, _, raw_port = addr.rpartition(":")
        try:
            port = int(raw_port)
        except ValueError:
            handler.send_json(400, {"error": "端口不合法"})
            return
        addr = host.strip()
    if not addr:
        handler.send_json(400, {"error": "IP 不合法"})
        return
    if not register_remote(addr, port, timeout=3):
        handler.send_json(404, {"error": "连不上 %s:%d（对方未启动或防火墙拦截）" % (addr, port)})
        return
    peer = store.store.peer_by_addr(addr, port)
    if peer:
        store.store.set_kind(peer["fingerprint"], detect_kind(peer))
    handler.send_json(200, {"ok": True, "peer": store.store.peer_by_addr(addr, port),
                            "peers": store.store.list_peers()})


def api_scan(handler, params: dict) -> None:
    """异步扫描本网段（组播不可用时的兜底）。"""
    try:
        handler.read_json_body()
    except ValueError:
        pass
    state = scan_subnet_async()
    handler.send_json(200, {"ok": True, "scan": state, "discovery": _discovery_status()})


def api_forget(handler, params: dict) -> None:
    try:
        body = handler.read_json_body()
    except ValueError as exc:
        handler.send_json(400, {"error": str(exc)})
        return
    fingerprint = str(body.get("fingerprint") or params.get("fingerprint") or "").strip()
    if not fingerprint:
        handler.send_json(400, {"error": "缺少 fingerprint"})
        return
    with store.store._lock:
        store.store.peers.pop(fingerprint, None)
    handler.send_json(200, {"ok": True})


def _discovery_status() -> dict:
    try:
        import discovery

        info = discovery.status()
    except Exception:
        info = {}
    info["scan"] = scan_status()
    return info


def _need_peer(handler, params: dict):
    device = (params.get("device") or "").strip()
    peer = store.store.get_peer(device)
    if not peer:
        handler.send_json(404, {"error": "设备未找到，请刷新设备列表"})
        return None
    if not peer.get("ip") or not peer.get("port"):
        handler.send_json(400, {"error": "对方地址不完整"})
        return None
    return peer


def api_list(handler, params: dict) -> None:
    """代理列对方共享目录（对方不是本工具时会 400）。"""
    peer = _need_peer(handler, params)
    if peer is None:
        return
    if peer.get("kind") != store.KIND_APP:
        handler.send_json(409, {
            "error": "对方是官方 LocalSend，无法浏览目录；可把左面板文件拖到右侧设备上推送过去",
            "kind": peer.get("kind"), "peer": peer})
        return
    root = params.get("root") or "share"
    rel = params.get("path") or ""
    from urllib.parse import urlencode

    url = peer_base(peer) + "/api/fs/list?" + urlencode({"root": root, "path": rel})
    try:
        with request(url, timeout=PEER_TIMEOUT) as resp:
            data = json.loads(resp.read(4 * 1024 * 1024).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        handler.send_json(502, {"error": "读取对方目录失败: %s" % exc, "peer": peer})
        return
    data["peer"] = peer
    store.store.touch_peer(peer["fingerprint"])
    handler.send_json(200, data)


def api_raw(handler, params: dict) -> None:
    """把对方的文件流式转给浏览器（右面板拖出到资源管理器时走这里）。

    额外好处：本机可同时留一份副本到 downloads，并在传输队列里显示进度。
    """
    from urllib.parse import urlencode

    peer = _need_peer(handler, params)
    if peer is None:
        return
    root = params.get("root") or "share"
    rel = params.get("path") or ""
    name = util.sanitize_filename(params.get("name") or os.path.basename(rel) or "download")
    inline = "1" if str(params.get("inline") or "") in ("1", "true", "yes") else "0"
    query = urlencode({"root": root, "path": rel, "name": name, "inline": inline})
    url = peer_base(peer) + "/api/fs/raw?" + query
    range_header = (handler.headers.get("Range") or "").strip()
    headers = {"Range": range_header} if range_header else {}
    method = "HEAD" if handler.command == "HEAD" else "GET"
    try:
        resp = request(url, method=method, timeout=300, headers=headers)
    except urllib.error.HTTPError as exc:
        code = exc.code if 400 <= exc.code < 600 else 502
        handler.send_json(code, {"error": "对方返回 HTTP %s" % exc.code, "peer": peer})
        return
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        handler.send_json(502, {"error": "无法从对方下载: %s" % exc, "peer": peer})
        return

    try:
        status = getattr(resp, "status", 200) or 200
        head = resp.headers
        raw_length = head.get("Content-Length")
        total = int(raw_length) if raw_length and raw_length.isdigit() else 0
        mtime = head.get("X-File-Mtime") or ""
        cfg = config.load()

        if method == "HEAD":                              # 只回元信息
            handler.send_response(200 if status in (200, 206) else 200)
            handler._cors_headers()
            handler.send_header("Content-Type", head.get("Content-Type") or util.guess_mime(name))
            handler.send_header("Content-Disposition", util.content_disposition(name))
            handler.send_header("Accept-Ranges", "bytes")
            if mtime:
                handler.send_header("X-File-Mtime", mtime)
            handler.send_header("Content-Length", str(total))
            handler.end_headers()
            return

        item = store.store.add_transfer(name=name, size=total, direction="download",
                                        device=peer.get("alias") or peer.get("ip") or "",
                                        peer_fp=peer["fingerprint"])
        copy_fp = None
        copy_path = ""
        if status == 200 and total and cfg.get("keep_copy_on_dragout", True):
            target_dir = cfg["download_dir"]
            os.makedirs(target_dir, exist_ok=True)
            copy_path = util.unique_path(target_dir, name)
            try:
                copy_fp = open(util.long_path(copy_path), "wb")
            except OSError:
                copy_fp = None
                copy_path = ""

        handler.send_response(status if status in (200, 206) else 200)
        handler._cors_headers()
        handler.send_header("Content-Type", head.get("Content-Type") or util.guess_mime(name))
        handler.send_header("Content-Disposition", util.content_disposition(name))
        handler.send_header("Accept-Ranges", "bytes")
        if head.get("Content-Range"):
            handler.send_header("Content-Range", head["Content-Range"])
        if mtime:
            handler.send_header("X-File-Mtime", mtime)
        handler.send_header("Content-Length", str(total))
        handler.end_headers()

        done = 0
        cancelled = False
        try:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                handler.wfile.write(block)
                if copy_fp is not None:
                    copy_fp.write(block)
                done += len(block)
                current = store.store.get_transfer(item["id"])
                if current.get("status") == "cancelled":
                    cancelled = True
                    break
                if done % (CHUNK * 4) < len(block) or done == total:
                    store.store.update_transfer(item["id"], done=done)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            handler.close_connection = True
            store.store.update_transfer(item["id"], status="error", done=done,
                                        error="连接中断: %r" % exc)
        finally:
            try:
                resp.close()
            except Exception:
                pass
            if copy_fp is not None:
                copy_fp.close()
            if cancelled:
                store.store.update_transfer(item["id"], status="cancelled", done=done)
            elif total and done >= total:
                if copy_path and mtime.isdigit():         # 副本也保留原始时间戳
                    try:
                        os.utime(util.long_path(copy_path), (int(mtime), int(mtime)))
                    except OSError:
                        pass
                store.store.update_transfer(item["id"], status="done", done=done, path=copy_path)
            elif not total:
                store.store.update_transfer(item["id"], status="done", done=done, path=copy_path)
            else:
                store.store.update_transfer(item["id"], status="error", done=done,
                                            error="数据不完整（收到 %d/%d）" % (done, total))
                if copy_path:
                    try:
                        os.remove(util.long_path(copy_path))
                    except OSError:
                        pass
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        handler.close_connection = True


# --------------------------------------------------------------------------- #
# 后端对后端的推送 / 拉取
# --------------------------------------------------------------------------- #
class _ProgressReader:
    """把请求体/本地文件包装成"限量 + 可取消 + 报进度"的读取器。

    注意：必须有长度上限，否则 http.client 会一直 read() 到阻塞。
    """

    def __init__(self, fp, length: int, transfer_id: str = "", chunk: int = CHUNK):
        self.fp = fp
        self.length = max(0, int(length))
        self.remaining = self.length
        self.chunk = chunk
        self.done = 0
        self.transfer_id = transfer_id

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        want = self.chunk if size is None or size <= 0 else max(size, self.chunk)
        want = min(want, self.remaining)
        block = self.fp.read(want)
        if not block:
            self.remaining = 0
            return b""
        self.remaining -= len(block)
        self.done += len(block)
        if self.transfer_id:
            item = store.store.get_transfer(self.transfer_id)
            if item.get("status") == "cancelled":
                raise ConnectionAbortedError("用户取消")
            if self.done % (self.chunk * 4) < len(block) or self.remaining == 0:
                store.store.update_transfer(self.transfer_id, done=self.done)
        return block


def _post_stream(peer: dict, name: str, reader, length: int, root: str = "inbox",
                 timeout: float = 600, mtime: float = 0.0, sha256: str = ""):
    """把 reader 的内容以裸 body 推给对方的 /api/fs/upload（显式 Content-Length）。"""
    import http.client
    from urllib.parse import urlencode

    query = urlencode({"root": root, "name": name})
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(int(length)),
        "X-FS-Peer": util.fingerprint(),
        "X-FS-Alias": config.get("alias", ""),
        "User-Agent": "FileShare/0.1",
    }
    if mtime:
        headers["X-File-Mtime"] = str(int(mtime))
    if sha256:
        headers["X-File-Sha256"] = sha256
    conn = http.client.HTTPConnection(peer["ip"], int(peer["port"]), timeout=timeout)
    try:
        conn.request("POST", "/api/fs/upload?" + query, body=reader, headers=headers)
        resp = conn.getresponse()
        data = resp.read(65536)
        return resp.status, data
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _download_to_peer_dir(peer: dict, rel: str, name: str, root: str, target_dir: str,
                          transfer_id: str) -> None:
    """把对方的某个文件下载到本机 target_dir（.part + 原子改名）。"""
    from urllib.parse import urlencode

    url = peer_base(peer) + "/api/fs/raw?" + urlencode(
        {"root": root, "path": rel, "name": name, "inline": "0"})
    part_path = ""
    try:
        resp = request(url, timeout=300)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError) as exc:
        store.store.update_transfer(transfer_id, status="error", error="无法下载: %s" % exc)
        return
    try:
        head = resp.headers
        raw_length = head.get("Content-Length")
        total = int(raw_length) if raw_length and raw_length.isdigit() else 0
        mtime = head.get("X-File-Mtime") or ""
        final_path = util.unique_path(target_dir, name)
        part_path = final_path + ".part"
        store.store.update_transfer(transfer_id, size=total)
        done = 0
        with open(util.long_path(part_path), "wb") as fp:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                fp.write(block)
                done += len(block)
                if store.store.get_transfer(transfer_id).get("status") == "cancelled":
                    raise ConnectionAbortedError("用户取消")
                if done % (CHUNK * 4) < len(block) or done == total:
                    store.store.update_transfer(transfer_id, done=done)
        if total and done != total:
            raise OSError("数据不完整（%d/%d）" % (done, total))
        os.replace(util.long_path(part_path), util.long_path(final_path))
        if mtime.isdigit():
            try:
                os.utime(util.long_path(final_path), (int(mtime), int(mtime)))
            except OSError:
                pass
        store.store.update_transfer(transfer_id, status="done", done=done, path=final_path)
    except ConnectionAbortedError as exc:
        store.store.update_transfer(transfer_id, status="cancelled", error=str(exc))
    except (OSError, ValueError) as exc:
        store.store.update_transfer(transfer_id, status="error", error="写入失败: %s" % exc)
    finally:
        try:
            resp.close()
        except Exception:
            pass
        if part_path and os.path.exists(util.long_path(part_path)):
            try:
                os.remove(util.long_path(part_path))
            except OSError:
                pass


def _push_file_to_peer(peer: dict, src_path: str, name: str, transfer_id: str):
    """同步把本地文件推给对方；返回 (ok, message)。"""
    if peer.get("kind") == store.KIND_LOCALSEND:
        import localsend

        return localsend.push_local_file(peer, src_path, name, transfer_id)
    try:
        size = int(os.path.getsize(util.long_path(src_path)))
        mtime = float(os.path.getmtime(util.long_path(src_path)))
    except OSError as exc:
        store.store.update_transfer(transfer_id, status="error", error="读取源文件失败: %s" % exc)
        return False, str(exc)
    sha = util.sha256_file(src_path) if size <= 128 * 1024 * 1024 else ""
    store.store.update_transfer(transfer_id, size=size)
    try:
        with open(util.long_path(src_path), "rb") as fp:
            reader = _ProgressReader(fp, size, transfer_id)
            status, body = _post_stream(peer, name, reader, size, root="inbox",
                                        mtime=mtime, sha256=sha)
        if status == 200:
            store.store.update_transfer(transfer_id, status="done", done=size, sha256=sha)
            return True, ""
        text = body.decode("utf-8", "replace")[:200]
        store.store.update_transfer(transfer_id, status="error",
                                    error="对方返回 %s: %s" % (status, text))
        return False, "对方返回 HTTP %s" % status
    except ConnectionAbortedError:
        store.store.update_transfer(transfer_id, status="cancelled")
        return False, "已取消"
    except Exception as exc:                      # 网络类异常统一转成传输错误
        store.store.update_transfer(transfer_id, status="error", error=str(exc))
        return False, str(exc)


def api_pull(handler, params: dict) -> None:
    """页内拖拽下载：把对方文件拉到本机 share / inbox / downloads。"""
    try:
        body = handler.read_json_body()
    except ValueError as exc:
        handler.send_json(400, {"error": str(exc)})
        return
    peer = store.store.get_peer(str(body.get("device") or ""))
    if not peer:
        handler.send_json(404, {"error": "设备未找到"})
        return
    rel = str(body.get("path") or "")
    if not rel:
        handler.send_json(400, {"error": "缺少 path"})
        return
    root = str(body.get("root") or "share")
    name = util.sanitize_filename(body.get("name") or os.path.basename(rel) or "download")
    target = str(body.get("target") or "downloads")
    if target not in ("share", "inbox", "downloads"):
        handler.send_json(400, {"error": "target 只能是 share / inbox / downloads"})
        return
    cfg = config.load()
    target_dir = {"share": cfg["share_dir"], "inbox": cfg["inbox_dir"],
                  "downloads": cfg["download_dir"]}[target]
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as exc:
        handler.send_json(500, {"error": "目标目录不可用: %s" % exc})
        return
    item = store.store.add_transfer(name=name, size=0, direction="download",
                                    device=peer.get("alias") or peer.get("ip") or "",
                                    peer_fp=peer["fingerprint"], path=rel)
    threading.Thread(target=_download_to_peer_dir,
                     args=(peer, rel, name, root, target_dir, item["id"]),
                     name="peer-pull", daemon=True).start()
    handler.send_json(202, {"ok": True, "transfer": store.store.get_transfer(item["id"])})


def api_push(handler, params: dict) -> None:
    """推送：A) JSON 指定本机文件（左面板文件 -> 右侧设备）；B) 裸 body 中继浏览器拖来的文件。"""
    import fsapi

    peer = store.store.get_peer(params.get("device") or "")
    if not peer:
        handler.send_json(404, {"error": "设备未找到"})
        return
    ctype = (handler.headers.get("Content-Type") or "").lower()
    length = handler.body_length()
    if length is None:
        handler.send_json(411, {"error": "推送必须携带 Content-Length"})
        return

    if ctype.startswith("application/json"):
        try:
            body = handler.read_json_body()
        except ValueError as exc:
            handler.send_json(400, {"error": str(exc)})
            return
        rel = str(body.get("path") or "")
        root = str(body.get("root") or "share")
        try:
            base = fsapi.root_of(root)
            src = util.safe_join(base, rel, must_exist=True)
        except ValueError as exc:
            handler.send_json(403, {"error": str(exc)})
            return
        except FileNotFoundError:
            handler.send_json(404, {"error": "源文件不存在"})
            return
        if os.path.isdir(src):
            handler.send_json(400, {"error": "只能推送文件，暂不支持整个目录"})
            return
        name = util.sanitize_filename(os.path.basename(src))
        item = store.store.add_transfer(name=name, size=0, direction="push",
                                        device=peer.get("alias") or "", peer_fp=peer["fingerprint"],
                                        path=rel)
        threading.Thread(target=_push_file_to_peer, args=(peer, src, name, item["id"]),
                         name="peer-push", daemon=True).start()
        handler.send_json(202, {"ok": True, "transfer": store.store.get_transfer(item["id"])})
        return

    # B) 中继：浏览器 -> 本机后端 -> 对方（避免把文件先落到本机 inbox）
    name = util.sanitize_filename(params.get("name") or handler.headers.get("X-File-Name") or "unnamed")
    item = store.store.add_transfer(name=name, size=length, direction="push",
                                    device=peer.get("alias") or "", peer_fp=peer["fingerprint"])
    handler._body_consumed = True                # 请求体在这里被消费
    reader = _ProgressReader(handler.rfile, length, item["id"])

    if peer.get("kind") == store.KIND_LOCALSEND:
        import localsend

        tmp_dir = os.path.join(config.DATA_DIR, "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, item["id"] + "_" + name)
        ok, message = False, ""
        try:
            with open(util.long_path(tmp_path), "wb") as fp:
                while True:
                    block = reader.read(reader.chunk)
                    if not block:
                        break
                    fp.write(block)
            if reader.remaining != 0:
                ok, message = False, "上传中断"
                store.store.update_transfer(item["id"], status="error", error=message)
            else:
                ok, message = localsend.push_local_file(peer, tmp_path, name, item["id"])
        except ConnectionAbortedError:
            ok, message = False, "已取消"
        except Exception as exc:
            ok, message = False, str(exc)
            store.store.update_transfer(item["id"], status="error", error=message)
        finally:
            try:
                os.remove(util.long_path(tmp_path))
            except OSError:
                pass
        if ok:
            handler.send_json(200, {"ok": True, "transfer": store.store.get_transfer(item["id"])})
        else:
            handler.send_json(502, {"error": message or "推送失败"})
        return

    if reader.remaining != length:
        handler.close_connection = True
    try:
        status, body = _post_stream(peer, name, reader, length, root="inbox")
        if status == 200:
            store.store.update_transfer(item["id"], status="done", done=length)
            handler.send_json(200, {"ok": True, "transfer": store.store.get_transfer(item["id"])})
        else:
            text = body.decode("utf-8", "replace")[:200]
            store.store.update_transfer(item["id"], status="error", error=text)
            handler.send_json(502, {"error": "对方返回 %s: %s" % (status, text)})
    except ConnectionAbortedError:
        store.store.update_transfer(item["id"], status="cancelled")
        handler.send_json(499, {"error": "已取消"})
    except Exception as exc:
        store.store.update_transfer(item["id"], status="error", error=str(exc))
        handler.close_connection = True
        handler.send_json(502, {"error": "推送失败: %s" % exc})
