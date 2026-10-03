"""通用工具：时间、ID、子进程（跨平台、双编码、禁 shell）、目标校验。"""
from __future__ import annotations

import hashlib
import re
import secrets
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"


def now() -> int:
    """epoch 秒。"""
    import time
    return int(time.time())


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def decode_output(raw: bytes) -> str:
    """Windows 控制台工具输出 GBK，Linux 为 UTF-8；双尝试。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def run_cmd(args: list[str], timeout: float) -> tuple[int, str, str]:
    """执行外部命令：数组参数（禁 shell），超时 kill，捕获 stdout/stderr。"""
    try:
        p = subprocess.run(
            args, capture_output=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        raise ToolMissing(f"工具不存在: {args[0]}")
    except subprocess.TimeoutExpired as e:
        raise ToolTimeout(f"命令超时({timeout}s): {' '.join(args[:3])}...")
    return p.returncode, decode_output(p.stdout or b""), decode_output(p.stderr or b"")


class ToolMissing(Exception):
    pass


class ToolTimeout(Exception):
    pass


# 目标校验：域名（字母数字-点）或 IPv4/IPv6，拒绝 shell 元字符
_DOMAIN_RE = re.compile(r"^(?=.{1,253}\Z)([a-zA-Z0-9_]([a-zA-Z0-9_-]{0,61}[a-zA-Z0-9_])?\.)+[a-zA-Z]{2,63}$")
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_BAD_CHARS = set(";|&`$><\\'\"\n\r")


def validate_target(target: str) -> bool:
    if not target or any(c in _BAD_CHARS for c in target):
        return False
    if _DOMAIN_RE.match(target):
        return True
    m = _IPV4_RE.match(target)
    if m and all(0 <= int(g) <= 255 for g in m.groups()):
        return True
    if ":" not in target:
        return False
    if target.startswith("[") and target.endswith("]"):   # [v6 字面量] 括号形式
        target = target[1:-1]
    return re.match(r"^[0-9a-fA-F:.]{2,45}$", target) is not None


def validate_host_port(target: str) -> bool:
    """TCP 任务目标：host:port / [v6]:port / 裸 host（端口走 params.port）。

    host 部分复用 validate_target（域名 / IPv4 / IPv6 含括号形式）；port 1~65535。
    """
    if not target or any(c in _BAD_CHARS for c in target):
        return False
    host, port = target, ""
    if target.startswith("["):                     # [v6]:port
        host, sep, rest = target.partition("]")
        host = host[1:]
        if sep:
            port = rest.lstrip(":")
    elif target.count(":") == 1:                   # host:port（IPv6 裸串多冒号不在此列）
        host, _, port = target.partition(":")
    else:
        host = target                              # 裸 host / 裸 v6（端口由 params.port 提供）
    if not host or not validate_target(host):
        return False
    if not port:
        return True                                # 允许裸 host，端口在 params.port
    return port.isdigit() and 0 < int(port) <= 65535


def validate_domain(domain: str) -> bool:
    """DNS 任务目标：宽松域名/IP 校验（字母数字点连字符，长度限制；也放行 IP）。"""
    if not domain or len(domain) > 253 or any(c in _BAD_CHARS for c in domain):
        return False
    if validate_target(domain):
        return True
    # 宽松域名：本地/内网自定义域（单标签、多级子域、下划线），只挡注入与空白
    return re.match(r"^[A-Za-z0-9_]([A-Za-z0-9_.-]{0,251}[A-Za-z0-9_])?\.?$", domain) is not None


def validate_dns_spec(spec: str) -> bool:
    """DNS 线路写法：裸 IP/域名（auto：UDP→TCP→DoH）或一等线路前缀。

    doh:<URL|host> / dot:<ip>[:port] / udp:<ip>[:port] / tcp:<ip>[:port] / <ip>@<port>
    严格解析在 common/dnsres.py:parse_spec()；此处只挡注入与明显非法写法。
    """
    if not spec or len(spec) > 200 or any(c in spec for c in " \t\n\r\"'`\\;|$<>"):
        return False
    low = spec.lower()
    if low.startswith("doh:"):
        # DoH URL 里合法地用得到 & ? = {}（如 ?name={host}&type=1），且走 urllib 不过 shell
        rest = spec[4:]
        return validate_url(rest) or re.match(r"^[A-Za-z0-9_.:{}\[\]/@?=&%+~-]+$", rest) is not None
    if low.startswith(("dot:", "udp:", "tcp:")):
        rest = spec.split(":", 1)[1]
        return bool(rest) and re.match(r"^[A-Za-z0-9_.:\[\]@-]+$", rest) is not None
    if low.startswith(("http://", "https://")):
        return re.match(r"^[A-Za-z0-9_.:{}\[\]/@?=&%+~-]+$", spec) is not None
    if "@" in spec:                      # <ip>@<port> → DoT
        host, _, port = spec.partition("@")
        return validate_target(host) and port.isdigit() and 0 < int(port) <= 65535
    return validate_target(spec)


def validate_url(url: str) -> bool:
    if any(c in _BAD_CHARS for c in url):
        return False
    return re.match(r"^https?://[^\s/'\"]+(/\S*)?$", url) is not None


def is_ip(target: str) -> bool:
    return _IPV4_RE.match(target) is not None or (":" in target and validate_target(target))
