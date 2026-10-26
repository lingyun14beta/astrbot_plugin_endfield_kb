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
from urllib.parse import urlparse

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import PluginUploadFile, error_response, json_response, request
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from . import index_fetch, notify
from .endfield_kb import KIND_LABELS, SCHEMA_VERSION, kind_counts, search
from .index_store import (
    decompress,
    default_index_dir,
    install,
    read_current,
    resolve,
    validate_db,
)

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

REWRITE_SYSTEM = "你是检索词提取器。只输出关键词，不要解释、不要编号。"

# 从当前语料里统计出的高频设定词；只做提示，不改写用户本意
REWRITE_GLOSSARY = (
    "地点：武陵、谷地、帝江号、小禾庄、源石发电站、围陋村、心脏修缮站\n"
    "势力：终末地工业、工团、天师、裂地者、天使\n"
    "概念：协议源石、源石技术、传送网络、侵蚀、巨兽心脏"
)

REWRITE_PROMPT = (
    "用户想找《明日方舟：终末地》里的一段剧情或设定，但他只记得大意。\n"
    "请把下面的描述改写成 5 到 10 个适合全文检索的中文关键词，"
    "优先使用游戏里可能出现的专名、地名和书面词，不要输出原句。\n\n"
    "可以参考下面的常见词，但只在描述确实涉及时才用，最多挑 2 个：\n"
    "{glossary}\n\n"
    "描述里已经出现的专名要原样保留，不要换成同义词。\n\n"
    "描述：{query}\n\n"
    "只输出一行关键词，用空格分隔。"
)

COMMAND_HELP = (
    "终末地知识库 · 指令\n"
    "  /zmd <关键词>   检索，可直接贴原文片段\n"
    "  /zmd            不带关键词，显示索引状态\n"
    "  /zmd状态        索引篇数、语料版本与体积\n"
    "  /zmd重载        重新扫描本地索引\n"
    "  /zmd更新        检查并下载最新索引\n"
    "  /zmd帮助        本条说明\n"
    "  /zmd订阅        当前会话加入版本推送（管理员）\n"
    "  /zmd退订        移出版本推送（管理员）\n"
    "  /zmd推送状态    订阅列表与最近检查时间（管理员）"
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
        return await self.plugin.search_text(query, top_k)


class EndfieldKBPlugin(Star):
    """终末地本地知识库插件。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._normalize_sources()
        self.index_dir = self._resolve_index_dir()
        self.db_path: Path | None = None
        self.meta: dict[str, str] = {}
        self.index_source = "missing"
        self.local_release = ""
        self._notify_task: asyncio.Task | None = None
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
        context.register_web_api(
            f"/{PLUGIN_NAME}/import_url",
            self.page_import_url,
            ["POST"],
            "按链接导入索引",
        )

    @staticmethod
    def _repair_url_value(value: object) -> list[str] | None:
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

    def _normalize_sources(self) -> None:
        """把旧版的字符串地址迁移成列表并落盘。

        旧 schema 用 text 存多行地址；改成 list 后，残留的字符串会被列表
        编辑器按字符拆开，所以在加载时统一修一次。
        """
        changed = False
        for key in ("manifest_urls", "index_urls"):
            repaired = self._repair_url_value(self.config.get(key))
            if repaired is not None:
                self.config[key] = repaired
                changed = True
        if not changed:
            return
        try:
            self.config.save_config()
        except Exception as exc:  # noqa: BLE001 - 存不下去也不影响本次运行
            logger.debug(f"[终末地KB] 地址配置迁移未落盘：{exc}")

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
        self.local_release = (
            self._version_key(meta.get("index_version") or meta.get("release", ""))
            if db
            else ""
        )
        if db:
            current = read_current(self.index_dir) or {}
            if current.get("release_id"):
                self.local_release = self._version_key(str(current["release_id"]))
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
        """插件激活时注册 LLM 工具并启动版本检查。"""
        if not self.db_path:
            logger.warning("[终末地KB] 索引缺失，工具已注册但会返回提示信息")
        self.context.add_llm_tools(EndfieldSearchTool(plugin=self))
        self._notify_task = asyncio.create_task(self._notify_loop())
        logger.info("[终末地KB] 已注册工具 endfield_kb_search")

    async def terminate(self) -> None:
        """插件卸载/重载时停掉后台检查。"""
        if self._notify_task is None:
            return
        self._notify_task.cancel()
        try:
            await self._notify_task
        except asyncio.CancelledError:
            pass
        finally:
            self._notify_task = None

    # ------------------------------------------------------------------ 检索

    async def _keywords(self, query: str, umo: str) -> str:
        """用对话模型把描述压成检索关键词。

        只在调用方明确要求时执行（控制台勾选、或显式调用），不做长度判断：
        勾了就一定会问模型，没勾就一次都不问。

        Args:
            query: 用户的原始描述。
            umo: 会话来源，用于选择该会话偏好的模型。

        Returns:
            空格分隔的关键词；失败时返回空串。
        """
        provider = None
        provider_id = str(self.config.get("rewrite_provider") or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
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
                    prompt=REWRITE_PROMPT.format(
                        glossary=REWRITE_GLOSSARY, query=query
                    ),
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

    async def search_text(self, query: str, top_k: int | None = None) -> str:
        """执行一次检索并返回给模型阅读的文本。

        Args:
            query: 检索词或描述。
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
                "index_version": self.meta.get("index_version", ""),
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

        limit = request.query.get("k", 8, type=int) or 8
        limit = max(1, min(limit, 50))
        scope = (request.query.get("scope") or "").strip()
        kinds = SCOPE_KINDS.get(scope)
        rewrite = str(request.query.get("rewrite", "0")).lower() in {"1", "true", "yes"}

        started = time.perf_counter()
        used = query
        if rewrite:
            keywords = await self._keywords(query, "")
            if keywords:
                used = keywords
        hits = search(self.db_path, used, top_k=limit, kinds=kinds)
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
    def _version_key(value: str) -> str:
        """版本比较用：保留 semver 后缀，例如 1.6.3-beta、1.6.3-r1。"""
        match = re.search(r"(\d+\.\d+\.\d+)(-[0-9A-Za-z.]+)?", value or "")
        if not match:
            return (value or "").strip().lower()
        return f"{match.group(1)}{(match.group(2) or '')}".lower()

    def _custom_urls(self, key: str) -> list[str]:
        """自定义源开关打开时返回用户填的地址，否则返回空表。

        Args:
            key: 配置项名（manifest_urls 或 index_urls）。

        Returns:
            地址列表；开关关闭或列表为空时为空表，调用方据此回退内置地址。
        """
        if not self.config.get("custom_sources"):
            return []
        return index_fetch.parse_urls(self.config.get(key))

    def _manifest_urls(self) -> list[str]:
        return self._custom_urls("manifest_urls") or index_fetch.parse_urls(
            DEFAULT_MANIFEST_URLS
        )

    def _zip_urls(self) -> list[str]:
        return self._custom_urls("index_urls") or index_fetch.parse_urls(
            DEFAULT_INDEX_URLS
        )

    def install_from_file(self, path: Path, release_hint: str = "") -> dict[str, Any]:
        """校验并安装一个本地索引文件（zip/gz/sqlite3）。

        Args:
            path: 待安装文件。
            release_hint: 清单里的版本标识，优先于库内 meta。

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
            hint = release_hint or str(
                meta.get("index_version") or meta.get("release") or ""
            )
            key = self._version_key(hint)
            label = (
                f"endfield-{key}"
                if re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", key or "")
                else "manual"
            )
            installed = install(
                db, self.index_dir, label, SCHEMA_VERSION, move_source=True
            )
            self._load_index()
            return {
                "ok": True,
                "file": installed.name,
                "release": key or label,
                "documents": meta.get("_documents") or meta.get("documents"),
            }
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def check_update(self) -> dict[str, Any]:
        """比较本地与远端版本，不下载。"""
        manifest, source = await index_fetch.fetch_manifest(self._manifest_urls())
        current = self.local_release or self._version_key(
            self.meta.get("index_version") or self.meta.get("release", "")
        )
        if not manifest:
            return {
                "ok": True,
                "remote": None,
                "current": current,
                "up_to_date": True,
                "message": "无法获取更新清单（网络或镜像不可达）",
            }
        remote_release = str(manifest.get("release_id") or "")
        remote_key = self._version_key(remote_release)
        return {
            "ok": True,
            "current": current,
            "remote": {
                "release": remote_key,
                "documents": manifest.get("documents"),
                "size": manifest.get("size"),
                "built_at": manifest.get("built_at"),
            },
            "up_to_date": bool(current) and remote_key == current,
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
            hint = str(manifest.get("release_id") or "") if manifest else ""
            result = await asyncio.to_thread(self.install_from_file, target, hint)
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

    @staticmethod
    def _guess_suffix(url: str, data: bytes) -> str:
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

    async def page_import_url(self):
        """按用户提供的链接下载并导入索引。"""
        payload = await request.json(default={})
        url = str(payload.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return error_response("只支持 http/https 链接", status_code=400)
        try:
            data = await index_fetch.fetch(url, timeout=120)
        except Exception as exc:  # noqa: BLE001
            return error_response(f"下载失败：{exc}", status_code=400)

        try:
            suffix = self._guess_suffix(url, data)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

        target = self.index_dir / f".import{suffix}"
        target.write_bytes(data)
        try:
            result = await asyncio.to_thread(self.install_from_file, target)
        except Exception as exc:  # noqa: BLE001
            return error_response(str(exc), status_code=400)
        finally:
            target.unlink(missing_ok=True)
        return json_response(result)

    # ------------------------------------------------------------------ 版本推送

    def _notify_state_path(self) -> Path:
        """通知状态的落盘位置，与索引目录同级。"""
        return self.index_dir.parent / notify.STATE_NAME

    def _notify_umos(self) -> list[str]:
        """读取推送目标，过滤掉空值和异常类型。"""
        raw = self.config.get("notify_umos") or []
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    def _save_config(self) -> None:
        """把运行时改动的配置写回文件。"""
        try:
            self.config.save_config()
        except Exception as exc:  # noqa: BLE001 - 保存失败不影响当前会话
            logger.warning(f"[终末地KB] 配置保存失败：{exc}")

    async def _notify_loop(self) -> None:
        """按间隔检查版本；异常只记日志，不退出循环。"""
        while True:
            try:
                await self._check_version_notice()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[终末地KB] 版本检查失败：{exc}")
            interval = max(5, int(self.config.get("notify_interval", 60) or 60))
            await asyncio.sleep(interval * 60)

    async def _check_version_notice(self) -> None:
        """查一次版本，有变化就推送到已订阅的会话。"""
        if not self.config.get("notify_enable", True):
            return
        path = self._notify_state_path()
        state = notify.load_state(path)
        stored = str(state.get("main") or "")
        status = await asyncio.to_thread(notify.fetch_status, stored)
        main = status["main"]
        pre = status["pre"]
        stamp = time.strftime("%Y-%m-%d %H:%M")

        if not stored:
            notify.save_state(path, {"main": main, "pre": pre, "checked_at": stamp})
            logger.info(f"[终末地KB] 首次记录版本 {main}，静默写入不发通知")
            return

        messages: list[str] = []
        if notify.is_newer(main, stored):
            messages.append(
                notify.render(
                    "main", main, stored, status["total"], status["incremental"]
                )
            )
        if self.config.get("notify_pre_download", True):
            old_pre = str(state.get("pre") or "")
            if pre and pre != old_pre:
                messages.append(
                    notify.render(
                        "pre", pre, old_pre, status["total"], status["pre_incremental"]
                    )
                )
            elif not pre and old_pre:
                messages.append(notify.render("pre-remove", "", old_pre, 0, 0))

        umos = self._notify_umos()
        if messages and umos:
            sent = 0
            for umo in umos:
                for text in messages:
                    try:
                        if await self.context.send_message(
                            umo, MessageChain([Plain(text=text)])
                        ):
                            sent += 1
                    except Exception as exc:  # noqa: BLE001 - 单个会话失败不影响其他
                        logger.warning(f"[终末地KB] 推送到 {umo} 失败：{exc}")
            if not sent:
                logger.warning("[终末地KB] 本次通知全部失败，保持旧状态下轮重试")
                return

        notify.save_state(path, {"main": main, "pre": pre, "checked_at": stamp})

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
        index_version = self.meta.get("index_version") or ""
        lines = [
            "终末地中文知识库",
            f"文档：{self.meta.get('documents')} 篇",
            f"字数：{self.meta.get('characters')}",
            f"出处：{self.meta.get('refs') or 0} 处",
            f"索引体积：{size:.1f} MB",
            f"构建时间：{self.meta.get('built_at')}",
            f"语料版本：{release}",
        ]
        if index_version:
            lines.append(f"索引版本：{index_version}")
        lines.append(f"文件：{self.db_path.name}（{self.index_source}）")
        return "\n".join(lines)

    @staticmethod
    def _command_args(event: AstrMessageEvent) -> str:
        """取指令名之后的剩余文本，用于检索关键词。"""
        parts = (event.message_str or "").strip().split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @filter.command("zmd", desc="检索终末地知识库；不带关键词时显示状态")
    async def zmd_search(self, event: AstrMessageEvent):
        query = self._command_args(event)
        if not query:
            yield event.plain_result(self._status_text() + "\n\n" + COMMAND_HELP)
            return
        legacy = {
            "帮助": "zmd帮助",
            "重载": "zmd重载",
            "更新": "zmd更新",
            "状态": "zmd状态",
        }
        if query in legacy:
            yield event.plain_result(f"指令已拆分，请改用 /{legacy[query]}")
            return
        yield event.plain_result(await self.search_text(query, 5))

    @filter.command("zmd状态", desc="查看索引篇数、语料版本与磁盘占用")
    async def zmd_status(self, event: AstrMessageEvent):
        yield event.plain_result(self._status_text())

    @filter.command("zmd帮助", desc="列出全部 zmd 指令")
    async def zmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(COMMAND_HELP)

    @filter.command("zmd重载", desc="重新扫描本地索引文件")
    async def zmd_reload(self, event: AstrMessageEvent):
        self._load_index()
        yield event.plain_result("索引已重载。\n\n" + self._status_text())

    @filter.command("zmd更新", desc="检查并安装最新索引")
    async def zmd_update(self, event: AstrMessageEvent):
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

    @filter.command("zmd订阅", desc="把当前会话加入版本推送（仅管理员）")
    async def zmd_subscribe(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员能修改推送订阅。")
            return
        umo = event.unified_msg_origin
        umos = self._notify_umos()
        if umo in umos:
            yield event.plain_result(f"本会话已订阅：\n{umo}")
            return
        umos.append(umo)
        self.config["notify_umos"] = umos
        self._save_config()
        yield event.plain_result(f"已订阅：{umo}\n当前共 {len(umos)} 个会话。")

    @filter.command("zmd退订", desc="把当前会话移出版本推送（仅管理员）")
    async def zmd_unsubscribe(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员能修改推送订阅。")
            return
        umo = event.unified_msg_origin
        umos = self._notify_umos()
        if umo not in umos:
            yield event.plain_result(f"本会话不在订阅列表里：\n{umo}")
            return
        umos = [item for item in umos if item != umo]
        self.config["notify_umos"] = umos
        self._save_config()
        yield event.plain_result(f"已退订。\n当前共 {len(umos)} 个会话。")

    @filter.command("zmd推送状态", desc="查看版本推送的订阅与最近检查时间")
    async def zmd_push_status(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员能查看推送状态。")
            return
        state = notify.load_state(self._notify_state_path())
        umos = self._notify_umos()
        interval = max(5, int(self.config.get("notify_interval", 60) or 60))
        lines = [
            "版本推送",
            f"开关：{'开' if self.config.get('notify_enable', True) else '关'}",
            f"间隔：{interval} 分钟",
            f"预下载通知：{'开' if self.config.get('notify_pre_download', True) else '关'}",
            f"订阅：{len(umos)} 个",
        ]
        if umos:
            lines.extend(f"  {umo}" for umo in umos)
        else:
            lines.append("  （空）")
        lines.append(
            f"已记录：正式 {state.get('main') or '-'} / 预下载 {state.get('pre') or '-'}"
        )
        lines.append(f"最近检查：{state.get('checked_at') or '-'}")
        yield event.plain_result("\n".join(lines))
