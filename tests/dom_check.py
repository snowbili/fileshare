# -*- coding: utf-8 -*-
"""前端渲染校验：把 Edge 无头模式 dump 出来的 DOM 做断言。

用法：
  1) 启动服务：  py -3.14 app\\server.py --no-window --no-discovery
  2) dump DOM：  msedge --headless=new --disable-gpu --virtual-time-budget=6000 --dump-dom http://127.0.0.1:8099/ > tests\\dom.html
  3) 校验：      py -3.14 tests\\dom_check.py
"""
from __future__ import annotations

import io
import os
import sys
# ?????? Windows / ?????????????? GBK ????????
# ?? print ?? UnicodeEncodeError????????????????
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


HERE = os.path.dirname(os.path.abspath(__file__))
DOM = os.path.join(HERE, "dom.html")

if not os.path.isfile(DOM):
    print("缺少 %s，请先用 Edge 无头模式 dump DOM（见文件头注释）" % DOM)
    raise SystemExit(2)

html = io.open(DOM, encoding="utf-8", errors="replace").read()

checks = [
    ("页面已下载完整", len(html) > 5000),
    ("token 已注入", 'data-token="' in html and "__FS_TOKEN__" not in html),
    ("我的共享目录面板存在", "我的共享目录" in html),
    ("对方共享目录面板存在", "对方共享目录" in html),
    ("左侧已渲染出共享文件名", 'data-name="使用说明.txt"' in html),
    ("myInfo 已被 JS 填充（不再是占位文案）", "正在读取本机信息" not in html),
    ("设备下拉已由 JS 重绘", "未发现设备" in html or "可浏览" in html or "仅推送" in html),
    ("右下推送区文案已由 JS 设置", "推送给" in html or "把左边文件拖到这里" in html),
    ("传输队列已渲染", "还没有传输记录" in html or "进行中" in html),
    ("没有出现错误提示条", 'class="toast err' not in html and "服务连接失败" not in html),
]

failed = 0
for name, ok in checks:
    print("  %s  %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        failed += 1

print("")
print("前端渲染校验：%d/%d 通过" % (len(checks) - failed, len(checks)))
raise SystemExit(0 if failed == 0 else 1)
