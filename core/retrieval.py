"""检索编排：关键词改写、BM25 + 语义融合、给模型阅读的格式化。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from .. import semantic
from ..endfield_kb import Hit, fetch_hits, search

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


class Retriever:
    """把配置、索引与语义层拼成一次检索。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._semantic_cache: semantic.SemanticIndex | None = None
        self._semantic_warned = False

    @property
    def config(self) -> dict[str, Any]:
        """插件配置。"""
        return self.plugin.config

    @property
    def db_path(self) -> Path | None:
        """当前索引文件。"""
        return self.plugin.db_path

    @property
    def index_dir(self) -> Path:
        """索引目录。"""
        return self.plugin.index_dir

    def invalidate(self) -> None:
        """索引或向量变动后丢弃缓存。"""
        self._semantic_cache = None

    async def rewrite(self, query: str, umo: str) -> str:
        """用对话模型把描述压成检索关键词。

        只在调用方明确要求时执行，不做长度判断：勾了就一定问模型。

        Args:
            query: 用户的原始描述。
            umo: 会话来源，用于选择该会话偏好的模型。

        Returns:
            空格分隔的关键词；失败时返回空串。
        """
        context = self.plugin.context
        provider = None
        provider_id = str(self.config.get("rewrite_provider") or "").strip()
        if provider_id:
            provider = context.get_provider_by_id(provider_id)
        if provider is None:
            try:
                provider = await context.get_using_provider_async(umo)
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
        tokens = [item for item in re.split(r"[\s,，、;；|]+", text) if item]
        return " ".join(tokens[:10])

    # ---------------------------------------------------------------- 语义

    def semantic_conf(self) -> dict[str, Any]:
        """读取语义检索配置。"""
        group = self.config.get("semantic_group")
        return group if isinstance(group, dict) else {}

    def semantic_dir(self) -> Path:
        """向量文件目录，跟随索引目录。"""
        return self.index_dir / "semantic"

    def semantic_enabled(self) -> bool:
        """语义检索是否启用（还要有索引可用）。"""
        return bool(self.semantic_conf().get("enable")) and self.db_path is not None

    def semantic_index(self) -> semantic.SemanticIndex | None:
        """惰性加载向量索引，失败返回 None。"""
        if self._semantic_cache is not None:
            return self._semantic_cache
        try:
            self._semantic_cache = semantic.SemanticIndex(self.semantic_dir())
        except semantic.SemanticError as exc:
            logger.debug(f"[终末地KB] 语义索引不可用：{exc}")
            return None
        return self._semantic_cache

    async def semantic_ids(self, query: str) -> list[str]:
        """向量召回；任何失败都退回空表，调用方只用 BM25。"""
        if not self.semantic_enabled():
            return []
        index = self.semantic_index()
        if index is None:
            return []
        conf = self.semantic_conf()
        try:
            provider = semantic.pick_provider(
                self.plugin.context, str(conf.get("provider") or "")
            )
            vector = await asyncio.wait_for(
                semantic.encode_query(provider, query), timeout=8
            )
            limit = max(1, min(int(conf.get("candidates", 30) or 30), 100))
            return index.search(vector, limit)
        except Exception as exc:  # noqa: BLE001 - 语义只是增强，失败不影响检索
            if self._semantic_warned:
                logger.debug(f"[终末地KB] 语义召回失败，改用 BM25：{exc}")
            else:
                self._semantic_warned = True
                logger.warning(f"[终末地KB] 语义召回失败，改用 BM25：{exc}")
            return []

    def fuse(self, hits: list[Hit], vector_ids: list[str], limit: int) -> list[Hit]:
        """把 BM25 结果与向量结果按 RRF 融合。"""
        if not vector_ids:
            return hits
        conf = self.semantic_conf()
        weight = max(0, min(int(conf.get("weight", 100) or 100), 100)) / 100
        fused = semantic.fuse([hit.doc_id for hit in hits], vector_ids, weight=weight)
        known = {hit.doc_id: hit for hit in hits}
        missing = [doc_id for doc_id in fused[:limit] if doc_id not in known]
        if missing and self.db_path is not None:
            known.update(fetch_hits(self.db_path, missing))
        ordered = [known[doc_id] for doc_id in fused[:limit] if doc_id in known]
        return ordered or hits

    # ---------------------------------------------------------------- 向量构建

    async def build_vectors(self, provider: Any, umo: str) -> None:
        """后台构建向量索引，完成后给发起者发一条消息。"""
        if self.db_path is None:
            text = "向量构建失败：索引尚未就绪"
        else:
            try:
                meta = await semantic.build(
                    self.db_path,
                    self.semantic_dir(),
                    provider,
                    log=lambda item: logger.info(f"[终末地KB] {item}"),
                )
                self._semantic_cache = None
                text = (
                    f"向量索引完成：{meta['count']} 篇"
                    f"（复用 {meta.get('reused', 0)}，新增 {meta.get('added', 0)}）\n"
                    f"{meta['dim']} 维 · {meta['model']}"
                )
            except Exception as exc:  # noqa: BLE001 - 构建失败要回告用户
                text = f"向量构建失败：{exc}"
                logger.error(f"[终末地KB] {text}")
        try:
            await self.plugin.context.send_message(umo, MessageChain([Plain(text=text)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[终末地KB] 向量结果通知发送失败：{exc}")

    # ---------------------------------------------------------------- 检索

    async def query(
        self,
        query: str,
        top_k: int,
        kinds: list[str] | None = None,
        score_ratio: float | None = None,
    ) -> list[Hit]:
        """先 BM25，再按需融合语义结果。"""
        if self.db_path is None:
            return []
        ratio = (
            score_ratio
            if score_ratio is not None
            else float(self.config.get("score_ratio", 45)) / 100
        )
        hits = search(self.db_path, query, top_k=top_k, score_ratio=ratio, kinds=kinds)
        if self.semantic_enabled():
            hits = self.fuse(hits, await self.semantic_ids(query), top_k)
        return hits

    def format_for_llm(self, hits: list[Hit], query: str) -> str:
        """把命中整理成模型可读的文本。"""
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
