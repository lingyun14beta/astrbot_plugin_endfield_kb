"""版本号与下载源相关的纯逻辑，不依赖 AstrBot。"""

from __future__ import annotations

import re
from typing import Any

from .. import index_fetch

GH_BASE = "https://github.com/lingyun14beta/astrbot-endfield-kb-index"
MS_BASE = "https://www.modelscope.cn/api/v1/datasets/lingyun14/astrbot-endfield-kb-index"
DEFAULT_MANIFEST_URLS = (
    f"{GH_BASE}/releases/latest/download/manifest.json\n"
    f"{MS_BASE}/repo?Revision=master&FilePath=manifest.json"
)
DEFAULT_INDEX_URLS = (
    f"{GH_BASE}/releases/latest/download/endfield_kb-latest.sqlite3.zip\n"
    f"{MS_BASE}/repo?Revision=master&FilePath=endfield_kb-latest.sqlite3.zip"
)


def version_key(value: str) -> str:
    """版本比较用：保留 semver 后缀，例如 1.6.3-beta、1.6.3-r1。"""
    match = re.search(r"(\d+\.\d+\.\d+)(-[0-9A-Za-z.]+)?", value or "")
    if not match:
        return (value or "").strip().lower()
    return f"{match.group(1)}{(match.group(2) or '')}".lower()


def repair_url_value(value: object) -> list[str] | None:
    """把字符串或被拆成单字符的列表还原成地址列表。

    Args:
        value: 配置里读到的原值。

    Returns:
        修好后的地址列表；本来就正常时返回 None，表示无需改动。
    """
    if isinstance(value, str):
        return index_fetch.parse_urls(value)
    if (
        isinstance(value, list)
        and len(value) > 8
        and all(isinstance(item, str) and len(item) == 1 for item in value)
        and "://" in "".join(str(item) for item in value)
    ):
        return index_fetch.parse_urls("".join(str(item) for item in value))
    return None


def custom_urls(config: dict[str, Any], key: str) -> list[str]:
    """自定义源开关打开时返回用户填的地址，否则返回空表。"""
    if not config.get("custom_sources"):
        return []
    return index_fetch.parse_urls(config.get(key))


def manifest_urls(config: dict[str, Any]) -> list[str]:
    """更新清单地址，自定义优先，否则用内置。"""
    return custom_urls(config, "manifest_urls") or index_fetch.parse_urls(
        DEFAULT_MANIFEST_URLS
    )


def zip_urls(config: dict[str, Any]) -> list[str]:
    """索引下载地址，自定义优先，否则用内置。"""
    return custom_urls(config, "index_urls") or index_fetch.parse_urls(
        DEFAULT_INDEX_URLS
    )
