#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""播放 URL 与 IP 的安全校验、日志脱敏（纯函数，不发起任何网络请求）。

职责：
- validate_audio_url：校验音箱播放 URL（SSRF 防护），返回原 URL 或 None；
- _url_host_ip / _is_private_ip / _is_loopback_or_linklocal：URL/IP 判定辅助；
- safe_url：日志脱敏（去掉 query，防 token/签名泄漏到日志）。

不负责：
- 不发起 HTTP 请求（DNS 解析除外，_url_host_ip 用 socket.getaddrinfo）；
- 不做鉴权状态、业务路由、播放调度；
- 不读配置：ALLOW_PRIVATE_URLS 由调用方（xiaogpt-bridge 启动时）设置。

依赖：仅标准库（ipaddress / socket / urllib.parse / re）。
"""
import ipaddress
import re
import socket
import urllib.parse

# 默认放行私网 LAN（音箱播放的 relay URL = http://<Mac-IP>:4378/stream 是私网
# 地址，属正常链路）；loopback/link-local/metadata 永远拒绝。
# playback.allow_private_urls=false 可开严格模式（连私网 LAN 也拒绝）。
ALLOW_PRIVATE_URLS = True
MAX_URL_LEN = 2048
_URL_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_URL_WHITESPACE_RE = re.compile(r"\s")


def _url_host_ip(hostname: str):
    """解析主机名对应的 IP 集合（用于 DNS 解析后校验，尽力而为）。"""
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        return {info[4][0] for info in infos}
    except OSError:
        return set()


def _is_private_ip(ip: str) -> bool:
    """判断 IP 是否私网 LAN（192.168/16、10/8、172.16/12）。
    仅用于严格模式（playback.allow_private_urls=false）下拒绝私网播放地址；
    loopback/link-local/metadata 由 _is_loopback_or_linklocal 单独判定。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return bool(addr.is_private)


def _is_loopback_or_linklocal(ip: str) -> bool:
    """仅 loopback/link-local/metadata/未指定（私网 LAN 放行——音箱播放的
    relay URL 就是 http://<Mac局域网IP>:4378/stream，属正常链路）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_link_local or addr.is_unspecified
                or addr.is_multicast or addr.is_reserved
                or str(addr) == "169.254.169.254")


def validate_audio_url(url: str) -> str | None:
    """校验由音箱播放的 URL（migpt /play_url 链路）。

    拒绝：非 http/https、userinfo（凭据泄露面）、控制/空白字符、超长、
    localhost / loopback（127.0.0.0/8、::1、0.0.0.0）/ link-local 网段
    （含云 metadata 地址 169.254.169.254）/ 未指定。

    放行：公网 + 私网 LAN（192.168/16、10/8、172.16/12）——音箱播放的
    直连 relay URL（http://<Mac-IP>:4378/stream）就是私网地址，属正常链路；
    私网放行也可配置关闭（playback.allow_private_urls=false，严格模式）。

    Mac 侧真正的高危 SSRF 面（relay 拉取任意 URL、可达 127.0.0.1:8123 等）
    由 web-audio-play.py 的 relay 校验兜底（那里拒绝全部私网）。"""
    if not url or not isinstance(url, str):
        return None
    if len(url) > MAX_URL_LEN:
        return None
    if _URL_CTRL_RE.search(url) or _URL_WHITESPACE_RE.search(url):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.username or parsed.password or parsed.netloc.startswith("@"):
        return None  # userinfo 一律拒绝（凭据泄露面）
    host = parsed.hostname or ""
    if not host:
        return None
    host_l = host.lower()
    if host_l in ("localhost", "localhost.localdomain"):
        return None
    # IPv6/IPv4 字面量直接判；域名解析后逐个判（尽力而为）
    try:
        probe = ipaddress.ip_address(host)
        if _is_loopback_or_linklocal(str(probe)):
            return None
        if not ALLOW_PRIVATE_URLS and _is_private_ip(str(probe)):
            return None
        return url
    except ValueError:
        pass  # 不是 IP 字面量，按域名处理
    ips = _url_host_ip(host)
    if ips:
        for ip in ips:
            if _is_loopback_or_linklocal(ip):
                return None
            if not ALLOW_PRIVATE_URLS and _is_private_ip(ip):
                return None
        return url
    return None  # 域名解析失败：宁缺毋滥


def safe_url(url: str) -> str:
    """日志脱敏：只打 scheme://host/path，去掉 query（可能含 token/签名）。"""
    try:
        p = urllib.parse.urlsplit(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except ValueError:
        return "(invalid url)"
