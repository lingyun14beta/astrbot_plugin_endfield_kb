"""可选的语义检索：嵌入模型调用、向量存储与混合排序。

这个模块只负责“模型相关”的部分：
- 从 AstrBot 的嵌入模型里取向量
- 把全部文档编码成向量并落盘
- 查询时算余弦相似度、与 BM25 结果做 RRF 融合

主插件不直接依赖 numpy 之外的东西；numpy 缺失或模型不可用时由调用方退回 BM25。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
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
DOCS_NAME = "semantic_docs.json"
META_NAME = "semantic_meta.json"
PARTIAL_DIR = "semantic_partial"

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


def _doc_hash(text: str) -> str:
    """文档内容指纹，用来判断哪些文档需要重新编码。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _load_existing(
    out_dir: Path,
) -> tuple[list[str], Any, dict[str, str], int]:
    """读取上次构建的 id、向量与指纹，用于增量复用。"""
    docs_path = out_dir / DOCS_NAME
    vector_path = out_dir / VECTOR_NAME
    if np is None or not docs_path.is_file() or not vector_path.is_file():
        return [], None, {}, 0
    try:
        docs = json.loads(docs_path.read_text(encoding="utf-8"))
        matrix = np.load(vector_path).astype(np.float32)
    except (OSError, ValueError):
        return [], None, {}, 0
    ids = [str(item.get("id")) for item in docs]
    hashes = {str(item.get("id")): str(item.get("hash") or "") for item in docs}
    return ids, matrix, hashes, int(matrix.shape[1])


def _cleanup_tmp(out_dir: Path) -> None:
    """删除上次中断留下的临时文件。"""
    for name in (VECTOR_NAME, DOCS_NAME, META_NAME):
        try:
            (out_dir / f"{name}.tmp").unlink()
        except OSError:
            pass


def _load_partial(
    out_dir: Path, model: str, dim: int
) -> tuple[dict[str, list[float]], dict[str, str], int]:
    """读取上次中断留下的分片进度。"""
    part_dir = out_dir / PARTIAL_DIR
    if np is None or not part_dir.is_dir():
        return {}, {}, 0
    vectors: dict[str, list[float]] = {}
    hashes: dict[str, str] = {}
    for path in sorted(part_dir.glob("part-*.npz")):
        try:
            with np.load(path, allow_pickle=False) as data:
                if str(data["model"].item()) != model:
                    continue
                part_dim = int(data["dim"].item())
                if dim > 0 and part_dim != dim:
                    continue
                ids = [str(item) for item in data["ids"].tolist()]
                part_hashes = [str(item) for item in data["hashes"].tolist()]
                matrix = data["vectors"].astype(np.float32)
        except (OSError, ValueError, KeyError):
            continue
        for index, doc_id in enumerate(ids):
            vectors[doc_id] = matrix[index]
            hashes[doc_id] = part_hashes[index]
        dim = part_dim
    return vectors, hashes, dim


def _save_partial(
    out_dir: Path,
    model: str,
    dim: int,
    ids: list[str],
    hashes: list[str],
    vectors: list[list[float]],
    dtype: str = "float16",
) -> None:
    """把一小段编码结果原子写入分片目录，供中断后续跑。"""
    if np is None or not ids:
        return
    part_dir = out_dir / PARTIAL_DIR
    part_dir.mkdir(parents=True, exist_ok=True)
    seq = len(list(part_dir.glob("part-*.npz"))) + 1
    target = part_dir / f"part-{seq:06d}.npz"
    tmp = part_dir / f"part-{seq:06d}.npz.tmp"
    np_dtype = np.float32 if dtype == "float32" else np.float16
    with tmp.open("wb") as handle:
        np.savez(
            handle,
            ids=np.array(ids, dtype=str),
            hashes=np.array(hashes, dtype=str),
            vectors=np.array(vectors, dtype=np_dtype),
            model=np.array(model),
            dim=np.array(dim),
        )
    tmp.replace(target)


def _cleanup_partial(out_dir: Path) -> None:
    """清理分片进度目录。"""
    part_dir = out_dir / PARTIAL_DIR
    if not part_dir.is_dir():
        return
    for path in part_dir.glob("*"):
        try:
            path.unlink()
        except OSError:
            pass
    try:
        part_dir.rmdir()
    except OSError:
        pass


REQUEST_BATCH = 16
REQUEST_TIMEOUT = 120
MAX_RETRIES = 5


class _RateLimiter:
    """按每分钟请求数把调用均匀排开。"""

    def __init__(self, rpm: int) -> None:
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        """等到下一个可用时隙；rpm<=0 时直接返回。"""
        if self.interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(now, self._next) + self.interval


def _is_rate_limit(exc: Exception) -> bool:
    """判断异常是否为 429/限流。"""
    for attr in ("status_code", "code"):
        if getattr(exc, attr, None) == 429:
            return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    return (
        "429" in text
        or "too many requests" in text
        or "rate limit" in text
        or "rate_limit" in text
    )


def _is_quota_error(exc: Exception) -> bool:
    """判断异常是否为余额不足。"""
    text = str(exc).lower()
    return "insufficient" in text and ("quota" in text or "balance" in text)


async def _embed_request(
    provider: Any,
    batch: list[str],
    limiter: _RateLimiter,
    error_log: LogFn,
    start: int,
) -> list[list[float]]:
    """编码一个请求批次；遇到 429 指数退避重试。"""
    attempt = 0
    while True:
        await limiter.wait()
        try:
            part = await asyncio.wait_for(
                provider.get_embeddings(batch), timeout=REQUEST_TIMEOUT
            )
            return part
        except asyncio.TimeoutError as exc:
            raise SemanticError(
                f"编码第 {start + 1}–{start + len(batch)} 篇超时"
                f"（{REQUEST_TIMEOUT} 秒）"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 统一包装成模块异常
            if _is_quota_error(exc):
                raise SemanticError(
                    "嵌入模型账户余额不足（403 insufficient quota），"
                    "充值后可从检查点继续"
                ) from exc
            if not _is_rate_limit(exc):
                raise SemanticError(
                    f"编码第 {start + 1}–{start + len(batch)} 篇失败：{exc}"
                ) from exc
            if attempt >= MAX_RETRIES:
                raise SemanticError(
                    f"编码第 {start + 1}–{start + len(batch)} 篇持续触发限流（429），"
                    f"已重试 {MAX_RETRIES} 次"
                ) from exc
            attempt += 1
            delay = min(60.0, 2.0**attempt) + random.uniform(0, 1)
            error_log(
                f"嵌入接口 429，第 {attempt}/{MAX_RETRIES} 次重试，"
                f"等待 {delay:.1f} 秒"
            )
            await asyncio.sleep(delay)


async def _embed_batches(
    provider: Any,
    texts: list[str],
    log: LogFn,
    dim: int,
    rpm: int = 0,
    error_log: LogFn | None = None,
    checkpoint: Callable[[dict[int, list[float]]], None] | None = None,
) -> tuple[dict[int, list[float]], int]:
    """把待编码文本按 16 篇一个请求编码，支持 RPM 限速与 429 退避。"""
    vectors: dict[int, list[float]] = {}
    total = len(texts)
    limiter = _RateLimiter(rpm)
    report = error_log or log
    last_checkpoint = 0
    for start in range(0, total, REQUEST_BATCH):
        batch = texts[start : start + REQUEST_BATCH]
        part = await _embed_request(provider, batch, limiter, report, start)
        if len(part) != len(batch):
            raise SemanticError("模型返回的向量数量与输入不一致")
        if dim <= 0:
            dim = len(part[0])
        for offset, vector in enumerate(part):
            if len(vector) != dim:
                raise SemanticError("模型返回的向量维度不一致")
            vectors[start + offset] = vector
        done = min(start + REQUEST_BATCH, total)
        if done % 256 == 0 or done == total:
            if checkpoint is not None:
                checkpoint(
                    {
                        index: vectors[index]
                        for index in range(last_checkpoint, done)
                    }
                )
            last_checkpoint = done
            log(f"已编码 {done}/{total}")
    return vectors, dim


async def build(
    db_path: Path,
    out_dir: Path,
    provider: Any,
    log: LogFn = print,
    incremental: bool = True,
    index_version: str = "",
    rpm: int = 0,
    error_log: LogFn | None = None,
    checkpoint_dtype: str = "float16",
) -> dict[str, Any]:
    """把索引里的文档编码成向量并落盘；默认只编码新增或内容变化的文档。

    Args:
        db_path: 索引 sqlite 路径。
        out_dir: 向量文件输出目录，通常与索引同目录。
        provider: 嵌入模型实例。
        log: 进度输出函数。
        incremental: 是否复用上次的向量；模型或维度变化时自动全量。
        index_version: 这份向量对应的语料版本，用于识别索引更新后的失配。
        rpm: 每分钟最多请求数；0 表示不限制。
        error_log: 限流等错误日志；默认复用 log。
        checkpoint_dtype: 检查点精度，float16 或 float32。

    Returns:
        含 count、dim、model、built_at、index_version、reused、added 的元信息。

    Raises:
        SemanticError: numpy 缺失、索引为空或模型调用失败。
    """
    if np is None:
        raise SemanticError("当前环境缺少 numpy，无法构建向量索引")

    _cleanup_tmp(out_dir)
    rows = _read_docs(db_path)
    if not rows:
        raise SemanticError("索引里没有文档，无法构建向量")

    ids = [str(row[0]) for row in rows]
    texts = [_doc_text(row[1], row[2]) for row in rows]
    hashes = [_doc_hash(text) for text in texts]

    old_ids, old_matrix, old_hashes, old_dim = _load_existing(out_dir)
    old_meta = read_meta(out_dir) or {}
    old_model = str(old_meta.get("model") or "")
    current_model = provider_label(provider)
    dim = _dim_of(provider)

    known: dict[str, list[float]] = {}
    known_hashes: dict[str, str] = {}
    reuse_base = (
        incremental
        and old_matrix is not None
        and bool(old_model)
        and old_model == current_model
        and (dim <= 0 or dim == old_dim)
    )
    if reuse_base:
        dim = old_dim if dim <= 0 else dim
        for position, doc_id in enumerate(old_ids):
            if position < len(old_matrix):
                known[doc_id] = old_matrix[position]
        known_hashes.update(old_hashes)
        log(f"增量构建：复用 {len(known)} 条旧向量")
    elif incremental and old_matrix is not None:
        if not old_model:
            log("旧向量缺少模型信息，改为全量构建")
        elif old_model != current_model:
            log(f"模型由「{old_model}」变为「{current_model}」，改为全量构建")
        else:
            log(f"向量维度从 {old_dim} 变为 {dim}，改为全量构建")

    if incremental:
        partial_vectors, partial_hashes, partial_dim = _load_partial(
            out_dir, current_model, dim
        )
        if partial_vectors:
            if dim <= 0 and partial_dim > 0:
                dim = partial_dim
            known.update(partial_vectors)
            known_hashes.update(partial_hashes)
            log(f"发现 {len(partial_vectors)} 条未完成进度，继续构建")

    need = [
        index
        for index, (doc_id, digest) in enumerate(zip(ids, hashes))
        if known_hashes.get(doc_id) != digest
    ]

    new_vectors: dict[int, list[float]] = {}
    if need:

        def checkpoint(chunk: dict[int, list[float]]) -> None:
            nonlocal dim
            positions = sorted(chunk)
            if not positions:
                return
            part_vectors = [chunk[position] for position in positions]
            if dim <= 0:
                dim = len(part_vectors[0])
            try:
                _save_partial(
                    out_dir,
                    current_model,
                    dim,
                    [ids[need[position]] for position in positions],
                    [hashes[need[position]] for position in positions],
                    part_vectors,
                    checkpoint_dtype,
                )
            except OSError as exc:
                (error_log or log)(f"检查点写入失败：{exc}")

        new_vectors, dim = await _embed_batches(
            provider,
            [texts[index] for index in need],
            log,
            dim,
            rpm=rpm,
            error_log=error_log,
            checkpoint=checkpoint,
        )
        new_vectors = {need[offset]: vec for offset, vec in new_vectors.items()}
    elif not known:
        raise SemanticError("没有可编码的文档")

    n = len(ids)
    if dim <= 0:
        raise SemanticError("无法确定向量维度")

    matrix = np.zeros((n, dim), dtype=np.float32)
    for index, doc_id in enumerate(ids):
        vector = new_vectors.get(index)
        if vector is None and known_hashes.get(doc_id) == hashes[index]:
            vector = known.get(doc_id)
        if vector is None:
            raise SemanticError(f"缺少文档 {doc_id} 的向量")
        matrix[index] = vector

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.clip(norms, 1e-8, None)

    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "count": n,
        "dim": int(matrix.shape[1]),
        "model": provider_label(provider),
        "index_version": index_version,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reused": n - len(need),
        "added": len(need),
    }

    # 三个文件都先写临时文件再统一替换，失败时清掉残留
    try:
        vector_tmp = out_dir / f"{VECTOR_NAME}.tmp"
        with vector_tmp.open("wb") as handle:
            np.save(handle, matrix.astype(np.float16))

        docs = [{"id": doc_id, "hash": digest} for doc_id, digest in zip(ids, hashes)]
        docs_tmp = out_dir / f"{DOCS_NAME}.tmp"
        docs_tmp.write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")

        meta_tmp = out_dir / f"{META_NAME}.tmp"
        meta_tmp.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        vector_tmp.replace(out_dir / VECTOR_NAME)
        docs_tmp.replace(out_dir / DOCS_NAME)
        meta_tmp.replace(out_dir / META_NAME)
        _cleanup_partial(out_dir)
    except BaseException:
        _cleanup_tmp(out_dir)
        raise
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
        docs_path = out_dir / DOCS_NAME
        ids_path = out_dir / IDS_NAME
        if not vector_path.is_file():
            raise SemanticError("向量索引尚未构建，先执行 /zmd向量")
        if docs_path.is_file():
            docs = json.loads(docs_path.read_text(encoding="utf-8"))
            self.ids = [str(item.get("id")) for item in docs]
        elif ids_path.is_file():
            # 兼容上一版只写 id 列表的索引
            self.ids = json.loads(ids_path.read_text(encoding="utf-8"))
        else:
            raise SemanticError("向量索引缺少文档列表，先执行 /zmd向量")
        self.matrix = np.load(vector_path).astype(np.float32)
        if len(self.ids) != self.matrix.shape[0]:
            raise SemanticError("向量文件与 id 列表数量不一致，建议重新构建")
        meta = read_meta(out_dir) or {}
        if meta.get("count") and int(meta["count"]) != len(self.ids):
            raise SemanticError("元信息与向量数量不一致，建议重新构建")
        if meta.get("dim") and int(meta["dim"]) != int(self.matrix.shape[1]):
            raise SemanticError("元信息与向量维度不一致，建议重新构建")
        self.model = str(meta.get("model") or "")
        self.index_version = str(meta.get("index_version") or "")

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
