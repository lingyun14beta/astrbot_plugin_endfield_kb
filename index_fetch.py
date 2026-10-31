"""索引更新：拉 manifest、下载 zip、校验。

只依赖标准库；网络请求放到线程里，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

MAX_BYTES = 100 * 1024 * 1024
USER_AGENT = "astrbot-plugin-endfield-kb/1.2"


class FetchError(RuntimeError):
    """下载或校验失败。"""


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跟随重定向前复查目标地址，避免跳进内网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        if not is_public_url(target):
            raise FetchError("重定向目标不是公网 https 地址")
        return super().redirect_request(req, fp, code, msg, headers, target)


def is_public_url(url: str) -> bool:
    """判断 URL 是否是可用的公网 https 链接，用于挡住内网 SSRF。

    只接受 https；解析域名后要求所有地址都是全局可路由地址。

    Args:
        url: 待检查的链接。

    Returns:
        是公网 https 链接时为 True。
    """
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise FetchError("文件超过 100 MB 上限")
    return data


async def fetch(url: str, timeout: float = 15) -> bytes:
    """异步取一个 URL 的字节。"""
    return await asyncio.to_thread(_fetch, url, timeout)


def _fetch_public(url: str, timeout: float) -> bytes:
    """带公网校验的下载，重定向也会复查。"""
    if not is_public_url(url):
        raise FetchError("只允许下载公网 https 地址")
    opener = urllib.request.build_opener(_PublicRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise FetchError("文件超过 100 MB 上限")
    return data


async def fetch_public(url: str, timeout: float = 120) -> bytes:
    """异步下载公网 https 资源，拒绝内网与非法重定向。"""
    return await asyncio.to_thread(_fetch_public, url, timeout)


def parse_urls(value: str | list[str] | None) -> list[str]:
    """把配置里的多行文本或列表解析成 URL 列表。"""
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value).splitlines() if line.strip()]


async def fetch_manifest(
    urls: list[str], timeout: float = 5
) -> tuple[dict | None, str | None]:
    """按顺序尝试 manifest，返回 (内容, 命中的 URL)。"""
    for url in urls:
        try:
            raw = await fetch(url, timeout)
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - 单个源失败继续下一个
            continue
        if isinstance(data, dict) and data.get("urls"):
            return data, url
    return None, None


async def download(urls: list[str], sha256: str = "", timeout: float = 90) -> bytes:
    """按顺序下载，校验 sha256；全部失败时抛出 FetchError。"""
    errors: list[str] = []
    for url in urls:
        try:
            data = await fetch(url, timeout)
            if sha256 and hashlib.sha256(data).hexdigest() != sha256:
                raise FetchError("sha256 不匹配")
            return data
        except Exception as exc:  # noqa: BLE001 - 换下一个源
            errors.append(f"{url}: {exc}")
    raise FetchError("所有下载源都失败：" + " | ".join(errors))


def sha256_of(path: Path) -> str:
    """计算文件 sha256。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
