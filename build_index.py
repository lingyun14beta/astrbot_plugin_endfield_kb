"""重建《明日方舟：终末地》中文索引。

用法：

    python build_index.py [--corpus 语料发布目录] [--out 输出文件]

默认从本机 PRTS 语料发布目录读取，构建完成后安装到
data/plugin_data/astrbot_plugin_endfield_kb/index/ 并切换 current.json。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from endfield_kb import SCHEMA_VERSION, build_index  # noqa: E402
from index_store import default_index_dir, install  # noqa: E402

PLUGIN_DIR = Path(__file__).parent
DEFAULT_CORPUS = Path.home() / ".dsh" / "prts-corpus" / "releases"


def latest_release(releases_dir: Path) -> Path:
    """返回 releases 下最新的一个发布目录。"""
    current = releases_dir / "current.json"
    if current.is_file():
        release_id = json.loads(current.read_text(encoding="utf-8")).get("release_id")
        if release_id:
            candidate = releases_dir / release_id
            if candidate.is_dir():
                return candidate
    candidates = [
        p for p in releases_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ]
    if not candidates:
        raise FileNotFoundError(f"{releases_dir} 下没有发布目录")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def release_label(corpus_root: Path) -> str:
    """从发布目录名里抽出可读版本号，如 endfield-1.6.3。"""
    match = re.search(r"endfield-(\d+\.\d+\.\d+)", corpus_root.name)
    return f"endfield-{match.group(1)}" if match else corpus_root.name


def main() -> int:
    parser = argparse.ArgumentParser(description="重建终末地中文知识库索引")
    parser.add_argument("--corpus", type=Path, help="语料发布目录（含 endfield_official_game）")
    parser.add_argument(
        "--out", type=Path, help="只构建到指定文件，不安装（用于打包发布）"
    )
    args = parser.parse_args()

    root = args.corpus or latest_release(DEFAULT_CORPUS)
    print(f"语料目录：{root}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    build_target = args.out or (PLUGIN_DIR / "data" / f".build-{stamp}.sqlite3")
    build_target.parent.mkdir(parents=True, exist_ok=True)

    try:
        stats = build_index(root, build_target)
        print(
            "完成：{documents} 篇，{characters} 字，{seconds} 秒，{size_mb} MB".format(
                **stats
            )
        )

        if args.out:
            print(f"输出文件：{build_target}")
            return 0

        index_dir = default_index_dir(PLUGIN_DIR)
        installed = install(
            build_target,
            index_dir,
            release_label(root),
            schema_version=SCHEMA_VERSION,
            move_source=True,
        )
        print(f"已安装：{installed}")
        print(f"索引目录：{index_dir}")
        return 0
    finally:
        if not args.out:
            build_target.unlink(missing_ok=True)
            shutil.rmtree(PLUGIN_DIR / "data" / ".extract", ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
