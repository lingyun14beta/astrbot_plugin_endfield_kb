"""插件视图的后端接口：统计、检索、更新、上传、按链接导入。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api.web import PluginUploadFile, error_response, json_response, request

from .. import index_fetch, semantic
from ..endfield_kb import KIND_LABELS, kind_counts

# 控制台「范围」筛选：界面选项 → 索引里的 kind
SCOPE_KINDS: dict[str, tuple[str, ...]] = {
    "story": ("original_story",),
    "archive": ("archive",),
    "wiki": (
        "knowledge",
        "story_wiki",
        "character_wiki",
        "character_activity_wiki",
        "reference",
    ),
    "character": ("character_profile", "character_voice"),
}


async def stats(plugin: Any):
    """返回索引统计，供控制台顶部状态条使用。"""
    if not plugin.db_path:
        return error_response("索引尚未构建", status_code=503)
    counts = kind_counts(plugin.db_path)
    return json_response(
        {
            "documents": int(plugin.meta.get("documents", 0)),
            "characters": int(plugin.meta.get("characters", 0)),
            "refs": int(plugin.meta.get("refs", 0)),
            "size_mb": round(plugin.db_path.stat().st_size / 1e6, 1),
            "built_at": plugin.meta.get("built_at", ""),
            "release": plugin.meta.get("release", ""),
            "index_version": plugin.meta.get("index_version", ""),
            "kinds": [
                {
                    "kind": kind,
                    "label": KIND_LABELS.get(kind, "资料"),
                    "count": count,
                }
                for kind, count in counts.items()
            ],
        }
    )


async def search(plugin: Any):
    """控制台检索接口。"""
    query = (request.query.get("q") or "").strip()
    if not query:
        return error_response("缺少查询词 q", status_code=400)
    if not plugin.db_path:
        return error_response("索引尚未构建", status_code=503)

    limit = request.query.get("k", 8, type=int) or 8
    limit = max(1, min(limit, 50))
    scope = (request.query.get("scope") or "").strip()
    kinds = SCOPE_KINDS.get(scope)
    rewrite = str(request.query.get("rewrite", "0")).lower() in {"1", "true", "yes"}

    started = time.perf_counter()
    used = query
    if rewrite:
        keywords = await plugin.retriever.rewrite(query, "")
        if keywords:
            used = keywords
    hits = await plugin.retriever.query(used, top_k=limit, kinds=kinds)
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    return json_response(
        {
            "query": query,
            "used_query": used,
            "rewritten": used != query,
            "scope": scope,
            "elapsed_ms": elapsed_ms,
            "count": len(hits),
            "hits": [
                {
                    "doc_id": hit.doc_id,
                    "title": hit.title,
                    "collection": hit.coll,
                    "kind": hit.kind,
                    "kind_label": hit.kind_label,
                    "score": round(hit.score, 1),
                    "semantic": hit.semantic,
                    "chars": len(hit.body),
                    "body": hit.body[:1600],
                    "refs": hit.ref_list[:60],
                }
                for hit in hits
            ],
        }
    )


async def updates(plugin: Any):
    """检查更新（仅对比版本）。"""
    try:
        return json_response(await plugin.updater.check_update())
    except Exception as exc:  # noqa: BLE001 - 统一转成错误响应
        return json_response({"ok": False, "message": str(exc)})


async def update(plugin: Any):
    """下载并安装最新索引。"""
    try:
        return json_response(await plugin.updater.apply_update())
    except Exception as exc:  # noqa: BLE001
        return error_response(str(exc), status_code=500)


async def upload(plugin: Any):
    """接收页面手动上传的索引文件。"""
    files = await request.files()
    item = files.get("file")
    if not isinstance(item, PluginUploadFile):
        return error_response("缺少文件", status_code=400)
    suffix = Path(item.filename or "index.zip").suffix.lower()
    if suffix not in {".zip", ".gz", ".sqlite3"}:
        return error_response("只支持 .zip / .gz / .sqlite3", status_code=400)

    target = plugin.index_dir / f".upload{suffix}"
    try:
        await item.save(target)
        result = await asyncio.to_thread(plugin.updater.install_from_file, target)
    except Exception as exc:  # noqa: BLE001
        return error_response(str(exc), status_code=400)
    finally:
        target.unlink(missing_ok=True)
    return json_response(result)


def guess_suffix(url: str, data: bytes) -> str:
    """按 URL 后缀或文件头判断格式。"""
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".zip", ".gz", ".sqlite3"}:
        return suffix
    if data[:4] == b"PK\x03\x04":
        return ".zip"
    if data[:2] == b"\x1f\x8b":
        return ".gz"
    if data[:16].startswith(b"SQLite format 3"):
        return ".sqlite3"
    raise ValueError("无法识别文件格式，仅支持 .zip / .gz / .sqlite3")


async def import_url(plugin: Any):
    """按用户提供的链接下载并导入索引。"""
    payload = await request.json(default={})
    url = str(payload.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return error_response("只支持 http/https 链接", status_code=400)
    if not await asyncio.to_thread(index_fetch.is_public_url, url):
        return error_response("只允许下载公网地址，内网链接已拒绝", status_code=400)
    try:
        data = await index_fetch.fetch(url, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return error_response(f"下载失败：{exc}", status_code=400)

    try:
        suffix = guess_suffix(url, data)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)

    target = plugin.index_dir / f".import{suffix}"
    target.write_bytes(data)
    try:
        result = await asyncio.to_thread(plugin.updater.install_from_file, target)
    except Exception as exc:  # noqa: BLE001
        return error_response(str(exc), status_code=400)
    finally:
        target.unlink(missing_ok=True)
    return json_response(result)


def _semantic_payload(plugin: Any) -> dict[str, Any]:
    """拼出语义检索的当前状态。"""
    conf = plugin.retriever.semantic_conf()
    meta = semantic.read_meta(plugin.retriever.semantic_dir())
    return {
        "enable": bool(conf.get("enable")),
        "index": meta,
        "building": plugin.vector_building(),
    }


async def semantic_state(plugin: Any):
    """返回语义检索开关与向量索引状态。"""
    return json_response(_semantic_payload(plugin))


async def semantic_save(plugin: Any):
    """从控制台切换语义检索开关。"""
    payload = await request.json(default={})
    group = dict(plugin.retriever.semantic_conf())
    if "enable" in payload:
        group["enable"] = bool(payload["enable"])
    plugin.config["semantic_group"] = group
    plugin.save_config()
    return json_response(_semantic_payload(plugin))
