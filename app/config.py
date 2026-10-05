# -*- coding: utf-8 -*-
"""配置与路径管理。

配置保存在 data/config.json，采用"临时文件 + os.replace"原子写入，避免写坏。
"""
from __future__ import annotations

import json
import os
import socket
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# FILESHARE_HOME 可把运行期目录（data/shared/inbox/downloads）搬到别处，
# 便于同一台机器上跑多个实例做双机测试，或做成便携版。
ROOT_DIR = os.path.abspath(os.environ.get("FILESHARE_HOME") or os.path.dirname(APP_DIR))
WEB_DIR = os.path.join(APP_DIR, "web")
DATA_DIR = os.path.join(ROOT_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
FINGERPRINT_PATH = os.path.join(DATA_DIR, "fingerprint.txt")
TOKEN_PATH = os.path.join(DATA_DIR, "token.txt")


def default_alias() -> str:
    """默认设备别名：本机计算机名。"""
    try:
        name = socket.gethostname()
    except OSError:
        name = ""
    return (name or "PC").strip() or "PC"


DEFAULT_CONFIG = {
    "alias": default_alias(),
    "http_port": 8099,
    "bind_address": "0.0.0.0",
    "device_type": "desktop",          # LocalSend 枚举：desktop / mobile / web / headless / server
    "device_model": "Windows",
    "share_dir": os.path.join(ROOT_DIR, "shared"),
    "inbox_dir": os.path.join(ROOT_DIR, "inbox"),
    "download_dir": os.path.join(ROOT_DIR, "downloads"),
    "auto_accept": False,              # 收到别人推送时是否免确认
    "pin": "",                         # 非空则该 PIN 用于 LocalSend 兼容校验
    "discovery_mode": "multicast",     # multicast | scan | off
    "announce_interval": 30,           # 组播重播间隔（秒）
    "peer_ttl": 90,                    # 设备无响应多久后下线（秒）
    "max_parallel": 3,                 # 单机最大并行上传数
    "allow_delete": True,              # 是否允许通过 API 删除共享目录内的文件
    "keep_copy_on_dragout": True,      # 拖出下载时在本机 downloads 目录留一份
    "accept_timeout": 60,              # 等待本机用户"接受/拒绝"的秒数
    "scan_on_start": True,             # 启动 5 秒后若没发现设备，自动扫网段
    "extra_hosts": [],                 # 额外允许的 Host 名（例如通过计算机名/mDNS 访问时）
}

_lock = threading.RLock()
_cache: "dict | None" = None


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


def load() -> dict:
    """读取配置（带缓存）。缺失字段用默认值补齐并回写。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        cfg = dict(DEFAULT_CONFIG)
        changed = not os.path.exists(CONFIG_PATH)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                    raw = json.load(fp)
                if isinstance(raw, dict):
                    for key, value in raw.items():
                        if key in DEFAULT_CONFIG:
                            cfg[key] = value
                        else:
                            changed = True  # 丢弃未知字段
            except (OSError, ValueError):
                changed = True
        _cache = cfg
        if changed:
            save(cfg)
        return _cache


def save(cfg: "dict | None" = None) -> dict:
    """原子写回配置。"""
    global _cache
    with _lock:
        if cfg is None:
            cfg = _cache or dict(DEFAULT_CONFIG)
        _cache = cfg
        _atomic_write(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
        return cfg


def get(key: str, default=None):
    return load().get(key, DEFAULT_CONFIG.get(key, default))


def update(values: dict) -> dict:
    """局部更新配置并落盘，返回完整配置。"""
    with _lock:
        cfg = dict(load())
        for key, value in values.items():
            if key in DEFAULT_CONFIG:
                cfg[key] = value
        return save(cfg)


def apply_runtime(values: dict) -> dict:
    """只在内存里生效，**不写入 config.json**。

    专供命令行参数使用：`--port 9000`、`--no-discovery` 这类临时覆盖不应该被永久记住，
    否则用户在调试时加一次参数，之后每次都按那个参数跑，很难排查。
    想永久修改请用界面「设置」或直接改 data/config.json。
    """
    global _cache
    with _lock:
        cfg = dict(load())
        for key, value in values.items():
            if key in DEFAULT_CONFIG:
                cfg[key] = value
        _cache = cfg
        return cfg


def set_port(port: int) -> dict:
    return update({"http_port": int(port)})


def ensure_dirs() -> None:
    """确保所有运行期目录存在。"""
    cfg = load()
    for key in ("share_dir", "inbox_dir", "download_dir"):
        path = cfg.get(key)
        if path:
            os.makedirs(path, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
