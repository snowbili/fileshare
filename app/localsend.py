# -*- coding: utf-8 -*-
"""LocalSend 协议 v2.2 兼容层（与官方 App / 手机互通）。

实现端点：
    POST /api/localsend/v2/register          双向发现握手
    GET  /api/localsend/v2/info              调试用设备信息
    POST /api/localsend/v2/prepare-upload    接收前确认（弹窗接受/拒绝）
    POST /api/localsend/v2/upload            接收文件（裸 body）
    POST /api/localsend/v2/cancel            取消会话
    POST /api/localsend/v2/prepare-download  反向下载 API（手机浏览器直接下载）
    GET  /api/localsend/v2/download          反向下载 API 取文件

说明：官方协议只有"推送某个会话的文件"，没有"浏览对方目录"的接口，
      因此右面板把官方 LocalSend 设备标为"仅可推送"。
"""
from __future__ import annotations

import json
import os
import time

import config
import fsapi
import store
import util

CHUNK = 1024 * 1024
SHARE_SESSION = "__share__"          # 反向下载用的伪会话（暴露我的共享目录）


def _check_pin(handler, params: dict) -> bool:
    pin = str(config.get("pin", "") or "")
    if not pin:
        return True
    supplied = str(params.get("pin") or handler.headers.get("X-Pin") or "").strip()
    return supplied == pin


def api_info(handler) -> None:
    handler.send_json(200, util.device_info())


def api_register(handler) -> None:
    """收到对方的 register：登记设备并回礼。"""
    try:
        info = handler.read_json_body(limit=256 * 1024)
    except ValueError as exc:
        handler.send_json(400, {"error": str(exc)})
        return
    ip = handler.client_address[0]
    if info.get("fingerprint"):
        store.store.upsert_peer(info, ip)
    handler.send_json(200, util.device_info())


def _log_incoming_start(info: dict, files: dict, ip: str) -> None:
    alias = info.get("alias") or ip
    total = sum(int(f.get("size") or 0) for f in files.values())
    store.store.add_transfer(name="准备接收：%s 的 %d 个文件" % (alias, len(files)),
                             size=total, direction="receive", device=alias)


def api_prepare_upload(handler, params: dict) -> None:
    if not _check_pin(handler, params):
        handler.send_json(401, {"error": "PIN required / Invalid PIN"})
        return
    try:
        body = handler.read_json_body()
    except ValueError as exc:
        handler.send_json(400, {"error": str(exc)})
        return
    files = body.get("files") or {}
    if not isinstance(files, dict) or not files:
        handler.send_empty(204)                # 没有文件，按协议返回 204
        return
    info = body.get("info") or {}
    ip = handler.client_address[0]
    store.store.upsert_peer(info, ip)

    if config.get("auto_accept", False):
        _log_incoming_start(info, files, ip)
        handler.send_json(200, store.store.create_session(ip, files, info))
        return

    session_id = util.random_id(16)
    store.store.add_pending(info, files, ip, session_id)
    timeout = max(10, int(config.get("accept_timeout", 60)))
    deadline = time.time() + timeout
    decision = None
    while time.time() < deadline:
        decision = store.store.get_pending(session_id).get("decision")
        if decision is not None:
            break
        time.sleep(0.3)
    if decision == "accept":
        _log_incoming_start(info, files, ip)
        handler.send_json(200, store.store.create_session(ip, files, info))
        return
    store.store.decide_pending(session_id, decision or "timeout")
    handler.send_json(403, {"error": "Rejected" if decision == "reject" else "Timeout"})


def api_upload(handler, params: dict) -> None:
    """接收一个文件（裸 body，落盘到 inbox）。"""
    session_id = params.get("sessionId") or ""
    file_id = params.get("fileId") or ""
    token = params.get("token") or ""
    if not session_id or not file_id or not token:
        handler.send_json(400, {"error": "参数缺失"})
        return
    session = store.store.get_session(session_id)
    if not session:
        handler.send_json(400, {"error": "会话不存在"})
        return
    if session.get("cancelled"):
        handler.send_json(409, {"error": "会话已取消"})
        return
    if session.get("peer_ip") != handler.client_address[0]:
        handler.send_json(403, {"error": "IP 不匹配"})
        return
    meta = (session.get("files") or {}).get(file_id)
    if not meta or meta.get("token") != token:
        handler.send_json(403, {"error": "无效令牌"})
        return
    length = handler.body_length()
    if length is None:
        handler.send_json(411, {"error": "必须携带 Content-Length"})
        return

    inbox = config.get("inbox_dir")
    try:
        os.makedirs(inbox, exist_ok=True)
    except OSError as exc:
        handler.send_json(500, {"error": "接收目录不可用: %s" % exc})
        return
    name = util.sanitize_filename(meta.get("fileName") or file_id)
    final_path = util.unique_path(inbox, name)
    part_path = final_path + ".part"
    alias = (session.get("info") or {}).get("alias") or handler.client_address[0]
    item = store.store.add_transfer(name=name, size=length, direction="receive",
                                    device=alias, path=file_id)
    written = 0
    try:
        with open(util.long_path(part_path), "wb") as fp:
            while written < length:
                block = handler.rfile.read(min(CHUNK, length - written))
                if not block:
                    break
                fp.write(block)
                written += len(block)
                if store.store.get_transfer(item["id"]).get("status") == "cancelled":
                    raise ConnectionAbortedError("本地取消")
                if written % (CHUNK * 4) < len(block):
                    store.store.update_transfer(item["id"], done=written)
    except ConnectionAbortedError:
        store.store.update_transfer(item["id"], status="cancelled", done=written)
        try:
            os.remove(util.long_path(part_path))
        except OSError:
            pass
        handler.send_json(499, {"error": "已取消"})
        return
    except OSError as exc:
        store.store.update_transfer(item["id"], status="error", error=str(exc))
        handler.send_json(500, {"error": "写入失败: %s" % exc})
        return

    handler.mark_body_read(written)          # 硬坑 #1：请求体已消费

    if written < length:
        store.store.update_transfer(item["id"], status="error", done=written, error="连接中断")
        handler.close_connection = True
        return
    try:
        os.replace(util.long_path(part_path), util.long_path(final_path))
    except OSError as exc:
        store.store.update_transfer(item["id"], status="error", error="改名失败: %s" % exc)
        handler.send_json(500, {"error": "改名失败: %s" % exc})
        return
    mtime_header = handler.headers.get("X-File-Mtime") or ""
    if mtime_header.isdigit():
        try:
            os.utime(util.long_path(final_path), (int(mtime_header), int(mtime_header)))
        except OSError:
            pass
    store.store.mark_received(session_id, file_id, written)
    store.store.update_transfer(item["id"], status="done", done=written, path=final_path)
    handler.send_empty(200)


def api_cancel(handler, params: dict) -> None:
    store.store.cancel_session(params.get("sessionId") or "")
    handler.send_empty(200)


# --------------------------------------------------------------------------- #
# 反向下载 API（手机浏览器/其他设备无需安装即可下载我的共享文件）
# --------------------------------------------------------------------------- #
def _share_files() -> dict:
    out = {}
    try:
        listing = fsapi.list_entries("share", "")
    except (OSError, ValueError, TypeError):
        return out
    for entry in listing.get("entries", []):
        if entry["is_dir"]:
            continue
        out[entry["rel"]] = {
            "id": entry["rel"],
            "fileName": entry["name"],
            "size": int(entry["size"]),
            "fileType": entry["mime"],
            "sha256": None,
            "preview": None,
            "mtime": entry["mtime"],
        }
    return out


def api_prepare_download(handler, params: dict) -> None:
    if not _check_pin(handler, params):
        handler.send_json(401, {"error": "PIN required / Invalid PIN"})
        return
    handler.read_json_body()                      # 官方客户端会带空 body
    handler.send_json(200, {
        "info": util.device_info(),
        "sessionId": params.get("sessionId") or SHARE_SESSION,
        "files": _share_files(),
    })


def api_download(handler, params: dict) -> None:
    if not _check_pin(handler, params):
        handler.send_json(401, {"error": "PIN required / Invalid PIN"})
        return
    file_id = params.get("fileId") or ""
    session_id = params.get("sessionId") or SHARE_SESSION
    if not file_id:
        handler.send_json(400, {"error": "缺少 fileId"})
        return
    session = store.store.get_session(session_id)
    if session and file_id in (session.get("files") or {}):
        meta = session["files"][file_id]
        candidate = os.path.join(config.get("inbox_dir") or "",
                                 util.sanitize_filename(meta["fileName"]))
        if os.path.isfile(util.long_path(candidate)):
            handler.stream_file(candidate, meta["fileName"], inline=False,
                               range_value=handler.headers.get("Range"))
            return
        handler.send_json(404, {"error": "文件尚未接收完成"})
        return
    try:
        target = util.safe_join(config.get("share_dir"), file_id, must_exist=True)
    except ValueError as exc:
        handler.send_json(403, {"error": str(exc)})
        return
    except FileNotFoundError:
        handler.send_json(404, {"error": "文件不存在"})
        return
    if os.path.isdir(target):
        handler.send_json(400, {"error": "目标是目录"})
        return
    handler.stream_file(target, os.path.basename(target), inline=False,
                        range_value=handler.headers.get("Range"))


# --------------------------------------------------------------------------- #
# 主动推送给官方 LocalSend 设备
# --------------------------------------------------------------------------- #
def push_local_file(peer: dict, src_path: str, name: str, transfer_id: str):
    """按官方协议推送单个文件：prepare-upload -> upload。返回 (ok, message)。"""
    import http.client
    from urllib.parse import urlencode

    try:
        size = int(os.path.getsize(util.long_path(src_path)))
        mtime = float(os.path.getmtime(util.long_path(src_path)))
    except OSError as exc:
        store.store.update_transfer(transfer_id, status="error", error="读取源文件失败: %s" % exc)
        return False, str(exc)
    store.store.update_transfer(transfer_id, size=size)
    file_id = util.random_id(8)
    payload = {
        "info": util.device_info(),
        "files": {
            file_id: {
                "id": file_id,
                "fileName": name,
                "size": size,
                "fileType": util.guess_mime(name),
                "sha256": util.sha256_file(src_path) if size <= 128 * 1024 * 1024 else None,
                "preview": None,
            }
        },
    }
    path = "/api/localsend/v2/prepare-upload"
    if config.get("pin"):
        path += "?" + urlencode({"pin": config.get("pin")})
    conn = http.client.HTTPConnection(peer["ip"], int(peer["port"]), timeout=300)
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        conn.request("POST", path, body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body)),
                              "User-Agent": "FileShare/0.1"})
        resp = conn.getresponse()
        data = resp.read(65536)
        if resp.status == 403:
            store.store.update_transfer(transfer_id, status="error", error="对方拒绝接收")
            return False, "对方拒绝接收"
        if resp.status == 401:
            store.store.update_transfer(transfer_id, status="error", error="需要 PIN 或 PIN 错误")
            return False, "需要 PIN 或 PIN 错误"
        if resp.status == 204:
            store.store.update_transfer(transfer_id, status="done", done=size)
            return True, "对方无需接收文件"
        if resp.status != 200:
            message = data.decode("utf-8", "replace")[:200]
            store.store.update_transfer(transfer_id, status="error",
                                        error="prepare-upload 返回 %s %s" % (resp.status, message))
            return False, "对方返回 HTTP %s" % resp.status
        result = json.loads(data.decode("utf-8", "replace"))
        session_id = result.get("sessionId") or ""
        token = (result.get("files") or {}).get(file_id) or ""
        if not session_id or not token:
            store.store.update_transfer(transfer_id, status="error", error="对方未返回会话令牌")
            return False, "对方未返回会话令牌"
        url = "/api/localsend/v2/upload?" + urlencode(
            {"sessionId": session_id, "fileId": file_id, "token": token})
        with open(util.long_path(src_path), "rb") as fp:
            conn.request("POST", url, body=fp,
                         headers={"Content-Type": "application/octet-stream",
                                  "Content-Length": str(size),
                                  "X-File-Mtime": str(int(mtime)),
                                  "User-Agent": "FileShare/0.1"})
            up = conn.getresponse()
            up.read(65536)
        if up.status != 200:
            store.store.update_transfer(transfer_id, status="error",
                                        error="upload 返回 HTTP %s" % up.status)
            return False, "对方返回 HTTP %s" % up.status
        store.store.update_transfer(transfer_id, status="done", done=size)
        return True, ""
    except ConnectionAbortedError:
        store.store.update_transfer(transfer_id, status="cancelled")
        return False, "已取消"
    except Exception as exc:
        store.store.update_transfer(transfer_id, status="error", error=str(exc))
        return False, str(exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass
