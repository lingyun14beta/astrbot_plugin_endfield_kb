"""检索结果出图：数据清洗、主题判定与 T2I 渲染。"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, time as clock_time
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
    """清洗正文：去掉资料引用串与开头的标题/集合名，保留对话换行。

    Args:
        title: 文档标题。
        coll: 集合名。
        body: 原始正文。
        limit: 保留的最大字数，<=0 表示不截断。

    Returns:
        可直接放进卡片的摘要文本。
    """
    raw = REF_RE.sub(" ", body or "")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for take in (2, 1):
        head = _canon(" ".join(lines[:take]))
        if head and head in {_canon(title), _canon(coll)}:
            lines = lines[take:]
            break
    else:
        if lines:
            stripped = _strip_heading(lines[0], title)
            if stripped == lines[0] and coll:
                stripped = _strip_heading(lines[0], coll)
            lines[0] = stripped

    cleaned: list[str] = []
    for line in lines:
        text = " ".join(line.split()).lstrip("·:：-— ")
        if text:
            cleaned.append(text)
    text = "\n".join(cleaned).lstrip("·:：-— ")
    if limit > 0 and len(text) > limit:
        text = text[:limit] + "…"
    return text


def _parse_time(value: str, fallback: clock_time) -> clock_time:
    """解析 HH:MM，失败时返回兜底值。"""
    match = HM_RE.match(value or "")
    if not match:
        return fallback
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return fallback
    return clock_time(hour, minute)


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
    start = _parse_time(dark_from, clock_time(19, 0))
    end = _parse_time(dark_to, clock_time(7, 0))
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
        self._last_warn = 0.0
        self.last_error = ""

    def _log_failure(self, logger: Any, message: str) -> None:
        """失败日志最多每 5 分钟 warning 一次，其余降为 debug。"""
        now = time.time()
        if now - self._last_warn > 300:
            self._last_warn = now
            logger.warning(message)
        else:
            logger.debug(message)

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
                self._item(index, hit, limit)
                for index, hit in enumerate(hits)
            ],
        }

    @staticmethod
    def _item(index: int, hit: Any, limit: int) -> dict[str, Any]:
        """拼一条结果的模板数据，长摘要走两列。"""
        excerpt = clean_excerpt(hit.title, hit.coll, hit.body, limit)
        return {
            "index": f"{index + 1:02d}",
            "kind": hit.kind_label,
            "title": hit.title,
            "coll": hit.coll,
            "excerpt": excerpt,
            "columns": len(excerpt) > 320 or excerpt.count("\n") >= 8,
            "refs": len(hit.ref_list),
        }

    @staticmethod
    def _valid_image(path: Any) -> bool:
        """检查落盘文件确实是图片，避免把错误页当成图片发出去。"""
        try:
            file = Path(str(path))
            if not file.is_file() or file.stat().st_size < 1024:
                return False
            with file.open("rb") as handle:
                head = handle.read(8)
        except OSError:
            return False
        return head.startswith(b"\xff\xd8\xff") or head.startswith(
            b"\x89PNG\r\n\x1a\n"
        )

    def _normalized_path(self, path: Any) -> str | None:
        """裁成 720 宽并统一存成 JPEG，避免端点忽略宽度或后缀不符。"""
        try:
            from PIL import Image
        except ImportError:
            return str(path) if self._valid_image(path) else None
        try:
            with Image.open(str(path)) as image:
                image.load()
                width, height = image.size
                if width > 720:
                    left = (width - 720) // 2
                    image = image.crop((left, 0, left + 720, height))
                out_dir = self.plugin.index_dir / ".t2i"
                out_dir.mkdir(parents=True, exist_ok=True)
                now = time.time()
                for old in out_dir.glob("*.jpg"):
                    try:
                        if now - old.stat().st_mtime > 3600:
                            old.unlink()
                    except OSError:
                        pass
                target = out_dir / f"{uuid.uuid4().hex}.jpg"
                image.convert("RGB").save(target, "JPEG", quality=95)
            return str(target)
        except (OSError, ValueError):
            return None

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
            self.last_error = f"模板读取失败：{exc}"
            self._log_failure(logger, f"[终末地KB] 出图模板读取失败：{exc}")
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
                    },
                    umo=umo,
                ),
                timeout=self.timeout() + 5,
            )
            if not path or not self._valid_image(path):
                raise RuntimeError("渲染结果不是有效图片")
            normalized = self._normalized_path(path)
            if not normalized:
                raise RuntimeError("图片裁切失败")
        except Exception as exc:  # noqa: BLE001 - 出图失败只影响展示
            self.last_error = str(exc) or exc.__class__.__name__
            self._log_failure(
                logger, f"[终末地KB] 出图失败，改用文本：{self.last_error}"
            )
            return None
        self.last_error = ""
        return normalized
