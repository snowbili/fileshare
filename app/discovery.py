# -*- coding: utf-8 -*-
"""设备发现（LocalSend 协议 3.1 / 3.2）。

* UDP 组播：224.0.0.167:53317，启动即广播 announce，按 announce_interval 重播
* 收到 announce=true 的消息 → 用 HTTP POST /api/localsend/v2/register 回礼（双向发现）
* HTTP 失败则退回 UDP 组播回包（announce=false）
* 组播不可用时由 peer.scan_subnet() 兜底（向本网段逐个 IP 发 register）
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time

import config
import store
import util

MULTICAST_GROUP = "224.0.0.167"
MULTICAST_PORT = 53317

_stop = threading.Event()
_thread = None
_sock: "socket.socket | None" = None
_state = {
    "multicast": False,      # 组播套接字是否可用
    "last_announce": 0.0,
    "last_error": "",
    "sent": 0,
    "received": 0,
}


def status() -> dict:
    info = dict(_state)
    info["group"] = "%s:%d" % (MULTICAST_GROUP, MULTICAST_PORT)
    info["interval"] = int(config.get("announce_interval", 30))
    return info


def _open_socket():
    global _sock
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", MULTICAST_PORT))
    except OSError as exc:
        _state["last_error"] = "绑定 UDP %d 失败: %s" % (MULTICAST_PORT, exc)
        sock.close()
        return None
    try:
        mreq = struct.pack("4s4s", socket.inet_aton(MULTICAST_GROUP), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as exc:
        _state["last_error"] = "加入组播组失败: %s" % exc
        sock.close()
        return None
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    except OSError:
        pass
    sock.settimeout(1.0)
    _sock = sock
    return sock


def announce() -> bool:
    """向组播组广播本机设备信息。"""
    if _sock is None:
        return False
    payload = json.dumps(util.announce_payload(), ensure_ascii=False).encode("utf-8")
    try:
        _sock.sendto(payload, (MULTICAST_GROUP, MULTICAST_PORT))
        _state["last_announce"] = time.time()
        _state["sent"] += 1
        return True
    except OSError as exc:
        _state["last_error"] = "组播发送失败: %s" % exc
        return False


def _handle_message(data: bytes, addr) -> None:
    try:
        info = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return
    if not isinstance(info, dict):
        return
    fingerprint = (info.get("fingerprint") or "").strip()
    if not fingerprint:
        return
    # 只有"指纹相同 + 来源是本机地址"才是自己发的组播回环；
    # 指纹相同但来自别的电脑，说明对方的 data 目录是从本机复制过去的，必须当成对方处理。
    if fingerprint == util.fingerprint() and addr[0] in util.my_ip_set():
        return
    _state["received"] += 1
    ip = addr[0]
    peer = store.store.upsert_peer(info, ip)
    if info.get("announce") and peer:                 # 只有 announce=true 才需要回礼
        threading.Thread(target=_reply, args=(info, ip), daemon=True).start()


def _reply(info: dict, ip: str) -> None:
    """HTTP 回礼；失败退回 UDP 回包。"""
    import peer

    port = int(info.get("port") or 0)
    if port and peer.register_remote(ip, port):
        return
    if _sock is None:
        return
    payload = util.device_info()
    payload["announce"] = False
    try:
        _sock.sendto(json.dumps(payload, ensure_ascii=False).encode("utf-8"), (ip, MULTICAST_PORT))
    except OSError:
        pass


def _loop() -> None:
    while not _stop.is_set():
        interval = max(10, int(config.get("announce_interval", 30)))
        announce()
        deadline = time.time() + interval
        while not _stop.is_set() and time.time() < deadline:
            try:
                data, addr = _sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                _handle_message(data, addr)
            except Exception as exc:                 # 单条消息出错不影响整体
                _state["last_error"] = "处理组播消息失败: %r" % exc
        store.store.prune_peers(int(config.get("peer_ttl", 90)))


def start() -> bool:
    global _thread
    if _thread is not None and _thread.is_alive():
        return True
    sock = _open_socket()
    if sock is None:
        _state["multicast"] = False
        return False
    _state["multicast"] = True
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="discovery", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()
    if _sock is not None:
        try:
            _sock.close()
        except OSError:
            pass
