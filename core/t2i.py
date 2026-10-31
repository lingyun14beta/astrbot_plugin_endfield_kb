"""检索结果出图：数据清洗、主题判定与 T2I 渲染。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, time
from pathlib import Path
from typing import Any

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "zmd_card.html"
REF_RE = re.compile(r"Reading/\S+")
CANON_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
HM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")

DEFAULT_DARK_FROM = "19:00"
DEFAULT_DARK_TO = "07:00"


def _canon(text: str) -> str:
    """去掉标点与空白，用于比较标题和正文开头。"""
    return CANON_RE.sub("", text or "")


def _norm(text: str) -> str:
    """把连续空白压成单个空格。"""
    return " ".join((text or "").split())


def _strip_heading(text: str, prefix: str) -> str:
    """去掉正文开头重复的标题/集合名。

    只有开头的字符逐个匹配 prefix，并且后面紧跟分隔符时才截断；
    避免把正文里的时间、说话人误当成标题删掉。
    """
    target = _canon(prefix)
    if not target:
        return text
    position = 0
    cut = -1
    for index, char in enumerate(text):
        if CANON_RE.match(char) is not None:
            continue
        if position >= len(target) or char.lower() != target[position].lower():
            return text
        position += 1
        if position == len(target):
            cut = index + 1
            break
    if cut < 0:
        return text
    rest = text[cut:].lstrip()
    if rest[:1] in {"·", ":", "：", "-", "—"}:
        return rest.lstrip("·:：-— ")
    return text


def clean_excerpt(title: str, coll: str, body: str, limit: int = 500) -> str:
    """清洗正文：去掉资料引用串与开头的标题/集合名，再按上限截断。

    Args:
        title: 文档标题。
        coll: 集合名。
        body: 原始正文。
        limit: 保留的最大字数，<=0 表示不截断。

    Returns:
        可直接放进卡片的摘要文本。
    """
    text = _norm(REF_RE.sub(" ", body or ""))
    stripped = _strip_heading(text, title)
    if stripped == text and coll:
        stripped = _strip_heading(text, coll)
    text = stripped.lstrip("·:：-— ")
    if limit > 0 and len(text) > limit:
        text = text[:limit] + "…"
    return text


def _parse_time(value: str, fallback: time) -> time:
    """解析 HH:MM，失败时返回兜底值。"""
    match = HM_RE.match(value or "")
    if not match:
        return fallback
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return fallback
    return time(hour, minute)


def resolve_theme(
    theme: str,
    dark_from: str = DEFAULT_DARK_FROM,
    dark_to: str = DEFAULT_DARK_TO,
    now: datetime | None = None,
) -> str:
    """决定卡片用浅色还是深色。

    auto 按服务器本地时间：默认 19:00 到次日 07:00 用深色。

    Args:
        theme: light / dark / auto。
        dark_from: 夜间开始时间。
        dark_to: 夜间结束时间。
        now: 用于测试的时间点，默认取当前时间。

    Returns:
        light 或 dark。
    """
    choice = (theme or "auto").strip().lower()
    if choice in {"light", "dark"}:
        return choice
    moment = now or datetime.now()
    start = _parse_time(dark_from, time(19, 0))
    end = _parse_time(dark_to, time(7, 0))
    current = moment.time()
    if start <= end:
        dark = start <= current < end
    else:
        dark = current >= start or current < end
    return "dark" if dark else "light"


class CardRenderer:
    """把检索结果渲染成 720 宽的图片。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._warned = False

    def conf(self) -> dict[str, Any]:
        """读取出图配置。"""
        group = self.plugin.config.get("t2i_group")
        return group if isinstance(group, dict) else {}

    def enabled(self) -> bool:
        """是否开启出图。"""
        return bool(self.conf().get("enable"))

    def max_hits(self) -> int:
        """结果条数，限制在 1-8。"""
        try:
            value = int(self.conf().get("max_hits", 5) or 5)
        except (TypeError, ValueError):
            value = 5
        return max(1, min(value, 8))

    def excerpt_chars(self) -> int:
        """每条摘要上限，限制在 500-2000。"""
        try:
            value = int(self.conf().get("excerpt_chars", 500) or 500)
        except (TypeError, ValueError):
            value = 500
        return max(500, min(value, 2000))

    def timeout(self) -> float:
        """渲染超时秒数，限制在 5-60。"""
        try:
            value = float(self.conf().get("timeout", 20) or 20)
        except (TypeError, ValueError):
            value = 20
        return max(5.0, min(value, 60.0))

    def _corpus(self) -> tuple[str, str]:
        """返回页脚用的语料名与顶部用的版本号。"""
        release = str(self.plugin.meta.get("release") or "")
        match = re.search(r"endfield-(\d+\.\d+\.\d+)", release)
        if match:
            version = match.group(1)
            return f"终末地 {version}", version
        return release or "本地语料", "LOCAL"

    def _data(self, hits: list[Any], query: str, elapsed_ms: int) -> dict[str, Any]:
        """拼出模板数据。"""
        limit = self.excerpt_chars()
        corpus, corpus_version = self._corpus()
        return {
            "query": query,
            "count": len(hits),
            "elapsed_ms": elapsed_ms,
            "corpus": corpus,
            "corpus_version": corpus_version,
            "index_version": str(
                self.plugin.meta.get("index_version")
                or self.plugin.local_release
                or "unknown"
            ),
            "refs_total": f"{int(self.plugin.meta.get('refs') or 0):,}",
            "theme_class": (
                "theme-dark"
                if resolve_theme(
                    str(self.conf().get("theme") or "auto"),
                    str(self.conf().get("dark_from") or DEFAULT_DARK_FROM),
                    str(self.conf().get("dark_to") or DEFAULT_DARK_TO),
                )
                == "dark"
                else ""
            ),
            "hits": [
                {
                    "index": f"{index + 1:02d}",
                    "kind": hit.kind_label,
                    "title": hit.title,
                    "coll": hit.coll,
                    "excerpt": clean_excerpt(hit.title, hit.coll, hit.body, limit),
                    "refs": len(hit.ref_list),
                }
                for index, hit in enumerate(hits)
            ],
        }

    @staticmethod
    def _valid_image(path: Any) -> bool:
        """检查落盘文件确实是图片，避免把错误页当成图片发出去。"""
        try:
            file = Path(str(path))
            if not file.is_file() or file.stat().st_size < 1024:
                return False
            head = file.read_bytes()[:8]
        except OSError:
            return False
        return head.startswith(b"\xff\xd8\xff") or head.startswith(
            b"\x89PNG\r\n\x1a\n"
        )

    async def render(
        self,
        hits: list[Any],
        query: str,
        elapsed_ms: int,
        umo: str,
    ) -> str | None:
        """渲染卡片；任何失败都返回 None，由调用方退回文本。"""
        from astrbot.api import logger

        try:
            template = TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            if not self._warned:
                self._warned = True
                logger.warning(f"[终末地KB] 出图模板读取失败：{exc}")
            return None

        try:
            path = await asyncio.wait_for(
                self.plugin.html_render(
                    template,
                    self._data(hits, query, elapsed_ms),
                    return_url=False,
                    options={
                        "type": "jpeg",
                        "quality": 95,
                        "full_page": True,
                        "width": 720,
                        "timeout": int(self.timeout() * 1000),
                    },
                    umo=umo,
                ),
                timeout=self.timeout() + 5,
            )
            if not path or not self._valid_image(path):
                raise RuntimeError("渲染结果不是有效图片")
        except Exception as exc:  # noqa: BLE001 - 出图失败只影响展示
            if self._warned:
                logger.debug(f"[终末地KB] 出图失败，改用文本：{exc}")
            else:
                self._warned = True
                logger.warning(f"[终末地KB] 出图失败，改用文本：{exc}")
            return None
        return str(path)
