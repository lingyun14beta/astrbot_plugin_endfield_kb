"""终末地中文知识库插件。

把本地 FTS5 索引（官方剧情原文 + 档案 + 整理 Wiki）注册为一个 LLM 工具，
模型在需要时主动调用；检索编排、更新、推送、语义这些细节放在 core/ 下。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import field
from pathlib import Path
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from . import semantic
from .core import versioning, webapi
from .core.pusher import Pusher
from .core.retrieval import Retriever
from .core.t2i import CardRenderer
from .core.updater import Updater
from .endfield_kb import SCHEMA_VERSION
from .index_store import default_index_dir, read_current, resolve

PLUGIN_NAME = "astrbot_plugin_endfield_kb"
PLUGIN_DIR = Path(__file__).parent

COMMAND_HELP = (
    "终末地知识库 · 指令\n"
    "  /zmd <关键词>   检索，可直接贴原文片段\n"
    "  /zmd文字 <词>   强制文本结果，不经过出图\n"
    "  /zmd            不带关键词，显示索引状态\n"
    "  /zmd状态        索引篇数、语料版本与体积\n"
    "  /zmd重载        重新扫描本地索引（管理员）\n"
    "  /zmd更新        检查并下载最新索引（管理员）\n"
    "  /zmd帮助        本条说明\n"
    "  /zmd订阅        当前会话加入版本推送（管理员）\n"
    "  /zmd退订        移出版本推送（管理员）\n"
    "  /zmd推送状态    订阅列表与最近检查时间（管理员）\n"
    "  /zmd向量        构建语义向量索引（管理员）\n"
    "  /zmd向量状态    向量索引与可用嵌入模型（管理员）"
)


@dataclass
class EndfieldSearchTool(FunctionTool[AstrAgentContext]):
    """给模型调用的检索工具。"""

    plugin: Any = None
    name: str = "endfield_kb_search"
    description: str = (
        "检索《明日方舟：终末地》的本地知识库，覆盖剧情原文、档案、"
        "角色资料与整理 Wiki。query 可以直接贴原文片段，也可以是关键词。"
        "返回带 [原文]/[整理] 标注的片段与出处。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索词或原文片段；口语描述也可以",
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
        self.updater = Updater(self)
        self.retriever = Retriever(self)
        self.pusher = Pusher(self)
        self.t2i = CardRenderer(self)
        self._vector_task: asyncio.Task | None = None
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
        context.register_web_api(
            f"/{PLUGIN_NAME}/semantic",
            self.page_semantic,
            ["GET"],
            "语义检索状态",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/semantic",
            self.page_semantic_save,
            ["POST"],
            "切换语义检索",
        )

    # ---------------------------------------------------------------- 初始化

    def _normalize_sources(self) -> None:
        """把旧版的字符串地址迁移成列表并落盘。"""
        changed = False
        for key in ("manifest_urls", "index_urls"):
            repaired = versioning.repair_url_value(self.config.get(key))
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

    def save_config(self) -> None:
        """把运行时改动的配置写回文件。"""
        try:
            self.config.save_config()
        except Exception as exc:  # noqa: BLE001 - 保存失败不影响当前会话
            logger.warning(f"[终末地KB] 配置保存失败：{exc}")

    def vector_building(self) -> bool:
        """向量构建任务是否还在跑。"""
        return self._vector_task is not None and not self._vector_task.done()

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
            versioning.version_key(meta.get("index_version") or meta.get("release", ""))
            if db
            else ""
        )
        if db:
            current = read_current(self.index_dir) or {}
            if current.get("release_id"):
                self.local_release = versioning.version_key(str(current["release_id"]))
        if hasattr(self, "retriever"):
            self.retriever.invalidate()
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
        self.pusher.start()
        logger.info("[终末地KB] 已注册工具 endfield_kb_search")

    async def terminate(self) -> None:
        """插件卸载/重载时停掉后台任务。"""
        await self.pusher.stop()
        if self._vector_task is not None:
            self._vector_task.cancel()
            try:
                await self._vector_task
            except asyncio.CancelledError:
                pass
            finally:
                self._vector_task = None

    # ---------------------------------------------------------------- 检索

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
                f"请运行 build_index.py，或把索引文件放进 {self.index_dir} 后执行 /zmd重载。"
            )

        limit = int(top_k or self.config.get("top_k", 5))
        limit = max(1, min(limit, 10))
        hits = await self.retriever.query(query, top_k=limit)
        if not hits:
            return f"没有检索到与「{query}」相关的内容。可以换更具体的专名或原文片段再试。"
        return self.retriever.format_for_llm(hits, query)

    # ---------------------------------------------------------------- 页面接口

    async def page_stats(self):
        return await webapi.stats(self)

    async def page_search(self):
        return await webapi.search(self)

    async def page_updates(self):
        return await webapi.updates(self)

    async def page_update(self):
        return await webapi.update(self)

    async def page_upload(self):
        return await webapi.upload(self)

    async def page_import_url(self):
        return await webapi.import_url(self)

    async def page_semantic(self):
        return await webapi.semantic_state(self)

    async def page_semantic_save(self):
        return await webapi.semantic_save(self)

    # ---------------------------------------------------------------- 指令

    def _status_text(self) -> str:
        if not self.meta or not self.db_path:
            return (
                "索引未就绪。\n"
                f"目录：{self.index_dir}\n"
                "可运行 build_index.py，或把索引文件放进该目录后执行 /zmd重载。"
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
        if self.t2i.enabled() and self.db_path:
            started = time.perf_counter()
            hits = await self.retriever.query(query, top_k=self.t2i.max_hits())
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            if not hits:
                yield event.plain_result(
                    f"没有检索到与「{query}」相关的内容。可以换更具体的专名或原文片段再试。"
                )
                return
            yield event.plain_result("正在生成卡片…")
            path = await self.t2i.render(
                hits, query, elapsed_ms, event.unified_msg_origin
            )
            if path:
                yield event.image_result(path)
                return
            yield event.plain_result("出图失败，已改用文本。")
            yield event.plain_result(self.retriever.format_for_llm(hits, query))
            return
        yield event.plain_result(await self.search_text(query, 5))

    @filter.command("zmd文字", desc="检索并以文本返回，不经过出图")
    async def zmd_text(self, event: AstrMessageEvent):
        query = self._command_args(event)
        if not query:
            yield event.plain_result("用法：/zmd文字 <关键词>")
            return
        yield event.plain_result(await self.search_text(query, 5))

    @filter.command("zmd状态", desc="查看索引篇数、语料版本与磁盘占用")
    async def zmd_status(self, event: AstrMessageEvent):
        yield event.plain_result(self._status_text())

    @filter.command("zmd帮助", desc="列出全部 zmd 指令")
    async def zmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(COMMAND_HELP)

    @filter.command("zmd重载", desc="重新扫描本地索引文件（仅管理员）")
    async def zmd_reload(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员能重载索引。")
            return
        self._load_index()
        yield event.plain_result("索引已重载。\n\n" + self._status_text())

    @filter.command("zmd更新", desc="检查并安装最新索引（仅管理员）")
    async def zmd_update(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员能更新索引。")
            return
        yield event.plain_result("正在检查并下载更新…")
        try:
            result = await self.updater.apply_update()
        except Exception as exc:  # noqa: BLE001 - 网络/校验失败统一提示
            yield event.plain_result(f"更新失败：{exc}")
            return
        if result.get("up_to_date"):
            yield event.plain_result(result.get("message") or "已是最新。")
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
        yield event.plain_result(self.pusher.subscribe(event.unified_msg_origin))

    @filter.command("zmd退订", desc="把当前会话移出版本推送（仅管理员）")
    async def zmd_unsubscribe(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员能修改推送订阅。")
            return
        yield event.plain_result(self.pusher.unsubscribe(event.unified_msg_origin))

    @filter.command("zmd推送状态", desc="查看版本推送的订阅与最近检查时间")
    async def zmd_push_status(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员能查看推送状态。")
            return
        yield event.plain_result(self.pusher.status_text())

    @filter.command("zmd向量", desc="用嵌入模型构建语义索引（仅管理员）")
    async def zmd_vectors(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员能构建向量索引。")
            return
        if self.db_path is None:
            yield event.plain_result("索引还没就绪，先安装或构建索引。")
            return
        if self._vector_task is not None and not self._vector_task.done():
            yield event.plain_result("已经在构建中，完成后会通知。")
            return
        try:
            provider = semantic.pick_provider(
                self.context, str(self.retriever.semantic_conf().get("provider") or "")
            )
        except semantic.SemanticError as exc:
            yield event.plain_result(str(exc))
            return
        self._vector_task = asyncio.create_task(
            self.retriever.build_vectors(provider, event.unified_msg_origin)
        )
        yield event.plain_result(
            f"开始构建向量：{semantic.provider_label(provider)}\n"
            "只需要编码新增或有改动的文档，可能要几分钟，完成后在这里通知。"
        )

    @filter.command("zmd向量状态", desc="查看语义索引与可用的嵌入模型（仅管理员）")
    async def zmd_vector_status(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员能查看向量状态。")
            return
        conf = self.retriever.semantic_conf()
        meta = semantic.read_meta(self.retriever.semantic_dir())
        providers = semantic.list_providers(self.context)
        lines = [
            "语义检索",
            f"开关：{'开' if conf.get('enable') else '关'}",
            f"模型：{conf.get('provider') or '（第一个可用）'}",
            f"融合权重：{conf.get('weight', 100)}%",
            f"召回条数：{conf.get('candidates', 30)}",
        ]
        if meta:
            lines.append(
                f"向量索引：{meta.get('count')} 篇 / {meta.get('dim')} 维 / {meta.get('model')}"
            )
            lines.append(f"构建时间：{meta.get('built_at')}")
            if meta.get("index_version"):
                lines.append(f"对应语料：{meta.get('index_version')}")
        else:
            lines.append("向量索引：未构建（/zmd向量 开始构建）")
        if providers:
            lines.append("可用嵌入模型：")
            lines.extend(
                f"  {item['id']}（{item['model'] or '未知模型'}，{item['dim']} 维）"
                for item in providers
            )
        else:
            lines.append("可用嵌入模型：无（请先在模型提供商添加 embedding）")
        yield event.plain_result("\n".join(lines))
