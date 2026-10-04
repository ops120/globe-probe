"""通用工具：时间、ID、子进程（跨平台、双编码、禁 shell）、目标校验。"""
from __future__ import annotations

import hashlib
import ipaddress
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
    except subprocess.TimeoutExpired:
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

# 服务端外呼目标的默认禁区：未指定地址 / 链路本地（含 AWS/GCP/Azure/阿里云的
# 169.254.169.254 元数据地址）/ IPv6 链路本地。探测内网（RFC1918、环回）是拨测平台
# 的本职，不在此列；但云元数据地址没有任何合法拨测语义，只有一种用途——借服务端
# 外呼窃取宿主机云凭据（经典 SSRF 跳板）。域名不在此解析（DNS rebinding 是残余风险）。
_BLOCKED_NETS = tuple(ipaddress.ip_network(n) for n in
                      ("0.0.0.0/8", "169.254.0.0/16", "fe80::/10"))


def _ip_blocked(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return any(ip in net for net in _BLOCKED_NETS)


def _url_ip_blocked(url: str) -> bool:
    """URL 的 host 是 IP 字面量且落在禁区 → True。userinfo/端口/括号 v6 都要剥掉。"""
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)", url)
    if not m:
        return False
    auth = m.group(1)
    if auth.startswith("["):                       # [v6]:port
        host = auth[1:auth.find("]")] if "]" in auth else auth[1:]
    elif auth.count(":") > 1:                      # 裸 v6（无端口写法）
        host = auth
    else:
        host = auth.rsplit(":", 1)[0]              # host[:port]
    host = host.rsplit("@", 1)[-1]                 # 带 userinfo 时取 @ 后的主机
    return _ip_blocked(host)


def listen_is_loopback(listen: str) -> bool:
    """listen 形如 host:port / [v6]:port / 裸 v6，监听在环回地址时为 True。

    用于写口鉴权的「本机开发模式」判定：未配置 admin_token 时只有环回监听
    才允许无鉴权写（见 api_web.check_write）。"""
    s = (listen or "").strip()
    if s.startswith("["):
        host = s[1:s.find("]")] if "]" in s else s[1:]
    elif s.count(":") > 1:
        host = s                                   # 裸 v6（无端口）
    else:
        host = s.rsplit(":", 1)[0]
    host = host.strip()
    if not host or host in ("*", "0.0.0.0", "::"):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_target(target: str) -> bool:
    if not target or any(c in _BAD_CHARS for c in target):
        return False
    if _DOMAIN_RE.match(target):
        return True
    m = _IPV4_RE.match(target)
    if m and all(0 <= int(g) <= 255 for g in m.groups()):
        return not _ip_blocked(target)
    if ":" not in target:
        return False
    if target.startswith("[") and target.endswith("]"):   # [v6 字面量] 括号形式
        target = target[1:-1]
    if re.match(r"^[0-9a-fA-F:.]{2,45}$", target) is None:
        return False
    # v6 形状合法但不一定是可解析字面量（保持旧行为交给系统工具判定）；可解析时套用禁区
    try:
        ipaddress.ip_address(target)
    except ValueError:
        return True
    return not _ip_blocked(target)


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
    if re.match(r"^https?://[^\s/'\"]+(/\S*)?$", url) is None:
        return False
    # 禁区 IP 字面量（云元数据/链路本地/未指定地址）不作为服务端外呼 URL
    return not _url_ip_blocked(url)


def is_ip(target: str) -> bool:
    return _IPV4_RE.match(target) is not None or (":" in target and validate_target(target))
