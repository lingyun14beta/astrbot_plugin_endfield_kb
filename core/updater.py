"""索引安装与升级：清单、下载、校验、切换。"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any

from .. import index_fetch
from ..endfield_kb import SCHEMA_VERSION
from ..index_store import decompress, install, validate_db
from . import versioning


class Updater:
    """索引更新服务，依赖插件的配置、目录与重载回调。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    @property
    def config(self) -> dict[str, Any]:
        """插件配置。"""
        return self.plugin.config

    @property
    def index_dir(self) -> Path:
        """索引目录。"""
        return self.plugin.index_dir

    def manifest_urls(self) -> list[str]:
        """更新清单地址。"""
        return versioning.manifest_urls(self.config)

    def zip_urls(self) -> list[str]:
        """索引下载地址。"""
        return versioning.zip_urls(self.config)

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
            db = decompress(path, work) if suffix in {".zip", ".gz"} else path
            meta = validate_db(db, expected_schema=SCHEMA_VERSION)
            hint = release_hint or str(
                meta.get("index_version") or meta.get("release") or ""
            )
            key = versioning.version_key(hint)
            label = (
                f"endfield-{key}"
                if re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", key or "")
                else "manual"
            )
            installed = install(
                db, self.index_dir, label, SCHEMA_VERSION, move_source=True
            )
            self.plugin._load_index()
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
        manifest, source = await index_fetch.fetch_manifest(self.manifest_urls())
        current = self.plugin.local_release or versioning.version_key(
            self.plugin.meta.get("index_version") or self.plugin.meta.get("release", "")
        )
        if not manifest:
            return {
                "ok": True,
                "remote": None,
                "current": current,
                "up_to_date": True,
                "message": "无法获取更新清单（网络或镜像不可达）",
            }
        remote_key = versioning.version_key(str(manifest.get("release_id") or ""))
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
        manifest, _ = await index_fetch.fetch_manifest(self.manifest_urls())
        urls = index_fetch.parse_urls(manifest.get("urls")) if manifest else []
        sha = str(manifest.get("sha256") or "") if manifest else ""
        if not urls:
            urls = self.zip_urls()
        if not urls:
            return {"ok": False, "message": "没有配置下载地址"}

        data = await index_fetch.download(urls, sha)
        target = self.index_dir / ".download.zip"
        target.write_bytes(data)
        try:
            hint = str(manifest.get("release_id") or "") if manifest else ""
            return await asyncio.to_thread(self.install_from_file, target, hint)
        finally:
            target.unlink(missing_ok=True)
