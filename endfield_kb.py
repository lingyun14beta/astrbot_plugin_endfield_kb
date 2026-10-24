"""《明日方舟：终末地》中文语料的本地 FTS5 索引与 BM25 检索。

本模块不依赖 AstrBot，可单独导入测试：

    python -c "from endfield_kb import search_text; print(search_text('围陋村植禾'))"
"""

from __future__ import annotations

import gzip
import json
import math
import re
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import jieba

TERM = re.compile(r"\w", re.UNICODE)

# 索引结构版本：增删列、改语义时加一，插件据此拒绝旧库
SCHEMA_VERSION = 2

# 整理资料里带 <相关角色>、<image>…</> 这类标记，输出前清掉
TAG_RE = re.compile(r"<[^<>\n]{1,120}>")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
BULLET_RE = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
MULTI_NEWLINE_RE = re.compile(r"\n{3,}")

# 正文内嵌的资料引用（如 dlg_e12m2_2、black_a1m9_2、Map02 Lv010·_env、/16003/16004）
REF_RE = re.compile(
    r"(?:dlg|text|radio|sns|remotecomm|misc|mail|black)_[A-Za-z0-9_]+"
    r"|Map\d{2}\s*Lv\d+\s*·\s*_env"
    r"|map\d{2}_lv\d+_env"
    r"|(?:/\d{5,})+"
)

# 纯交代资料来源的段落（“需要单独说明的是 …”），删掉不影响内容本身
SCOPE_RE = re.compile(r"^\s*需要单独说明的是")

PAREN_RE = re.compile(r"[（(][^（）()]{0,200}?[）)]")

# 《Map02 Lv010·_env》这类以资料 id 当作品名的地方，换成中性说法
BOOK_REF_ZHONG_RE = re.compile(
    r"《\s*(?:" + REF_RE.pattern + r")\s*》\s*中"
)
BOOK_REF_RE = re.compile(r"《\s*(?:" + REF_RE.pattern + r")\s*》")

# 以引用开头的句子（“Map02 Lv010·_env 中未确认…”）属于出处说明
REF_LEAD_RE = re.compile(r"^\s*(?:" + REF_RE.pattern + r")\s*[中里内]")

SENTENCE_SPLIT_RE = re.compile(r"(?<=[。；！？])")

PUNCT_CJK_SPACE_RE = re.compile(r"(?<=[。；！？，、：])[ \t]+")

CJK_SPACE_RE = re.compile(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])")
PUNCT_SPACE_RE = re.compile(r"[ \t]+([，。；：、！？])")
EMPTY_PAREN_RE = re.compile(r"[（(][ \t]*[）)]")

# 说话人里的 {场景代号} 注解，展示时去掉
SPEAKER_TAG_RE = re.compile(r"\{[^}]*\}")

# 只过滤掉高频虚词，保留“是/在”等可能出现在原句里的词会有噪声，但宁可多召回
STOPWORDS = set("的了着过又也很就都而与其之乎者也")

KIND_LABELS = {
    "original_story": "原文",
    "archive": "档案",
    "character_profile": "角色档案",
    "character_voice": "角色语音",
    "knowledge": "整理",
    "character_wiki": "整理",
    "story_wiki": "整理",
    "character_activity_wiki": "整理",
    "reference": "整理",
}

# 终末地语料包内的分片目录
SOURCE_PACKS = ("endfield_official_game", "endfield_reviewed_knowledge")

SCHEMA = """
CREATE TABLE docs (
    id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    coll TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    chars INTEGER NOT NULL DEFAULT 0,
    refs TEXT NOT NULL DEFAULT ''
);
CREATE VIRTUAL TABLE fts USING fts5(
    title,
    tokens,
    doc_id UNINDEXED,
    coll UNINDEXED,
    kind UNINDEXED,
    tokenize = 'unicode61'
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


@dataclass
class Hit:
    """单条检索结果。"""

    doc_id: str
    title: str
    coll: str
    kind: str
    body: str
    score: float
    tokens: str = ""
    refs: str = ""

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, "资料")

    @property
    def ref_list(self) -> list[str]:
        return [ref for ref in self.refs.split("\n") if ref]


def tokenize(text: str) -> list[str]:
    """用 jieba 切词并过滤标点与虚词。

    Args:
        text: 待切分的文本。

    Returns:
        小写化后的词列表。
    """
    tokens: list[str] = []
    for token in jieba.cut(text or ""):
        token = token.strip().lower()
        if not token or token in STOPWORDS or not TERM.search(token):
            continue
        tokens.append(token)
    return tokens


def to_fts_query(text: str) -> str:
    """把自然语言转成 FTS5 的 OR 查询串。

    Args:
        text: 用户查询或改写后的关键词串。

    Returns:
        FTS5 MATCH 可用的查询表达式。
    """
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokenize(text))


def extract_refs(text: str) -> list[str]:
    """按出现顺序收集正文里的资料引用并去重。

    Args:
        text: 未清洗的原始正文。

    Returns:
        引用串列表，供详情面板展示出处。
    """
    refs: list[str] = []
    for match in REF_RE.finditer(text or ""):
        token = match.group(0).strip()
        if token and token not in refs:
            refs.append(token)
    return refs


def strip_refs(text: str) -> str:
    """去掉正文里的内嵌资料引用，保留内容本身。

    整理资料习惯在句子后面挂 ``（dlg_e12m2_2）`` 这类出处，或在段尾用整段
    交代“该材料/本词条”的范围，这些对读者是噪声，也不参与叙事。

    Args:
        text: 清洗过标记的正文。

    Returns:
        去掉引用串和出处说明段后的正文。
    """
    # 整理资料的“资料来源说明”通常独占一行，整行删掉比抠字更安全
    lines = [
        line
        for line in text.split("\n")
        if not (REF_RE.search(line) and SCOPE_RE.search(line))
    ]
    text = "\n".join(lines)

    def drop_paren(match: re.Match[str]) -> str:
        inner = match.group(0)[1:-1]
        if not REF_RE.search(inner):
            return match.group(0)
        rest = REF_RE.sub("", inner).strip(" \t、，,；;")
        return "" if len(rest) <= 6 else match.group(0)

    cleaned_lines: list[str] = []
    for line in text.split("\n"):
        # 整行都是“资料来源说明”的，整行删掉
        if SCOPE_RE.match(line) and REF_RE.search(line):
            continue
        # 句首就是某个资料 id 的句子（“xxx 中未确认…”），属于出处说明
        sentences = [
            sentence
            for sentence in SENTENCE_SPLIT_RE.split(line)
            if sentence and not REF_LEAD_RE.match(sentence)
        ]
        current = "".join(sentences)
        current = BOOK_REF_ZHONG_RE.sub("该材料中", current)
        current = BOOK_REF_RE.sub("该材料", current)
        current = PAREN_RE.sub(drop_paren, current)
        current = REF_RE.sub("", current)
        if current.strip():
            cleaned_lines.append(current)

    text = "\n".join(cleaned_lines)
    text = PUNCT_CJK_SPACE_RE.sub("", text)
    text = CJK_SPACE_RE.sub("", text)
    text = PUNCT_SPACE_RE.sub(r"\1", text)
    text = EMPTY_PAREN_RE.sub("", text)
    text = re.sub(r"([，、])\s*([。；])", r"\2", text)
    return text


def clean_body(text: str) -> str:
    """去掉语料里的富文本标记与内嵌引用，保留可读正文。

    Args:
        text: 原始正文（可能含 <相关角色>、<image>、Markdown 标题等）。

    Returns:
        清洗后的纯文本。
    """
    text = TAG_RE.sub("", text)
    text = HEADING_RE.sub("", text)
    text = BOLD_RE.sub(r"\1", text)
    text = BULLET_RE.sub("· ", text)
    text = MULTI_NEWLINE_RE.sub("\n\n", text)
    text = strip_refs(text)
    return text.strip()


def iter_documents(pack_dir: Path) -> Iterator[tuple[dict, str]]:
    """遍历一个语料包分片目录，产出 (document 元数据, 正文)。

    对话行会带上说话人（``说话人：台词``），旁白与黑屏文本没有说话人。
    """
    for shard in sorted((pack_dir / "shards").glob("*.jsonl.gz")):
        with gzip.open(shard, "rt", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                record = json.loads(line)
                doc = record.get("document") or {}
                body_lines: list[str] = []
                for line_obj in record.get("lines") or []:
                    text = (line_obj.get("text") or "").strip()
                    if not text:
                        continue
                    speaker = SPEAKER_TAG_RE.sub(
                        "", line_obj.get("speaker_raw") or ""
                    ).strip()
                    if speaker and not text.startswith(f"{speaker}："):
                        text = f"{speaker}：{text}"
                    body_lines.append(text)
                body = "\n".join(body_lines)
                if not body:
                    continue
                yield doc, body


def build_index(
    corpus_root: Path,
    db_path: Path,
    log=print,
) -> dict[str, object]:
    """从 PRTS 语料发布目录重建中文索引。

    Args:
        corpus_root: 发布包目录，其下应有 endfield_official_game 等分片包。
        db_path: 输出的 SQLite 文件路径。
        log: 进度输出函数。

    Returns:
        含文档数、字符数、耗时、体积的统计字典。

    Raises:
        FileNotFoundError: 语料目录不存在或缺少分片。
    """
    if not (corpus_root / SOURCE_PACKS[0] / "shards").is_dir():
        raise FileNotFoundError(f"找不到语料分片目录：{corpus_root / SOURCE_PACKS[0]}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    building = db_path.with_name(db_path.name + ".building")
    building.unlink(missing_ok=True)

    started = time.time()
    con = sqlite3.connect(building)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.executescript(SCHEMA)

    documents = 0
    characters = 0
    ref_total = 0
    skipped_duplicates = 0
    data_version = ""
    seen_bodies: set[str] = set()
    for pack_index, pack in enumerate(SOURCE_PACKS):
        manifest = corpus_root / pack / "pack-manifest.json"
        if manifest.is_file() and not data_version:
            try:
                data_version = json.loads(manifest.read_text(encoding="utf-8")).get(
                    "source_version", ""
                )
            except ValueError:
                data_version = ""
        count_before = documents
        for doc, raw_body in iter_documents(corpus_root / pack):
            body = clean_body(raw_body)
            if not body:
                continue
            # 只在“整理包”里剔除官方包已收录的同一段文字；
            # 官方包内部相同的短句（如“但……”）属于不同场景，必须保留。
            if pack_index > 0 and body in seen_bodies:
                skipped_duplicates += 1
                continue
            if pack_index == 0:
                seen_bodies.add(body)
            doc_id = doc.get("document_id") or f"{pack}:{documents}"
            kind = doc.get("resource_type") or ""
            refs = extract_refs(raw_body)
            ref_total += len(refs)
            tokens = " ".join(tokenize(doc.get("display_title", "") + " " + body))
            cur = con.execute(
                "INSERT INTO docs (doc_id, title, coll, kind, body, chars, refs)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    doc_id,
                    doc.get("display_title", ""),
                    doc.get("collection_name", "") or "",
                    kind,
                    body,
                    len(body),
                    "\n".join(refs),
                ),
            )
            con.execute(
                "INSERT INTO fts (rowid, title, tokens, doc_id, coll, kind)"
                " VALUES (?,?,?,?,?,?)",
                (
                    cur.lastrowid,
                    doc.get("display_title", ""),
                    tokens,
                    doc_id,
                    doc.get("collection_name", "") or "",
                    kind,
                ),
            )
            documents += 1
            characters += len(body)
        log(f"  {pack}: +{documents - count_before} 篇（累计 {documents}）")

    con.execute(
        "INSERT INTO meta (k, v) VALUES ('documents', ?)", (str(documents),)
    )
    con.execute("INSERT INTO meta (k, v) VALUES ('characters', ?)", (str(characters),))
    con.execute(
        "INSERT INTO meta (k, v) VALUES ('duplicates', ?)", (str(skipped_duplicates),)
    )
    con.execute(
        "INSERT INTO meta (k, v) VALUES ('built_at', ?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    con.execute("INSERT INTO meta (k, v) VALUES ('data_version', ?)", (data_version,))
    con.execute("INSERT INTO meta (k, v) VALUES ('release', ?)", (corpus_root.name,))
    con.execute("INSERT INTO meta (k, v) VALUES ('refs', ?)", (str(ref_total),))
    con.execute("INSERT INTO meta (k, v) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.commit()
    # FTS5 写入会留下空闲页，构建末尾压一次，成品能小 1 MB 左右
    con.execute("VACUUM")
    con.close()

    db_path.unlink(missing_ok=True)
    building.replace(db_path)
    return {
        "documents": documents,
        "characters": characters,
        "refs": ref_total,
        "duplicates": skipped_duplicates,
        "seconds": round(time.time() - started, 1),
        "size_mb": round(db_path.stat().st_size / 1e6, 1),
        "data_version": data_version,
        "schema_version": SCHEMA_VERSION,
    }


def read_meta(db_path: Path) -> dict[str, str]:
    """读取索引的元信息（文档数、构建时间等）。"""
    if not db_path.is_file():
        return {}
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT k, v FROM meta"))
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def kind_counts(db_path: Path) -> dict[str, int]:
    """按资料类型统计篇数，用于页面顶部的统计条。"""
    if not db_path.is_file():
        return {}
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return dict(
            con.execute(
                "SELECT kind, count(*) FROM docs GROUP BY kind ORDER BY 2 DESC"
            ).fetchall()
        )
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def search(
    db_path: Path,
    query: str,
    top_k: int = 5,
    score_ratio: float = 0.45,
) -> list[Hit]:
    """在索引上做 BM25 检索，并用稀有词约束候选。

    Args:
        db_path: 索引文件路径。
        query: 查询文本，可以是原句片段，也可以是关键词。
        top_k: 返回条数上限。
        score_ratio: 相对分数阈值。BM25 分数为负且越小越相关，
            仅保留不高于“最佳分数 × 该比例”的结果，用来砍掉陪跑项。

    Returns:
        按相关度排序的命中列表。
    """
    expression = to_fts_query(query)
    if not expression:
        return []
    query_tokens = set(tokenize(query))
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT d.doc_id, d.title, d.coll, d.kind, d.body, fts.tokens, d.refs,"
            " bm25(fts) AS score"
            " FROM fts JOIN docs d ON d.id = fts.rowid"
            " WHERE fts MATCH ? ORDER BY score LIMIT ?",
            (expression, max(top_k * 6, 20)),
        ).fetchall()
        # 查询词的稀有度权重：靠“我/你”这类高频词命中不算数
        weights: dict[str, float] = {}
        if query_tokens and len(query_tokens) >= 3:
            total_docs = con.execute("SELECT count(*) FROM docs").fetchone()[0] or 1
            for token in query_tokens:
                quoted = '"' + token.replace('"', '""') + '"'
                df = con.execute(
                    "SELECT count(*) FROM fts WHERE fts MATCH ?", (quoted,)
                ).fetchone()[0]
                weights[token] = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
    except sqlite3.Error:
        return []
    finally:
        con.close()

    total_weight = sum(weights.values())
    scored: list[tuple[float, Hit]] = []
    for doc_id, title, coll, kind, body, doc_tokens, refs, score in rows:
        if total_weight > 0:
            present = set(doc_tokens.split())
            overlap = sum(w for t, w in weights.items() if t in present) / total_weight
        else:
            overlap = 0.0
        scored.append(
            (overlap, Hit(doc_id, title, coll, kind, body, score, doc_tokens, refs))
        )

    # 用稀有词守住下限：权重最高的一批词必须真正出现，
    # 否则“我/你/和”这类高频词会把无关文档带进来。
    if weights:
        heaviest = max(weights.values())
        core = {t for t, w in weights.items() if w >= heaviest * 0.6}
        strict = [
            (c, h) for c, h in scored if core <= set(h.tokens.split())
        ]
        if strict:
            scored = strict
        else:
            best_token = max(weights, key=weights.get)
            fallback = [(c, h) for c, h in scored if best_token in set(h.tokens.split())]
            if fallback:
                scored = fallback

    # 保留 BM25 原始排序（scored 已按分数升序），再按相对分数剪枝
    hits = [hit for _, hit in scored]
    if not hits:
        return []
    best = hits[0].score
    cutoff = best * score_ratio
    kept = [h for h in hits if h.score <= cutoff] or hits[:1]
    return kept[:top_k]


def search_text(db_path: Path, query: str, top_k: int = 5) -> str:
    """调试用：直接返回格式化后的检索结果。"""
    hits = search(db_path, query, top_k=top_k)
    if not hits:
        return "没有命中。"
    lines = []
    for hit in hits:
        lines.append(f"[{hit.kind_label}]《{hit.title}》（{hit.coll}）")
        lines.append(hit.body)
        lines.append("")
    return "\n".join(lines)
