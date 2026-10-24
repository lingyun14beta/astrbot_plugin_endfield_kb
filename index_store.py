"""索引文件的落盘、校验与版本切换。

只依赖标准库，不导入 AstrBot，便于构建脚本单独使用。

目录约定（index_dir）：

    index/
    ├─ current.json                 # 指针：当前使用的文件与版本
    └─ endfield_kb-1.6.3.sqlite3    # 版本化文件名
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path

INDEX_GLOB = "endfield_kb*.sqlite3"
CURRENT_NAME = "current.json"


def default_index_dir(plugin_dir: Path) -> Path:
    """按 AstrBot 目录约定推断索引目录。

    Args:
        plugin_dir: 插件目录（.../data/plugins/<plugin_name>）。

    Returns:
        .../data/plugin_data/<plugin_name>/index；不是标准布局时退回插件内 data/index。
    """
    data_root = plugin_dir.parent.parent
    if data_root.name == "data":
        return data_root / "plugin_data" / plugin_dir.name / "index"
    return plugin_dir / "data" / "index"


class IndexValidationError(RuntimeError):
    """索引文件损坏或版本不兼容。"""


def ensure_layout(index_dir: Path) -> None:
    """确保索引目录存在。"""
    index_dir.mkdir(parents=True, exist_ok=True)


def read_current(index_dir: Path) -> dict | None:
    """读取 current.json，失败返回 None。"""
    path = index_dir / CURRENT_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def write_current(index_dir: Path, payload: dict) -> None:
    """原子写入 current.json。"""
    target = index_dir / CURRENT_NAME
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def validate_db(path: Path, expected_schema: int | None = None) -> dict[str, str]:
    """校验 SQLite 索引是否可用。

    Args:
        path: 索引文件路径。
        expected_schema: 期望的 schema 版本；None 表示不检查。

    Returns:
        meta 表的键值字典。

    Raises:
        IndexValidationError: 文件缺失、SQLite 损坏或 schema 不兼容。
    """
    if not path.is_file():
        raise IndexValidationError(f"索引文件不存在：{path}")
    if path.stat().st_size < 1024 * 1024:
        raise IndexValidationError(f"索引文件过小，可能下载不完整：{path}")

    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise IndexValidationError(f"无法打开索引：{exc}") from exc

    try:
        check = con.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise IndexValidationError("SQLite 完整性检查未通过")
        meta = dict(con.execute("SELECT k, v FROM meta"))
        docs = con.execute("SELECT count(*) FROM docs").fetchone()[0]
    except sqlite3.Error as exc:
        raise IndexValidationError(f"索引结构异常：{exc}") from exc
    finally:
        con.close()

    if docs <= 0:
        raise IndexValidationError("索引里没有文档")

    schema = meta.get("schema_version")
    if expected_schema is not None and schema is not None:
        if int(schema) != expected_schema:
            raise IndexValidationError(
                f"schema 版本不匹配：文件 {schema}，插件 {expected_schema}"
            )
    meta["_documents"] = str(docs)
    return meta


def list_candidates(index_dir: Path) -> list[Path]:
    """列出索引目录下的候选库文件，按修改时间倒序。"""
    return sorted(
        (p for p in index_dir.glob(INDEX_GLOB) if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def decompress(path: Path, work_dir: Path) -> Path:
    """把 .zip / .gz 解到工作目录，返回其中的 sqlite 文件。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith(".sqlite3")]
            if not names:
                raise IndexValidationError("压缩包里没有 .sqlite3 文件")
            member = names[0]
            out = work_dir / Path(member).name
            with zf.open(member) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            return out
    if path.suffix == ".gz":
        out = work_dir / path.stem
        with gzip.open(path, "rb") as src, out.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        return out
    raise IndexValidationError(f"不支持的压缩格式：{path.name}")


def install(
    source: Path,
    index_dir: Path,
    release_id: str,
    schema_version: int | None = None,
    move_source: bool = False,
) -> Path:
    """校验并安装一个索引文件，切换 current.json。

    Args:
        source: 待安装的 sqlite 文件（可位于临时目录）。
        index_dir: 索引目录。
        release_id: 版本标识，用于文件名。
        schema_version: 期望的 schema 版本。
        move_source: 为 True 时直接移动源文件，省一次 35 MB 拷贝
            （只对临时文件使用；旧库迁移要保持原文件）。

    Returns:
        安装后的文件路径。

    Raises:
        IndexValidationError: 校验失败，原索引保持不变。
    """
    ensure_layout(index_dir)
    meta = validate_db(source, expected_schema=schema_version)
    safe_release = "".join(c for c in release_id if c.isalnum() or c in ".-_") or "unknown"
    target = index_dir / f"endfield_kb-{safe_release}.sqlite3"

    if move_source and source.resolve() != target.resolve():
        try:
            os.replace(source, target)
        except OSError:
            tmp = target.with_suffix(".sqlite3.tmp")
            shutil.copyfile(source, tmp)
            os.replace(tmp, target)
            source.unlink(missing_ok=True)
    else:
        tmp = target.with_suffix(".sqlite3.tmp")
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)

    write_current(
        index_dir,
        {
            "release_id": release_id,
            "schema_version": meta.get("schema_version", ""),
            "file": target.name,
            "documents": meta.get("_documents", ""),
            "characters": meta.get("characters", ""),
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    cleanup(index_dir, keep=2)
    return target


def cleanup(index_dir: Path, keep: int = 2) -> None:
    """只保留最近 keep 个索引文件，删掉更旧的。"""
    for old in list_candidates(index_dir)[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def resolve(
    index_dir: Path,
    legacy_dirs: list[Path] | None = None,
    schema_version: int | None = None,
) -> tuple[Path | None, dict[str, str], str]:
    """按优先级找到可用的索引。

    顺序：current.json → 目录内最新的 .sqlite3 → 压缩包 → 旧路径迁移。

    Args:
        index_dir: 索引目录。
        legacy_dirs: 旧版本存放位置（例如插件目录下的 data/）。
        schema_version: 期望的 schema 版本。

    Returns:
        (索引路径或 None, meta, 来源说明)。
    """
    ensure_layout(index_dir)

    current = read_current(index_dir)
    if current and current.get("file"):
        candidate = index_dir / str(current["file"])
        try:
            meta = validate_db(candidate, expected_schema=schema_version)
            return candidate, meta, "current"
        except IndexValidationError:
            pass

    for candidate in list_candidates(index_dir):
        try:
            meta = validate_db(candidate, expected_schema=schema_version)
        except IndexValidationError:
            continue
        write_current(
            index_dir,
            {
                "release_id": meta.get("release", candidate.stem),
                "schema_version": meta.get("schema_version", ""),
                "file": candidate.name,
                "documents": meta.get("_documents", ""),
                "characters": meta.get("characters", ""),
                "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
        )
        return candidate, meta, "scan"

    for archive in sorted(index_dir.glob("endfield_kb*.zip")) + sorted(
        index_dir.glob("endfield_kb*.gz")
    ):
        try:
            extracted = decompress(archive, index_dir / ".extract")
            meta = validate_db(extracted, expected_schema=schema_version)
        except (IndexValidationError, OSError, zipfile.BadZipFile):
            continue
        installed = install(
            extracted, index_dir, meta.get("release", "manual"), schema_version
        )
        shutil.rmtree(index_dir / ".extract", ignore_errors=True)
        return installed, meta, "archive"

    for legacy in legacy_dirs or []:
        old = legacy / "endfield_kb.sqlite3"
        if not old.is_file():
            continue
        try:
            meta = validate_db(old, expected_schema=schema_version)
        except IndexValidationError:
            continue
        installed = install(old, index_dir, meta.get("release", "legacy"), schema_version)
        return installed, meta, "migrated"

    return None, {}, "missing"
