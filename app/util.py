# -*- coding: utf-8 -*-
"""通用工具：指纹、设备信息、本机 IP、文件名清洗、长路径、安全路径拼接、MIME、哈希。

安全要点（对应硬坑 #4/#5/#6）：
* sanitize_filename  过滤 Windows 非法字符、结尾空格/点、保留设备名、限制长度
* safe_join          拒绝 .. / 绝对路径 / 盘符 / UNC，并用 realpath 拦 symlink、junction
* long_path          超长路径加 \\\\?\\ 前缀
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import secrets
import socket
import time
import uuid
from email.utils import formatdate
from urllib.parse import quote

import config

MAX_NAME_LEN = 200
MACHINE_PATH = os.path.join(config.DATA_DIR, "machine.txt")
_ips_cache = {"time": 0.0, "value": []}
_INVALID_CHARS = set('<>:"/\\|?*')
_RESERVED = {"CON", "PRN", "AUX", "NUL"}
_RESERVED |= {"COM%d" % i for i in range(1, 10)}
_RESERVED |= {"LPT%d" % i for i in range(1, 10)}
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


# --------------------------------------------------------------------------- #
# 指纹 / 设备信息
# --------------------------------------------------------------------------- #
def fingerprint() -> str:
    """持久化的随机指纹（HTTP 模式下 LocalSend 用它区分设备）。"""
    path = config.FINGERPRINT_PATH
    try:
        with open(path, "r", encoding="utf-8") as fp:
            value = fp.read().strip()
        if value:
            return value
    except OSError:
        pass
    value = secrets.token_hex(16)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(value)
    except OSError:
        pass
    return value


def device_info() -> dict:
    """本机 announce / register 都使用这份设备信息。"""
    cfg = config.load()
    return {
        "alias": cfg["alias"],
        "version": "2.0",              # 协议版本 major.minor，与 LocalSend v2 对齐
        "deviceModel": cfg["device_model"],
        "deviceType": cfg["device_type"],
        "fingerprint": fingerprint(),
        "port": int(cfg["http_port"]),
        "protocol": "http",
        "download": True,              # 我们实现了反向下载 API
    }


def announce_payload() -> dict:
    info = device_info()
    info["announce"] = True
    return info


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #
def local_ips(force: bool = False) -> list:
    """本机所有可用的 IPv4（排除 127.*）。带 10 秒缓存，避免每个请求都查一遍。"""
    now = time.time()
    if not force and _ips_cache["value"] and now - _ips_cache["time"] < 10:
        return list(_ips_cache["value"])

    found: list = []

    def add(ip: str) -> None:
        if ip and ip not in found and not ip.startswith("127.") and ip != "0.0.0.0":
            found.append(ip)

    for probe in ("8.8.8.8", "1.1.1.1", "223.5.5.5"):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.2)
            sock.connect((probe, 80))
            add(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            if sock is not None:
                sock.close()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(item[4][0])
    except OSError:
        pass
    _ips_cache["time"] = now
    _ips_cache["value"] = found
    return list(found)


def my_ip_set() -> set:
    """本机地址集合（含 127.*），用于判断"某个包是不是自己发的"。"""
    ips = {ip.lower() for ip in local_ips()}
    ips.update({"127.0.0.1", "localhost", "::1"})
    return ips


def primary_ip() -> str:
    ips = local_ips()
    return ips[0] if ips else "127.0.0.1"


def machine_id() -> str:
    """本机标识（主机名 + 网卡 MAC 的哈希）。

    用来识别 data 目录是不是被整体复制到别的电脑上了——这正是"两台 FileShare
    互相看不见"的常见原因：复制过去的 fingerprint.txt 与本机相同，双方都把对方
    当成"自己"而丢弃。
    """
    try:
        raw = "%s|%s" % (socket.gethostname().lower(), uuid.getnode())
    except OSError:
        raw = socket.gethostname().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def fingerprint() -> str:
    """设备指纹（持久化随机串）。

    若发现 data 目录来自另一台电脑（machine.txt 不匹配或缺失），会自动换一个新指纹，
    避免"把自己当成别人"。"""
    stored = ""
    try:
        with open(config.FINGERPRINT_PATH, "r", encoding="utf-8") as fp:
            stored = fp.read().strip()
    except OSError:
        stored = ""
    try:
        with open(MACHINE_PATH, "r", encoding="utf-8") as fp:
            stored_machine = fp.read().strip()
    except OSError:
        stored_machine = ""

    current = machine_id()
    regenerate = (not stored) or (stored_machine != current)
    value = secrets.token_hex(16) if regenerate else stored
    if regenerate:
        os.makedirs(os.path.dirname(config.FINGERPRINT_PATH), exist_ok=True)
        try:
            with open(config.FINGERPRINT_PATH, "w", encoding="utf-8") as fp:
                fp.write(value)
        except OSError:
            pass
    if stored_machine != current:
        try:
            os.makedirs(os.path.dirname(MACHINE_PATH), exist_ok=True)
            with open(MACHINE_PATH, "w", encoding="utf-8") as fp:
                fp.write(current)
        except OSError:
            pass
    return value


def subnet_hosts(ip: str, prefix: int = 24) -> list:
    """给定本机 IP，枚举同网段候选主机（默认 /24）。"""
    try:
        octets = [int(part) for part in ip.split(".")]
        if len(octets) != 4:
            return []
    except ValueError:
        return []
    if prefix > 24:                       # 只在 /24 及更大范围内扫描，避免超大网段
        prefix = 24
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    addr = ((octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]) & 0xFFFFFFFF
    net = addr & mask
    bcast = net | (~mask & 0xFFFFFFFF)
    hosts = []
    cur = net + 1
    while cur < bcast and len(hosts) < 1024:
        hosts.append("%d.%d.%d.%d" % ((cur >> 24) & 255, (cur >> 16) & 255, (cur >> 8) & 255, cur & 255))
        cur += 1
    return hosts



# --------------------------------------------------------------------------- #
# 文件名 / 路径
# --------------------------------------------------------------------------- #
def sanitize_filename(name: str) -> str:
    """把任意字符串变成 Windows 上安全的文件名（不含目录部分）。"""
    name = (name or "").replace("\\", "/").split("/")[-1]
    chars = []
    for ch in name:
        if ch in _INVALID_CHARS or ord(ch) < 32 or ord(ch) == 0x7F:
            chars.append("_")
        else:
            chars.append(ch)
    result = "".join(chars).strip().strip(".").rstrip(" .")
    if not result:
        result = "unnamed"
    stem, ext = os.path.splitext(result)
    if stem.upper() in _RESERVED or result.upper() in _RESERVED:
        result = "_" + result
        stem, ext = os.path.splitext(result)
    if len(result) > MAX_NAME_LEN:
        ext = ext[:20]
        stem = stem[: MAX_NAME_LEN - len(ext)]
        result = stem + ext
    return result or "unnamed"


def unique_path(directory: str, name: str) -> str:
    """同名文件自动改名：name.ext -> name (1).ext。"""
    candidate = os.path.join(directory, name)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(name)
    for index in range(1, 10000):
        candidate = os.path.join(directory, "%s (%d)%s" % (stem, index, ext))
        if not os.path.exists(candidate):
            return candidate
    raise OSError("同名文件过多，无法生成新文件名")


def long_path(path: str) -> str:
    """Windows 上超过 240 字符时加 \\\\?\\ 前缀，绕过 MAX_PATH 限制（硬坑 #5）。"""
    if os.name != "nt":
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    if len(path) >= 240:
        return "\\\\?\\" + path
    return path


def _check_part(part: str) -> None:
    if not part or part in (".", ".."):
        raise ValueError("非法路径片段")
    if any(ch in _INVALID_CHARS for ch in part):
        raise ValueError("路径片段含非法字符: %s" % part)
    if part != part.rstrip(" ."):
        raise ValueError("路径片段以空格或点结尾: %s" % part)


def safe_join(root: str, rel: str, must_exist: bool = False) -> str:
    """把相对路径安全地拼到 root 下（硬坑 #6）。

    拒绝：绝对路径、盘符、UNC、..、含非法字符的片段、realpath 逃逸（symlink/junction）。
    """
    rel = (rel or "").replace("\\", "/").strip()
    if rel.startswith("/") or rel.startswith("//") or _DRIVE_RE.match(rel):
        raise ValueError("不允许绝对路径")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    for part in parts:
        _check_part(part)
    root_real = os.path.realpath(root)
    target = os.path.join(root_real, *parts) if parts else root_real
    real = os.path.realpath(target)
    if real != root_real and not real.startswith(root_real + os.sep):
        raise ValueError("路径越界")
    if must_exist and not os.path.exists(real):
        raise FileNotFoundError("路径不存在")
    return real


def rel_of(root: str, path: str) -> str:
    """取相对 root 的路径，统一用 / 分隔。"""
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    return "" if rel == "." else rel.replace("\\", "/")


# --------------------------------------------------------------------------- #
# MIME / 响应头
# --------------------------------------------------------------------------- #
def guess_mime(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


def content_disposition(name: str, inline: bool = False) -> str:
    """RFC 5987 编码，兼容中文/emoji 文件名（硬坑 #2）。"""
    fallback = name.encode("ascii", "ignore").decode("ascii").replace('"', "_").strip()
    fallback = fallback or "file"
    kind = "inline" if inline else "attachment"
    return '%s; filename="%s"; filename*=UTF-8\'\'%s' % (kind, fallback, quote(name, safe=""))


def http_date(timestamp: "float | None" = None) -> str:
    return formatdate(timestamp or time.time(), usegmt=True)


def parse_range(value: str, size: int):
    """解析单段 Range，返回 (start, end) 闭区间；无法解析返回 None。"""
    if not value or not value.startswith("bytes="):
        return None
    spec = value[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    left, _, right = spec.partition("-")
    try:
        if left == "":                      # bytes=-500 取末尾 500 字节
            length = int(right)
            if length <= 0:
                return None
            start = max(0, size - length)
            return (start, size - 1)
        start = int(left)
        end = int(right) if right else size - 1
    except ValueError:
        return None
    if start > end or start >= size:
        return None
    return (start, min(end, size - 1))


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #
def sha256_file(path: str, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(long_path(path), "rb") as fp:
        while True:
            block = fp.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return ("%d %s" % (int(value), unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%.1f TB" % value


def random_id(length: int = 16) -> str:
    return secrets.token_hex(max(1, length // 2))


def now() -> float:
    return time.time()
