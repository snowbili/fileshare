# -*- coding: utf-8 -*-
"""组播抓包诊断：看看局域网里到底有哪些设备在广播、指纹是否撞车。

用法：
    py -3.14 tests\\sniff_discovery.py [监听秒数，默认 20]

它会：
  1) 绑定 UDP 53317 并加入组播组 224.0.0.167
  2) 先广播一次本机 announce（顺便让别的设备把我们也记上）
  3) 打印收到的每个包（来源 IP、别名、port、protocol、指纹前 8 位）
  4) 明确指出"指纹与本机相同"的包 —— 这就是两台 FileShare 互相看不见的原因
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))

import util  # noqa: E402
# ?????? Windows / ?????????????? GBK ????????
# ?? print ?? UnicodeEncodeError????????????????
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


GROUP = "224.0.0.167"
PORT = 53317

def main() -> int:
    seconds = 20
    if len(sys.argv) > 1:
        try:
            seconds = max(3, int(sys.argv[1]))
        except ValueError:
            pass

    my_fp = util.fingerprint()
    my_ips = util.local_ips()
    print("本机别名    : %s" % util.device_info()["alias"])
    print("本机指纹    : %s" % my_fp)
    print("本机地址    : %s" % my_ips)
    print("监听        : %s:%d，共 %d 秒（期间请让对方设备也启动/重播）" % (GROUP, PORT, seconds))
    print("-" * 70)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", PORT))
    except OSError as exc:
        print("绑定 UDP %d 失败：%s（可能有其它程序独占该端口）" % (PORT, exc))
        return 2
    try:
        mreq = struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as exc:
        print("加入组播组失败：%s" % exc)
        return 2
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.settimeout(1.0)

    payload = json.dumps(util.announce_payload(), ensure_ascii=False).encode("utf-8")
    try:
        sock.sendto(payload, (GROUP, PORT))
        print("已发出本机 announce（%d 字节）" % len(payload))
    except OSError as exc:
        print("发送 announce 失败：%s" % exc)

    seen = []
    local_clash = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            info = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            print("[%s] 收到非 JSON 包 %d 字节" % (addr[0], len(data)))
            continue
        if not isinstance(info, dict):
            continue
        fp = (info.get("fingerprint") or "").strip()
        same = fp == my_fp
        is_local = addr[0] in util.my_ip_set()
        if same and is_local:
            continue                                    # 自己发的回环，忽略
        tag = "⚠指纹与本机相同" if same else ("自己(回环)" if is_local else "其它设备")
        print("[%s] %-16s alias=%-18s port=%-6s proto=%-6s fp=%s  %s"
              % (time.strftime("%H:%M:%S"), addr[0], str(info.get("alias"))[:18],
                 info.get("port"), info.get("protocol"), fp[:8] or "(空)", tag))
        seen.append(info)
        if same and not is_local:
            local_clash.append(addr[0])
    sock.close()

    print("-" * 70)
    print("共收到 %d 个有效 announce" % len(seen))
    if local_clash:
        print("")
        print("发现问题：以下设备与本机指纹完全相同 -> %s" % ", ".join(sorted(set(local_clash))))
        print("  原因：对方机器上的 data 目录是从本机整体复制过去的（fingerprint.txt 一起被拷走）。")
        print("  处理：在**对方机器**上删除 data\\fingerprint.txt 与 data\\machine.txt，然后重启 FileShare；")
        print("        本工具已支持这种场景（会显示 ⚠ 并仍能互传），删掉后即为干净状态。")
        return 1
    if not seen:
        print("没有收到任何 announce。可能原因：")
        print("  1) 对方设备没在运行；2) 路由器开了 AP/客户端隔离；")
        print("  3) 本机 53317 被其它程序独占（装官方 LocalSend 时可能出现）；")
        print("  4) 防火墙拦了 UDP 53317（管理员运行 firewall.cmd）。")
        return 3
    print("未发现指纹撞车。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
