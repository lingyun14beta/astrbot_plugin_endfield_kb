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
from urllib.parse import urlparse

MAX_BYTES = 256 * 1024 * 1024
USER_AGENT = "astrbot-plugin-endfield-kb/1.2"


class FetchError(RuntimeError):
    """下载或校验失败。"""


def is_public_url(url: str) -> bool:
    """判断 URL 是否指向公网地址，用于挡住内网 SSRF。

    解析域名后要求所有地址都是全局可路由地址；解析失败或为空视为不可用。

    Args:
        url: 待检查的链接。

    Returns:
        是公网 http/https 链接时为 True。
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
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
        raise FetchError("文件超过 256 MB 上限")
    return data


async def fetch(url: str, timeout: float = 15) -> bytes:
    """异步取一个 URL 的字节。"""
    return await asyncio.to_thread(_fetch, url, timeout)


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
