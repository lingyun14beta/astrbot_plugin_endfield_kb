"""终末地中文知识库插件。

把本地 FTS5 索引（官方剧情原文 + 档案 + 整理 Wiki）注册为一个 LLM 工具，
模型在需要时主动调用；查询前可选地用对话模型把自然语言压成检索关键词。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from dataclasses import field
from pathlib import Path
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import PluginUploadFile, error_response, json_response, request
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from . import index_fetch
from .endfield_kb import KIND_LABELS, SCHEMA_VERSION, kind_counts, search
from .index_store import decompress, default_index_dir, install, resolve, validate_db

PLUGIN_NAME = "astrbot_plugin_endfield_kb"
PLUGIN_DIR = Path(__file__).parent

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

REWRITE_SYSTEM = "你是检索词提取器。只输出关键词，不要解释、不要编号。"
REWRITE_PROMPT = (
    "用户想找《明日方舟：终末地》里的一段剧情或设定，但他只记得大意。\n"
    "请把下面的描述改写成 5 到 10 个适合全文检索的中文关键词，"
    "优先使用游戏里可能出现的专名、地名、人名和书面词，不要输出原句。\n\n"
    "描述：{query}\n\n"
    "只输出一行关键词，用空格分隔。"
)

# 查询本身很短、且没有句子标点时，说明用户已经在给关键词，跳过改写省钱
SENTENCE_RE = re.compile(r"[。！？；，,]")

COMMAND_HELP = (
    "终末地知识库 · zmd\n"
    "  zmd <关键词>      检索，可直接贴原文片段\n"
    "  zmd 查 <关键词>   强制检索（查询词正好是“更新/帮助”等保留词时）\n"
    "  zmd               查看索引状态\n"
    "  zmd 重载          重新读取本地索引\n"
    "  zmd 更新          检查并下载最新索引\n"
    "  zmd 帮助          本条说明"
)


@dataclass
class EndfieldSearchTool(FunctionTool[AstrAgentContext]):
    """供 Agent 调用的终末地本地知识库检索工具。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    plugin: Any = None
    name: str = "endfield_kb_search"
    description: str = (
        "检索本地的《明日方舟：终末地》中文知识库，覆盖官方剧情原文（对话、无线电、"
        "环境语音、黑屏旁白）、中枢档案、角色档案与整理 Wiki。"
        "当用户问到终末地的剧情片段、人物关系、地点、事件、台词出处或世界观设定，"
        "而你不确定具体内容时调用。query 传简明的检索词或一句描述，不要整段复制用户消息；"
        "需要更多结果时可调大 top_k。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索词或一句简短描述，例如“小禾庄 祈愿树”或“管理员和祀的关系”。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回条数，默认 5，最大 10。",
                    "default": 5,
                },
            },
            "required": ["query"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        query: str,
        top_k: int = 5,
    ) -> ToolExecResult:
        umo = ""
        try:
            umo = context.context.event.unified_msg_origin
        except Exception:  # noqa: BLE001 - 上下文缺失时退回全局模型
            umo = ""
        return await self.plugin.search_text(query, umo, top_k)


@register(
    "astrbot_plugin_endfield_kb",
    "local",
    "终末地中文知识库：本地 BM25 检索剧情、档案与设定，零外部依赖",
    "v1.1.0",
    "",
)
class EndfieldKBPlugin(Star):
    """终末地本地知识库插件。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.index_dir = self._resolve_index_dir()
        self.db_path: Path | None = None
        self.meta: dict[str, str] = {}
        self.index_source = "missing"
        self._load_index()
        context.register_web_api(
            f"/{PLUGIN_NAME}/stats",
            self.page_stats,
            ["GET"],
            "终末地知识库统计",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/search",
            self.page_search,
            ["GET"],
            "终末地知识库检索",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/updates",
            self.page_updates,
            ["GET"],
            "检查索引更新",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/update",
            self.page_update,
            ["POST"],
            "下载并安装索引更新",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/upload",
            self.page_upload,
            ["POST"],
            "手动上传索引文件",
        )

    def _resolve_index_dir(self) -> Path:
        """确定索引目录：配置优先，其次 AstrBot 插件数据目录。"""
        explicit = str(self.config.get("index_dir") or "").strip()
        if explicit:
            return Path(explicit).expanduser()
        try:
            from astrbot.api.star import StarTools

            return StarTools.get_data_dir(PLUGIN_NAME) / "index"
        except Exception:  # noqa: BLE001 - 非标准部署时退回插件内目录
            return default_index_dir(PLUGIN_DIR)

    def _load_index(self) -> None:
        """扫描并校验本地索引；找不到时保持 None。"""
        db, meta, source = resolve(
            self.index_dir,
            legacy_dirs=[PLUGIN_DIR / "data"],
            schema_version=SCHEMA_VERSION,
        )
        self.db_path = db
        self.meta = meta
        self.index_source = source
        if db:
            logger.info(
                f"[终末地KB] 索引就绪：{meta.get('documents')} 篇 / "
                f"{meta.get('characters')} 字，来源 {source}，文件 {db}"
            )
        else:
            logger.warning(
                f"[终末地KB] 未找到可用索引（目录 {self.index_dir}）。"
                "可运行 build_index.py，或把索引文件放进该目录后执行 zmd 重载"
            )

    async def initialize(self) -> None:
        """插件激活时注册 LLM 工具。"""
        if not self.db_path:
            logger.warning("[终末地KB] 索引缺失，工具已注册但会返回提示信息")
        self.context.add_llm_tools(EndfieldSearchTool(plugin=self))
        logger.info("[终末地KB] 已注册工具 endfield_kb_search")

    # ------------------------------------------------------------------ 检索

    async def _keywords(self, query: str, umo: str) -> str:
        """用对话模型把长描述压成检索关键词。

        Args:
            query: 用户的原始描述。
            umo: 会话来源，用于选择该会话偏好的模型。

        Returns:
            空格分隔的关键词；不需要或失败时返回空串。
        """
        if not self.config.get("enable_rewrite", True):
            return ""
        if len(query) < 24 and not SENTENCE_RE.search(query):
            return ""
        try:
            provider = await self.context.get_using_provider_async(umo)
        except Exception as exc:  # noqa: BLE001 - 取模型失败不应阻断检索
            logger.debug(f"[终末地KB] 获取模型失败：{exc}")
            return ""
        if provider is None:
            return ""
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=REWRITE_PROMPT.format(query=query),
                    system_prompt=REWRITE_SYSTEM,
                    contexts=[],
                ),
                timeout=float(self.config.get("rewrite_timeout", 12)),
            )
        except Exception as exc:  # noqa: BLE001 - 超时或接口错误都退回原文查询
            logger.debug(f"[终末地KB] 关键词改写失败：{exc}")
            return ""
        text = (getattr(response, "completion_text", "") or "").strip()
        tokens = [t for t in re.split(r"[\s,，、;；|]+", text) if t]
        return " ".join(tokens[:10])

    async def search_text(self, query: str, umo: str, top_k: int | None = None) -> str:
        """执行一次检索并返回给模型阅读的文本。

        Args:
            query: 检索词或描述。
            umo: 会话来源。
            top_k: 返回条数，缺省读配置。

        Returns:
            带来源标注的片段文本。
        """
        query = (query or "").strip()
        if not query:
            return "查询为空。"
        if not self.db_path:
            return (
                "知识库索引尚未就绪。\n"
                f"请运行 build_index.py，或把索引文件放进 {self.index_dir} 后执行 zmd 重载。"
            )

        limit = int(top_k or self.config.get("top_k", 5))
        limit = max(1, min(limit, 10))
        ratio = float(self.config.get("score_ratio", 45)) / 100

        keywords = await self._keywords(query, umo)
        hits = search(self.db_path, keywords or query, top_k=limit, score_ratio=ratio)
        if not hits and keywords:
            hits = search(self.db_path, query, top_k=limit, score_ratio=ratio)
        if not hits:
            return f"没有检索到与「{query}」相关的内容。可以换更具体的专名或原文片段再试。"

        return self._format(hits, query)

    def _format(self, hits, query: str) -> str:
        max_chars = int(self.config.get("max_chars", 1800))
        lines = [f"检索「{query}」命中 {len(hits)} 条（本地 PRTS 终末地语料）："]
        used = 0
        for index, hit in enumerate(hits, 1):
            head = f"\n[{index}] [{hit.kind_label}]《{hit.title}》"
            if hit.coll:
                head += f" · {hit.coll}"
            refs = hit.ref_list
            tail = ""
            if refs:
                shown = "、".join(refs[:6])
                more = f" 等 {len(refs)} 处" if len(refs) > 6 else ""
                tail = f"\n出处：{shown}{more}"
            remain = max_chars - used - len(head) - len(tail)
            if remain <= 80:
                break
            body = hit.body.strip()
            if len(body) > remain:
                body = body[:remain] + "…"
            lines.append(head + "\n" + body + tail)
            used += len(head) + len(body) + len(tail)
        lines.append(
            "\n（标注为“整理”的是 PRTS 二次整理资料，当作线索；"
            "引用台词或具体情节请以“原文”条目为准。）"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ 页面接口

    async def page_stats(self):
        """返回索引统计，供插件视图顶部的状态条使用。"""
        if not self.db_path:
            return error_response("索引尚未构建", status_code=503)
        counts = kind_counts(self.db_path)
        return json_response(
            {
                "documents": int(self.meta.get("documents", 0)),
                "characters": int(self.meta.get("characters", 0)),
                "refs": int(self.meta.get("refs", 0)),
                "size_mb": round(self.db_path.stat().st_size / 1e6, 1),
                "built_at": self.meta.get("built_at", ""),
                "release": self.meta.get("release", ""),
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

    async def page_search(self):
        """供插件视图调用的检索接口。"""
        query = (request.query.get("q") or "").strip()
        if not query:
            return error_response("缺少查询词 q", status_code=400)
        if not self.db_path:
            return error_response("索引尚未构建", status_code=503)

        limit = request.query.get("k", 6, type=int) or 6
        limit = max(1, min(limit, 10))
        rewrite = str(request.query.get("rewrite", "0")).lower() in {"1", "true", "yes"}

        started = time.perf_counter()
        used = query
        if rewrite:
            keywords = await self._keywords(query, "")
            if keywords:
                used = keywords
        hits = search(self.db_path, used, top_k=limit)
        elapsed_ms = round((time.perf_counter() - started) * 1000)

        return json_response(
            {
                "query": query,
                "used_query": used,
                "rewritten": used != query,
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
                        "chars": len(hit.body),
                        "body": hit.body[:1600],
                        "refs": hit.ref_list[:60],
                    }
                    for hit in hits
                ],
            }
        )

    # ------------------------------------------------------------------ 更新

    @staticmethod
    def _short_release(value: str) -> str:
        """把长 release 名归一成版本号，例如 1.6.3。"""
        match = re.search(r"(\d+\.\d+\.\d+)", value or "")
        return match.group(1) if match else (value or "")

    def _manifest_urls(self) -> list[str]:
        return index_fetch.parse_urls(
            self.config.get("manifest_urls") or DEFAULT_MANIFEST_URLS
        )

    def _zip_urls(self) -> list[str]:
        return index_fetch.parse_urls(
            self.config.get("index_urls") or DEFAULT_INDEX_URLS
        )

    def install_from_file(self, path: Path) -> dict[str, Any]:
        """校验并安装一个本地索引文件（zip/gz/sqlite3）。

        Args:
            path: 待安装文件。

        Returns:
            安装结果，含文件名、篇数、版本。

        Raises:
            IndexValidationError: 文件损坏或版本不兼容。
        """
        work = self.index_dir / ".import"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        try:
            suffix = path.suffix.lower()
            if suffix in {".zip", ".gz"}:
                db = decompress(path, work)
            else:
                db = path
            meta = validate_db(db, expected_schema=SCHEMA_VERSION)
            raw_release = str(meta.get("release") or "")
            short = self._short_release(raw_release)
            label = (
                f"endfield-{short}"
                if re.fullmatch(r"\d+\.\d+\.\d+", short or "")
                else "manual"
            )
            installed = install(db, self.index_dir, label, SCHEMA_VERSION)
            self._load_index()
            return {
                "ok": True,
                "file": installed.name,
                "release": short or label,
                "documents": meta.get("_documents") or meta.get("documents"),
            }
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def check_update(self) -> dict[str, Any]:
        """比较本地与远端版本，不下载。"""
        manifest, source = await index_fetch.fetch_manifest(self._manifest_urls())
        current = self._short_release(self.meta.get("release", ""))
        if not manifest:
            return {
                "ok": True,
                "remote": None,
                "current": current,
                "up_to_date": True,
                "message": "无法获取更新清单（网络或镜像不可达）",
            }
        remote_release = str(manifest.get("release_id") or "")
        return {
            "ok": True,
            "current": current,
            "remote": {
                "release": self._short_release(remote_release),
                "documents": manifest.get("documents"),
                "size": manifest.get("size"),
                "built_at": manifest.get("built_at"),
            },
            "up_to_date": bool(current) and self._short_release(remote_release) == current,
            "source": source,
        }

    async def apply_update(self) -> dict[str, Any]:
        """下载并安装最新索引。"""
        manifest, _ = await index_fetch.fetch_manifest(self._manifest_urls())
        urls = index_fetch.parse_urls(manifest.get("urls")) if manifest else []
        sha = str(manifest.get("sha256") or "") if manifest else ""
        if not urls:
            urls = self._zip_urls()
        if not urls:
            return {"ok": False, "message": "没有配置下载地址"}

        data = await index_fetch.download(urls, sha)
        target = self.index_dir / ".download.zip"
        target.write_bytes(data)
        try:
            result = await asyncio.to_thread(self.install_from_file, target)
        finally:
            target.unlink(missing_ok=True)
        return result

    async def page_updates(self):
        """供页面调用的更新检查。"""
        try:
            return json_response(await self.check_update())
        except Exception as exc:  # noqa: BLE001 - 统一转成错误响应
            return json_response({"ok": False, "message": str(exc)})

    async def page_update(self):
        """供页面调用的更新安装。"""
        try:
            return json_response(await self.apply_update())
        except Exception as exc:  # noqa: BLE001
            return error_response(str(exc), status_code=500)

    async def page_upload(self):
        """接收页面手动上传的索引文件。"""
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("缺少文件", status_code=400)
        suffix = Path(upload.filename or "index.zip").suffix.lower()
        if suffix not in {".zip", ".gz", ".sqlite3"}:
            return error_response("只支持 .zip / .gz / .sqlite3", status_code=400)

        target = self.index_dir / f".upload{suffix}"
        try:
            await upload.save(target)
            result = await asyncio.to_thread(self.install_from_file, target)
        except Exception as exc:  # noqa: BLE001
            return error_response(str(exc), status_code=400)
        finally:
            target.unlink(missing_ok=True)
        return json_response(result)

    # ------------------------------------------------------------------ 指令

    def _status_text(self) -> str:
        if not self.meta or not self.db_path:
            return (
                "索引未就绪。\n"
                f"目录：{self.index_dir}\n"
                "可运行 build_index.py，或把索引文件放进该目录后执行 zmd 重载。"
            )
        size = self.db_path.stat().st_size / 1e6
        release = self.meta.get("release") or "未知"
        match = re.search(r"endfield-(\d+\.\d+\.\d+)", release)
        if match:
            release = f"终末地 {match.group(1)}"
        return (
            "终末地中文知识库\n"
            f"文档：{self.meta.get('documents')} 篇\n"
            f"字数：{self.meta.get('characters')}\n"
            f"出处：{self.meta.get('refs') or 0} 处\n"
            f"索引体积：{size:.1f} MB\n"
            f"构建时间：{self.meta.get('built_at')}\n"
            f"语料版本：{release}\n"
            f"文件：{self.db_path.name}（{self.index_source}）"
        )

    @filter.command("zmd")
    async def zmd(self, event: AstrMessageEvent):
        """终末地知识库：检索、状态、重载、帮助。"""
        text = (event.message_str or "").strip()
        for prefix in ("/zmd", "zmd", "/ZMD", "ZMD"):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        text = text.strip()

        if not text:
            yield event.plain_result(self._status_text() + "\n\n" + COMMAND_HELP)
            return

        head, _, rest = text.partition(" ")
        head = head.strip()

        if head in {"帮助", "help", "?"}:
            yield event.plain_result(COMMAND_HELP)
            return
        if head in {"重载", "reload"}:
            self._load_index()
            yield event.plain_result("索引已重载。\n\n" + self._status_text())
            return
        if head in {"更新", "update"}:
            yield event.plain_result("正在检查并下载更新…")
            try:
                result = await self.apply_update()
            except Exception as exc:  # noqa: BLE001 - 网络/校验失败统一提示
                yield event.plain_result(f"更新失败：{exc}")
                return
            if result.get("ok"):
                yield event.plain_result(
                    f"索引已更新：{result.get('release')} / {result.get('documents')} 篇\n\n"
                    + self._status_text()
                )
            else:
                yield event.plain_result(result.get("message") or "更新失败")
            return

        query = text
        if head in {"查", "search"}:
            query = rest.strip() or text
        yield event.plain_result(
            await self.search_text(query, event.unified_msg_origin, 5)
        )
