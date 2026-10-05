# -*- coding: utf-8 -*-
"""运行期状态：已发现设备、传输队列、LocalSend 会话、待确认请求。

内存态 + 周期性 JSON 快照（data/state.json）。
"""
from __future__ import annotations

import json
import os
import threading
import time

import config
import util

STATE_PATH = os.path.join(config.DATA_DIR, "state.json")
KIND_APP = "app"              # 对方是本工具（可浏览目录）
KIND_LOCALSEND = "localsend"  # 对方是官方 LocalSend（只能推/收，不能浏览）
KIND_UNKNOWN = "unknown"


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.peers: "dict[str, dict]" = {}      # fingerprint -> peer
        self.transfers: "dict[str, dict]" = {}  # transfer id -> info
        self.sessions: "dict[str, dict]" = {}   # 收到的上传会话（LocalSend 协议）
        self.pending: "dict[str, dict]" = {}    # 等待用户"接受/拒绝"的请求
        self._autosave = None
        self._stop = threading.Event()
        self.started = time.time()

    # ---------------------------------------------------------------- peers
    def upsert_peer(self, info: dict, ip: str, announce: bool = False) -> dict:
        """登记/更新一台设备。info 为对方设备信息，ip 为真实来源地址。

        注意"是不是自己"的判定：**指纹相同 + 来源地址是本机**才算自己。
        只看指纹会把"从别的电脑复制过去的 data 目录（指纹相同）"当成自己而丢弃，
        这正是两台 FileShare 互相发现不到的常见原因。
        """
        fp = (info.get("fingerprint") or "").strip()
        if not fp:
            return {}
        same_fingerprint = (fp == util.fingerprint())
        if same_fingerprint and ip in util.my_ip_set():
            return {}                                   # 真正的自己
        key = fp if not same_fingerprint else ("%s@%s" % (fp, ip))
        try:
            port = int(info.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        now = time.time()
        with self._lock:
            peer = self.peers.get(key) or {}
            peer.update({
                "fingerprint": key,
                "real_fingerprint": fp,
                "same_fingerprint": same_fingerprint,
                "alias": (info.get("alias") or peer.get("alias") or "未知设备").strip(),
                "device_model": info.get("deviceModel") or peer.get("device_model") or "",
                "device_type": info.get("deviceType") or peer.get("device_type") or "desktop",
                "version": info.get("version") or peer.get("version") or "",
                "protocol": info.get("protocol") or peer.get("protocol") or "http",
                "download": bool(info.get("download", peer.get("download", False))),
                "port": port or peer.get("port") or 0,
                "ip": ip or peer.get("ip") or "",
                "kind": peer.get("kind") or KIND_UNKNOWN,
                "last_seen": now,
                "first_seen": peer.get("first_seen") or now,
            })
            self.peers[key] = peer
            return dict(peer)

    def touch_peer(self, fingerprint_value: str) -> None:
        with self._lock:
            peer = self.peers.get(fingerprint_value)
            if peer:
                peer["last_seen"] = time.time()

    def set_kind(self, fingerprint_value: str, kind: str) -> None:
        with self._lock:
            peer = self.peers.get(fingerprint_value)
            if peer:
                peer["kind"] = kind

    def prune_peers(self, ttl: int) -> None:
        deadline = time.time() - max(10, ttl)
        with self._lock:
            for fp in [k for k, v in self.peers.items() if v.get("last_seen", 0) < deadline]:
                self.peers.pop(fp, None)

    def get_peer(self, fingerprint_value: str) -> dict:
        with self._lock:
            peer = self.peers.get(fingerprint_value)
            return dict(peer) if peer else {}

    def peer_by_addr(self, ip: str, port: int):
        with self._lock:
            for peer in self.peers.values():
                if peer.get("ip") == ip and int(peer.get("port") or 0) == int(port):
                    return dict(peer)
        return {}

    def list_peers(self) -> list:
        with self._lock:
            peers = [dict(p) for p in self.peers.values()]
        peers.sort(key=lambda p: p.get("last_seen", 0), reverse=True)
        return peers

    # ------------------------------------------------------------ transfers
    def add_transfer(self, **fields) -> dict:
        tid = util.random_id(12)
        item = {
            "id": tid,
            "name": fields.get("name") or "未命名",
            "size": int(fields.get("size") or 0),
            "done": int(fields.get("done") or 0),
            "direction": fields.get("direction") or "upload",
            "status": fields.get("status") or "active",
            "device": fields.get("device") or "",
            "peer_fp": fields.get("peer_fp") or "",
            "path": fields.get("path") or "",
            "error": "",
            "started": time.time(),
            "ended": 0.0,
            "sha256": fields.get("sha256") or "",
        }
        with self._lock:
            self.transfers[tid] = item
        return dict(item)

    def get_transfer(self, tid: str) -> dict:
        with self._lock:
            item = self.transfers.get(tid)
            return dict(item) if item else {}

    def update_transfer(self, tid: str, **fields) -> dict:
        with self._lock:
            item = self.transfers.get(tid)
            if not item:
                return {}
            item.update(fields)
            if item.get("status") in ("done", "error", "cancelled") and not item.get("ended"):
                item["ended"] = time.time()
            return dict(item)

    def list_transfers(self) -> list:
        with self._lock:
            items = [dict(t) for t in self.transfers.values()]
        items.sort(key=lambda t: t.get("started", 0), reverse=True)
        if len(items) > 300:                       # 只保留最近 300 条
            keep = {t["id"] for t in items[:300]}
            with self._lock:
                for tid in [tid for tid in self.transfers if tid not in keep]:
                    self.transfers.pop(tid, None)
        return items[:300]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for t in self.transfers.values() if t.get("status") == "active")

    def cancel_all_active(self) -> int:
        count = 0
        with self._lock:
            for item in self.transfers.values():
                if item.get("status") == "active":
                    item["status"] = "cancelled"
                    item["ended"] = time.time()
                    count += 1
        return count

    # ------------------------------------------------------------- sessions
    def create_session(self, peer_ip: str, files: dict, info: "dict | None" = None) -> dict:
        """为收到的推送请求建立会话，返回 {sessionId, files:{id: token}}。"""
        session_id = util.random_id(16)
        tokens = {}
        stored = {}
        for file_id, meta in files.items():
            token = util.random_id(12)
            tokens[file_id] = token
            stored[file_id] = {
                "id": file_id,
                "fileName": util.sanitize_filename(meta.get("fileName") or file_id),
                "size": int(meta.get("size") or 0),
                "fileType": meta.get("fileType") or "application/octet-stream",
                "sha256": meta.get("sha256") or "",
                "received": 0,
                "token": token,
            }
        with self._lock:
            self.sessions[session_id] = {
                "sessionId": session_id,
                "peer_ip": peer_ip,
                "info": info or {},
                "files": stored,
                "cancelled": False,
                "created": time.time(),
            }
        return {"sessionId": session_id, "files": tokens}

    def get_session(self, session_id: str) -> dict:
        with self._lock:
            session = self.sessions.get(session_id)
            return session if session else {}

    def mark_received(self, session_id: str, file_id: str, size: int) -> None:
        with self._lock:
            session = self.sessions.get(session_id)
            if session and file_id in session["files"]:
                session["files"][file_id]["received"] = int(size)

    def cancel_session(self, session_id: str) -> None:
        with self._lock:
            session = self.sessions.get(session_id)
            if session:
                session["cancelled"] = True

    # -------------------------------------------------------------- pending
    def add_pending(self, info: dict, files: dict, peer_ip: str, session_id: str) -> dict:
        item = {
            "sessionId": session_id,
            "info": info,
            "files": [util.sanitize_filename(f.get("fileName") or fid) for fid, f in files.items()],
            "total": sum(int(f.get("size") or 0) for f in files.values()),
            "peer_ip": peer_ip,
            "created": time.time(),
            "decision": None,
        }
        with self._lock:
            self.pending[session_id] = item
        return item

    def list_pending(self) -> list:
        with self._lock:
            items = [dict(p) for p in self.pending.values() if p.get("decision") is None]
        items.sort(key=lambda p: p.get("created", 0), reverse=True)
        return items

    def get_pending(self, session_id: str) -> dict:
        with self._lock:
            item = self.pending.get(session_id)
            return dict(item) if item else {}

    def decide_pending(self, session_id: str, decision: str) -> bool:
        with self._lock:
            item = self.pending.get(session_id)
            if not item or item.get("decision") is not None:
                return False
            item["decision"] = decision
            return True

    # ----------------------------------------------------------------- state
    def snapshot(self) -> dict:
        cfg = config.load()
        ips = util.local_ips()
        return {
            "app": "fileshare",
            "app_version": "0.1",
            "alias": cfg["alias"],
            "fingerprint": util.fingerprint(),
            "port": int(cfg["http_port"]),
            "ips": ips,
            "url": "http://%s:%d" % (ips[0] if ips else "127.0.0.1", int(cfg["http_port"])),
            "share_dir": cfg["share_dir"],
            "inbox_dir": cfg["inbox_dir"],
            "download_dir": cfg["download_dir"],
            "device_type": cfg["device_type"],
            "device_model": cfg["device_model"],
            "auto_accept": bool(cfg["auto_accept"]),
            "pin_set": bool(cfg["pin"]),
            "allow_delete": bool(cfg["allow_delete"]),
            "keep_copy_on_dragout": bool(cfg["keep_copy_on_dragout"]),
            "discovery_mode": cfg["discovery_mode"],
            "max_parallel": int(cfg["max_parallel"]),
            "peers": self.list_peers(),
            "transfers": self.list_transfers(),
            "pending": self.list_pending(),
            "uptime": int(time.time() - self.started),
        }

    # ------------------------------------------------------------ 持久化
    def save(self) -> None:
        try:
            data = {
                "peers": self.list_peers(),
                "transfers": self.list_transfers()[:100],
                "saved": time.time(),
            }
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False)
            os.replace(tmp, STATE_PATH)
        except (OSError, ValueError):
            pass

    def start_autosave(self, interval: float = 15.0) -> None:
        def loop() -> None:
            while not self._stop.wait(interval):
                self.save()

        self._autosave = threading.Thread(target=loop, name="state-autosave", daemon=True)
        self._autosave.start()

    def stop(self) -> None:
        self._stop.set()
        self.save()


store = Store()
