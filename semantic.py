"""可选的语义检索：嵌入模型调用、向量存储与混合排序。

这个模块只负责“模型相关”的部分：
- 从 AstrBot 的嵌入模型里取向量
- 把全部文档编码成向量并落盘
- 查询时算余弦相似度、与 BM25 结果做 RRF 融合

主插件不直接依赖 numpy 之外的东西；numpy 缺失或模型不可用时由调用方退回 BM25。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

try:
    import numpy as np
except ImportError:  # pragma: no cover - AstrBot 运行环境一定带 numpy
    np = None

VECTOR_NAME = "semantic_vectors.npy"
IDS_NAME = "semantic_ids.json"
META_NAME = "semantic_meta.json"

# 单条文档送进模型的最大字符数；语料平均 210 字，长档案截断即可
MAX_CHARS = 800

LogFn = Callable[[str], None]


class SemanticError(RuntimeError):
    """语义检索不可用或构建失败。"""


def numpy_ready() -> bool:
    """numpy 是否可用。"""
    return np is not None


def _dim_of(provider: Any) -> int:
    """尽量读出模型的向量维度，读不到返回 0。"""
    try:
        return int(provider.get_dim())
    except Exception:  # noqa: BLE001 - 个别 provider 未实现
        return 0


def provider_label(provider: Any) -> str:
    """给嵌入模型起一个可读名字，用于日志和状态输出。"""
    config = getattr(provider, "provider_config", {}) or {}
    name = config.get("id") or getattr(provider, "provider_id", "") or "embedding"
    model = config.get("embedding_model") or config.get("model") or ""
    return f"{name}（{model}）" if model else str(name)


def list_providers(context: Any) -> list[dict[str, Any]]:
    """列出当前可用的嵌入模型。"""
    result: list[dict[str, Any]] = []
    for provider in context.get_all_embedding_providers():
        config = getattr(provider, "provider_config", {}) or {}
        result.append(
            {
                "id": str(config.get("id") or getattr(provider, "provider_id", "")),
                "model": str(config.get("embedding_model") or config.get("model") or ""),
                "dim": _dim_of(provider),
            }
        )
    return result


def pick_provider(context: Any, provider_id: str = "") -> Any:
    """按 id 取嵌入模型，留空时取第一个。

    Args:
        context: 插件上下文。
        provider_id: 配置里填的模型 id。

    Returns:
        嵌入模型实例。

    Raises:
        SemanticError: 没有可用模型，或指定 id 不存在。
    """
    providers = list(context.get_all_embedding_providers())
    if not providers:
        raise SemanticError("没有可用的嵌入模型，请先在「模型提供商」里添加 embedding 类型")
    if not provider_id:
        return providers[0]
    for provider in providers:
        config = getattr(provider, "provider_config", {}) or {}
        if str(config.get("id") or getattr(provider, "provider_id", "")) == provider_id:
            return provider
    raise SemanticError(f"找不到嵌入模型 {provider_id}")


def _doc_text(title: str, body: str) -> str:
    """拼出送进模型的文本：标题 + 正文前若干字。"""
    text = f"{title}\n{body}".strip()
    return text[:MAX_CHARS]


async def build(
    db_path: Path,
    out_dir: Path,
    provider: Any,
    log: LogFn = print,
) -> dict[str, Any]:
    """把索引里的全部文档编码成向量并落盘。

    Args:
        db_path: 索引 sqlite 路径。
        out_dir: 向量文件输出目录，通常与索引同目录。
        provider: 嵌入模型实例。
        log: 进度输出函数。

    Returns:
        含 count、dim、model、built_at 的元信息。

    Raises:
        SemanticError: numpy 缺失、索引为空或模型调用失败。
    """
    if np is None:
        raise SemanticError("当前环境缺少 numpy，无法构建向量索引")

    rows = _read_docs(db_path)
    if not rows:
        raise SemanticError("索引里没有文档，无法构建向量")

    ids: list[str] = []
    texts: list[str] = []
    for doc_id, title, body in rows:
        ids.append(str(doc_id))
        texts.append(_doc_text(title, body))

    vectors: list[list[float]] = []
    chunk = 256
    total = len(texts)
    for start in range(0, total, chunk):
        batch = texts[start : start + chunk]
        try:
            part = await asyncio.wait_for(
                provider.get_embeddings_batch(batch, batch_size=16, tasks_limit=2),
                timeout=600,
            )
        except asyncio.TimeoutError as exc:
            raise SemanticError(
                f"编码第 {start + 1}–{start + len(batch)} 篇超时（600 秒）"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 统一包装成模块异常
            raise SemanticError(f"编码第 {start + 1}–{start + len(batch)} 篇失败：{exc}") from exc
        if len(part) != len(batch):
            raise SemanticError("模型返回的向量数量与输入不一致")
        vectors.extend(part)
        log(f"已编码 {min(start + chunk, total)}/{total}")

    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.clip(norms, 1e-8, None)

    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "count": total,
        "dim": int(matrix.shape[1]),
        "model": provider_label(provider),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 三个文件都先写临时文件再替换，避免留下新旧混搭的半成品
    vector_tmp = out_dir / f"{VECTOR_NAME}.tmp"
    with vector_tmp.open("wb") as handle:
        np.save(handle, matrix.astype(np.float16))
    vector_tmp.replace(out_dir / VECTOR_NAME)

    ids_tmp = out_dir / f"{IDS_NAME}.tmp"
    ids_tmp.write_text(json.dumps(ids, ensure_ascii=False), encoding="utf-8")
    ids_tmp.replace(out_dir / IDS_NAME)

    meta_tmp = out_dir / f"{META_NAME}.tmp"
    meta_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    meta_tmp.replace(out_dir / META_NAME)
    return meta


def _read_docs(db_path: Path) -> list[tuple[str, str, str]]:
    """读出 doc_id、title、body。"""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute("SELECT doc_id, title, body FROM docs").fetchall()
    finally:
        con.close()


def read_meta(out_dir: Path) -> dict[str, Any] | None:
    """读取向量索引的元信息，缺失返回 None。"""
    path = out_dir / META_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class SemanticIndex:
    """内存里的向量索引，负责余弦检索。"""

    def __init__(self, out_dir: Path) -> None:
        if np is None:
            raise SemanticError("当前环境缺少 numpy")
        vector_path = out_dir / VECTOR_NAME
        ids_path = out_dir / IDS_NAME
        if not vector_path.is_file() or not ids_path.is_file():
            raise SemanticError("向量索引尚未构建，先执行 /zmd向量")
        self.ids = json.loads(ids_path.read_text(encoding="utf-8"))
        self.matrix = np.load(vector_path).astype(np.float32)
        if len(self.ids) != self.matrix.shape[0]:
            raise SemanticError("向量文件与 id 列表数量不一致，建议重新构建")

    @property
    def size(self) -> int:
        """文档数量。"""
        return len(self.ids)

    @property
    def dim(self) -> int:
        """向量维度。"""
        return int(self.matrix.shape[1])

    def search(self, vector: list[float], top_k: int = 30) -> list[str]:
        """按余弦相似度返回 doc_id 列表。"""
        query = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if norm <= 0 or query.shape[0] != self.dim:
            raise SemanticError("查询向量为空或维度不匹配，建议重新构建向量")
        query = query / norm
        scores = self.matrix @ query
        top_k = max(1, min(top_k, scores.shape[0]))
        index = np.argpartition(-scores, top_k - 1)[:top_k]
        index = index[np.argsort(-scores[index])]
        return [self.ids[int(i)] for i in index]


async def encode_query(provider: Any, query: str) -> list[float]:
    """把查询编码成向量。"""
    try:
        return await provider.get_embedding(query)
    except Exception as exc:  # noqa: BLE001
        raise SemanticError(f"查询编码失败：{exc}") from exc


def fuse(
    bm25_ids: list[str],
    vector_ids: list[str],
    weight: float = 0.5,
    k: int = 60,
) -> list[str]:
    """用 RRF 融合两路结果。

    Args:
        bm25_ids: BM25 的 doc_id，按相关度降序。
        vector_ids: 向量检索的 doc_id，按相似度降序。
        weight: 向量那一路的权重，1.0 表示与 BM25 等权。
        k: RRF 平滑常数。

    Returns:
        融合后的 doc_id，按分数降序。
    """
    scores: dict[str, float] = {}
    for rank, doc_id in enumerate(bm25_ids):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, doc_id in enumerate(vector_ids):
        scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank + 1)
    return sorted(scores, key=lambda item: scores[item], reverse=True)
