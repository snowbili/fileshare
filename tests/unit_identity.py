# -*- coding: utf-8 -*-
"""指纹/自身判定逻辑的单元校验（不启服务，直接调函数）。

覆盖这个坑：两台 FileShare 互相看不见，因为 data 目录被整体复制导致指纹相同，
旧代码把对方当成"自己"丢弃。

用法： python tests/unit_identity.py
"""
from __future__ import annotations

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOME = os.path.join(HERE, "tmp_unit")

# 用独立数据目录，避免污染真实 data/
os.environ["FILESHARE_HOME"] = HOME
shutil.rmtree(HOME, ignore_errors=True)
os.makedirs(HOME, exist_ok=True)
sys.path.insert(0, os.path.join(ROOT, "app"))

import store  # noqa: E402
import util  # noqa: E402
# ?????? Windows / ?????????????? GBK ????????
# ?? print ?? UnicodeEncodeError????????????????
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


results = []

def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("  %s  %-56s %s" % ("PASS" if ok else "FAIL", name, detail))

def main() -> int:
    my_fp = util.fingerprint()
    my_ips = util.local_ips()
    print("本机指纹：%s" % my_fp)
    print("本机地址：%s" % my_ips)
    print("")

    # 1) 指纹文件与 machine.txt 都已生成
    check("fingerprint.txt 已生成", os.path.isfile(os.path.join(HOME, "data", "fingerprint.txt")))
    check("machine.txt 已生成", os.path.isfile(os.path.join(HOME, "data", "machine.txt")))

    # 2) 同一机器重复调用指纹稳定
    check("同机指纹稳定不变", util.fingerprint() == my_fp)

    # 3) 模拟"data 目录被复制到另一台电脑"：machine.txt 换成别的机器 id
    with open(os.path.join(HOME, "data", "machine.txt"), "w", encoding="utf-8") as fp:
        fp.write("deadbeef" * 4)
    new_fp = util.fingerprint()
    check("machine.txt 不匹配时自动换新指纹（复制目录场景）", new_fp != my_fp,
          "%s -> %s" % (my_fp[:8], new_fp[:8]))
    check("换指纹后 machine.txt 被更新为当前机器",
          open(os.path.join(HOME, "data", "machine.txt"), encoding="utf-8").read().strip() == util.machine_id())
    my_fp = new_fp

    # 4) 自身判定：指纹相同 + 本机地址 = 自己（丢弃）
    info = {"alias": "me", "fingerprint": my_fp, "port": 8099, "version": "2.0", "protocol": "http"}
    for ip in (my_ips + ["127.0.0.1"]):
        got = store.store.upsert_peer(dict(info), ip)
        check("指纹相同 + 本机地址(%s) 判定为自己，不入列表" % ip, got == {})

    # 5) 关键修复：指纹相同 + 外部地址（另一台电脑复制的 data）= 对方设备
    got = store.store.upsert_peer(dict(info), "192.168.99.99")
    check("指纹相同 + 外部 IP 会被登记为设备（修复点）", bool(got) and got["ip"] == "192.168.99.99",
          "kind=%s" % got.get("kind"))
    check("该设备带 same_fingerprint 标记，便于界面提示", got.get("same_fingerprint") is True)
    check("内部 key 用 IP 区分，避免与其它设备互相顶掉",
          got["fingerprint"] == my_fp + "@192.168.99.99", got["fingerprint"][-20:])
    check("real_fingerprint 保留原始指纹", got.get("real_fingerprint") == my_fp)

    # 6) 另一台设备指纹不同 → 正常登记，且不误标
    other = {"alias": "other", "fingerprint": "a" * 32, "port": 8099, "version": "2.0"}
    got2 = store.store.upsert_peer(dict(other), "192.168.99.100")
    check("不同指纹设备正常登记", bool(got2) and got2["fingerprint"] == "a" * 32)
    check("不同指纹设备不带 same_fingerprint 标记", got2.get("same_fingerprint") is False)
    check("设备总数 = 2", len(store.store.list_peers()) == 2,
          "aliases=%s" % [p["alias"] for p in store.store.list_peers()])

    # 7) 重复 announce 不会重复登记
    store.store.upsert_peer(dict(info), "192.168.99.99")
    check("同一设备重复上报不会产生重复条目", len(store.store.list_peers()) == 2)

    # 8) 本机地址集合含回环
    check("my_ip_set 含 127.0.0.1", "127.0.0.1" in util.my_ip_set())

    failed = [name for name, ok in results if not ok]
    print("")
    print("单元校验：%d/%d 通过" % (len(results) - len(failed), len(results)))
    shutil.rmtree(HOME, ignore_errors=True)
    return 0 if not failed else 1

if __name__ == "__main__":
    raise SystemExit(main())
