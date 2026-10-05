# -*- coding: utf-8 -*-
"""本地共享目录 API（自定义扩展）。

端点：
    GET  /api/fs/list   ?root=share|inbox|downloads&path=   列目录
    GET  /api/fs/stat   ?root=&path=                        单文件元信息
    GET  /api/fs/raw    ?root=&path=&name=&inline=1          流式下载（Range / 断点续传）
    HEAD /api/fs/raw                                        探测
    HEAD /api/fs/upload ?root=&path=&name=                   续传探测（X-Received / X-Final-Name）
    POST /api/fs/upload ?root=&path=&name=                   裸 body 上传，写 .part 后原子改名
    POST /api/fs/mkdir  ?root=&path=                         新建目录
    POST /api/fs/rename ?root=&path=&new_name=               重命名
    POST /api/fs/delete ?root=&path=&recursive=1             删除

硬坑落地：
    #2 RFC 5987 文件名     -> server.stream_file（中文/emoji 不乱码）
    #6 路径穿越            -> util.safe_join
    #7 chunked 拒绝        -> server 统一检查
    #8 保留 mtime          -> X-File-Mtime + os.utime
    #9 .part + os.replace  -> 上传原子落盘，中断可续传
    #12 服务端算 sha256     -> 校验 X-File-Sha256
"""
from __future__ import annotations

import json
import os
import shutil
import threading

import config
import store
import util

CHUNK = 1024 * 1024          # 1MB：Windows 上 sendfile 会退化成 64KB 循环，不如直接大块写
WRITE_ROOTS = ("share", "inbox")
READ_ROOTS = ("share", "inbox", "downloads")
_locks: "dict[str, threading.Lock]" = {}
_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    key = os.path.normcase(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def root_of(kind: str, for_write: bool = False) -> str:
    cfg = config.load()
    kind = (kind or "share").lower()
    allowed = WRITE_ROOTS if for_write else READ_ROOTS
    if kind not in allowed:
        raise ValueError("不允许的根目录: %s" % kind)
    if kind == "share":
        return cfg["share_dir"]
    if kind == "inbox":
        return cfg["inbox_dir"]
    return cfg["download_dir"]


def list_entries(kind: str, rel: str) -> dict:
    """列出目录内容（跳过上传中间态文件）。"""
    root = root_of(kind)
    target = util.safe_join(root, rel, must_exist=True)
    if not os.path.isdir(target):
        raise NotADirectoryError("不是目录")
    entries = []
    with os.scandir(util.long_path(target)) as iterator:
        for entry in iterator:
            name = entry.name
            if name.endswith(".part") or name.endswith(".part.json"):
                continue
            try:
                stat = entry.stat()
                is_dir = entry.is_dir()
            except OSError:
                continue
            entries.append({
                "name": name,
                "rel": ("%s/%s" % (rel, name)) if rel else name,
                "is_dir": is_dir,
                "size": 0 if is_dir else int(stat.st_size),
                "mtime": int(stat.st_mtime),
                "mime": "" if is_dir else util.guess_mime(name),
            })
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    return {
        "root": kind,
        "path": rel or "",
        "parent": os.path.dirname(rel).replace("\\", "/") if rel else None,
        "entries": entries,
        "count": len(entries),
    }


def api_list(handler, params: dict) -> None:
    kind = params.get("root") or "share"
    rel = params.get("path") or ""
    try:
        data = list_entries(kind, rel)
    except ValueError as exc:
        handler.send_json(403, {"error": str(exc)})
        return
    except FileNotFoundError:
        handler.send_json(404, {"error": "目录不存在"})
        return
    except NotADirectoryError as exc:
        handler.send_json(400, {"error": str(exc)})
        return
    except OSError as exc:
        handler.send_json(500, {"error": str(exc)})
        return
    data["name"] = os.path.basename(util.safe_join(root_of(kind), rel)) if rel else root_of(kind)
    handler.send_json(200, data)


def api_stat(handler, params: dict) -> None:
    """返回单个文件的元信息（前端拖出前预检用）。"""
    kind = params.get("root") or "share"
    rel = params.get("path") or ""
    try:
        root = root_of(kind)
        target = util.safe_join(root, rel, must_exist=True)
    except ValueError as exc:
        handler.send_json(403, {"error": str(exc)})
        return
    except FileNotFoundError:
        handler.send_json(404, {"error": "文件不存在"})
        return
    if os.path.isdir(target):
        handler.send_json(400, {"error": "目标是目录"})
        return
    stat = os.stat(util.long_path(target))
    handler.send_json(200, {
        "name": os.path.basename(target),
        "rel": util.rel_of(root, target),
        "size": int(stat.st_size),
        "mtime": int(stat.st_mtime),
        "mime": util.guess_mime(target),
    })


def api_raw(handler, params: dict) -> None:
    """流式下载本地文件（GET / HEAD）。"""
    kind = params.get("root") or "share"
    rel = params.get("path") or ""
    try:
        root = root_of(kind)
        target = util.safe_join(root, rel, must_exist=True)
    except ValueError as exc:
        handler.send_json(403, {"error": str(exc)})
        return
    except FileNotFoundError:
        handler.send_json(404, {"error": "文件不存在"})
        return
    if os.path.isdir(target):
        handler.send_json(400, {"error": "目标是目录，不能下载"})
        return
    name = params.get("name") or os.path.basename(target)
    inline = str(params.get("inline") or "") in ("1", "true", "yes")
    handler.stream_file(target, name, inline=inline, range_value=handler.headers.get("Range"))


def api_upload_probe(handler, params: dict) -> None:
    """续传探测：返回已收字节数与最终文件名（硬坑 #9）。"""
    kind = params.get("root") or "share"
    rel = params.get("path") or ""
    name = util.sanitize_filename(params.get("name") or "unnamed")
    try:
        root = root_of(kind, for_write=True)
        target_dir = util.safe_join(root, rel)
    except ValueError as exc:
        handler.send_json(403, {"error": str(exc)})
        return
    if not os.path.isdir(target_dir):
        handler.send_json(404, {"error": "目标目录不存在"})
        return
    part = os.path.join(target_dir, name + ".part")
    meta_path = part + ".json"
    received = 0
    final_name = ""
    if os.path.isfile(util.long_path(part)):
        try:
            received = int(os.path.getsize(util.long_path(part)))
            with open(util.long_path(meta_path), "r", encoding="utf-8") as fp:
                final_name = (json.load(fp) or {}).get("final_name") or ""
        except (OSError, ValueError):
            received = 0
    handler.send_json(200, {
        "name": name,
        "final_name": final_name or name,
        "received": received,
        "resumable": received > 0,
    }, headers={"X-Received": str(received), "X-Final-Name": final_name or name})


def api_upload(handler, params: dict) -> None:
    """裸 body 上传：流式写入 <name>.part，校验通过后 os.replace 原子改名。"""
    import hashlib

    kind = params.get("root") or "share"
    rel = params.get("path") or ""
    raw_name = params.get("name") or handler.headers.get("X-File-Name") or ""
    if not raw_name:
        handler.send_json(400, {"error": "缺少文件名参数 name"})
        return
    name = util.sanitize_filename(raw_name)
    try:
        root = root_of(kind, for_write=True)
        target_dir = util.safe_join(root, rel)
    except ValueError as exc:
        handler.send_json(403, {"error": str(exc)})
        return
    if not os.path.isdir(target_dir):
        handler.send_json(404, {"error": "目标目录不存在"})
        return

    length = handler.body_length()
    if length is None:
        handler.send_json(411, {"error": "上传必须携带 Content-Length"})
        return
    try:
        offset = max(0, int(params.get("offset") or handler.headers.get("X-Offset") or 0))
    except ValueError:
        offset = 0
    try:
        mtime = float(handler.headers.get("X-File-Mtime") or params.get("mtime") or 0)
    except ValueError:
        mtime = 0.0
    expect_sha = (handler.headers.get("X-File-Sha256") or "").strip().lower()
    overwrite = str(params.get("overwrite") or "") in ("1", "true", "yes")
    device = handler.headers.get("X-FS-Alias") or handler.client_address[0]

    part = os.path.join(target_dir, name + ".part")
    meta_path = part + ".json"
    with _lock_for(part):
        final_name = ""
        if offset > 0 and os.path.isfile(util.long_path(part)):
            actual = int(os.path.getsize(util.long_path(part)))
            if actual == offset:
                try:
                    with open(util.long_path(meta_path), "r", encoding="utf-8") as fp:
                        final_name = (json.load(fp) or {}).get("final_name") or ""
                except (OSError, ValueError):
                    final_name = ""
            offset = actual                        # 以磁盘实际大小为准
        if offset == 0:
            final_name = ""
            for stale in (part, meta_path):
                try:
                    if os.path.exists(util.long_path(stale)):
                        os.remove(util.long_path(stale))
                except OSError:
                    pass
        if not final_name:
            if overwrite and os.path.isfile(os.path.join(target_dir, name)):
                final_name = name
            else:
                final_name = os.path.basename(util.unique_path(target_dir, name))
            with open(util.long_path(meta_path), "w", encoding="utf-8") as fp:
                json.dump({"final_name": final_name, "name": name,
                           "size": offset + length, "mtime": mtime}, fp, ensure_ascii=False)

        total = offset + length
        transfer = store.store.add_transfer(name=final_name, size=total, done=offset,
                                     direction="upload", device=device, path=rel)
        tid = transfer["id"]
        digest = hashlib.sha256()
        if offset > 0 and expect_sha:              # 续传：先补算已有部分的哈希
            remaining = offset
            try:
                with open(util.long_path(part), "rb") as fp:
                    while remaining > 0:
                        block = fp.read(min(CHUNK, remaining))
                        if not block:
                            break
                        digest.update(block)
                        remaining -= len(block)
            except OSError:
                pass
        written = 0
        aborted = False
        try:
            with open(util.long_path(part), "ab" if offset > 0 else "wb") as fp:
                while written < length:
                    block = handler.rfile.read(min(CHUNK, length - written))
                    if not block:
                        break
                    fp.write(block)
                    written += len(block)
                    if expect_sha:
                        digest.update(block)
                    if store.store.get_transfer(tid).get("status") == "cancelled":
                        aborted = True
                        break
                    store.store.update_transfer(tid, done=offset + written)
        except OSError as exc:
            store.store.update_transfer(tid, status="error", error="写入失败: %s" % exc)
            handler.send_json(500, {"error": "写入失败: %s" % exc})
            return

        handler.mark_body_read(written)        # 硬坑 #1：告诉框架请求体已消费

        if written < length and not aborted:
            # 客户端中断：保留 .part 供续传（硬坑 #9）
            store.store.update_transfer(tid, status="error", done=offset + written,
                                 error="连接中断，可续传")
            handler.close_connection = True
            return
        if aborted:
            store.store.update_transfer(tid, status="cancelled", done=offset + written)
            handler.send_json(499, {"error": "已取消"})
            return

        if expect_sha:
            actual_sha = digest.hexdigest()
            if actual_sha != expect_sha:
                for stale in (part, meta_path):
                    try:
                        os.remove(util.long_path(stale))
                    except OSError:
                        pass
                store.store.update_transfer(tid, status="error", error="sha256 校验失败")
                handler.send_json(400, {"error": "sha256 校验失败"})
                return

        final_path = os.path.join(target_dir, final_name)
        try:
            os.replace(util.long_path(part), util.long_path(final_path))
        except OSError as exc:
            store.store.update_transfer(tid, status="error", error="改名失败: %s" % exc)
            handler.send_json(500, {"error": "改名失败: %s" % exc})
            return
        try:
            if os.path.exists(util.long_path(meta_path)):
                os.remove(util.long_path(meta_path))
        except OSError:
            pass
        if mtime > 0:                                  # 硬坑 #8：保留原始时间戳
            try:
                os.utime(util.long_path(final_path), (mtime, mtime))
            except OSError:
                pass
        rel_final = util.rel_of(root, final_path)
        store.store.update_transfer(tid, status="done", done=total, path=rel_final, sha256=expect_sha)
        handler.send_json(200, {
            "ok": True,
            "name": final_name,
            "path": rel_final,
            "size": total,
            "mtime": int(os.path.getmtime(util.long_path(final_path))),
            "sha256": expect_sha,
        })


def _write_target(handler, params: dict):
    """删除/重命名/新建 共用的参数解析。"""
    kind = params.get("root") or "share"
    rel = params.get("path") or ""
    root = root_of(kind, for_write=True)
    target = util.safe_join(root, rel)
    return root, target, rel


def api_mkdir(handler, params: dict) -> None:
    try:
        root, target, rel = _write_target(handler, params)
    except ValueError as exc:
        handler.send_json(403, {"error": str(exc)})
        return
    if not rel:
        handler.send_json(400, {"error": "缺少目录名"})
        return
    if os.path.exists(util.long_path(target)):
        handler.send_json(409, {"error": "同名文件或目录已存在"})
        return
    try:
        os.makedirs(util.long_path(target))
    except OSError as exc:
        handler.send_json(500, {"error": "新建目录失败: %s" % exc})
        return
    handler.send_json(200, {"ok": True, "path": util.rel_of(root, target)})


def api_rename(handler, params: dict) -> None:
    new_name = util.sanitize_filename(params.get("new_name") or "")
    try:
        root, target, rel = _write_target(handler, params)
    except ValueError as exc:
        handler.send_json(403, {"error": str(exc)})
        return
    if not rel:
        handler.send_json(400, {"error": "不能重命名根目录"})
        return
    if not os.path.exists(util.long_path(target)):
        handler.send_json(404, {"error": "源文件不存在"})
        return
    new_path = os.path.join(os.path.dirname(target), new_name)
    if os.path.exists(util.long_path(new_path)) and os.path.normcase(new_path) != os.path.normcase(target):
        handler.send_json(409, {"error": "目标名称已存在"})
        return
    try:
        os.replace(util.long_path(target), util.long_path(new_path))
    except OSError as exc:
        handler.send_json(500, {"error": "重命名失败: %s" % exc})
        return
    handler.send_json(200, {"ok": True, "path": util.rel_of(root, new_path)})


def api_delete(handler, params: dict) -> None:
    cfg = config.load()
    if not cfg.get("allow_delete", True):
        handler.send_json(403, {"error": "服务端已禁止删除"})
        return
    recursive = str(params.get("recursive") or "") in ("1", "true", "yes")
    try:
        root, target, rel = _write_target(handler, params)
    except ValueError as exc:
        handler.send_json(403, {"error": str(exc)})
        return
    if not rel:
        handler.send_json(400, {"error": "不能删除根目录"})
        return
    if not os.path.exists(util.long_path(target)):
        handler.send_json(404, {"error": "文件不存在"})
        return
    try:
        if os.path.isdir(target):
            if recursive:
                shutil.rmtree(util.long_path(target))
            else:
                os.rmdir(util.long_path(target))
        else:
            os.remove(util.long_path(target))
    except OSError as exc:
        handler.send_json(500, {"error": "删除失败: %s" % exc})
        return
    handler.send_json(200, {"ok": True})
