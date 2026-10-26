"""终末地版本通知：查询鹰角启动器接口、比对版本、生成文案。

只依赖标准库；网络请求放在线程里，避免阻塞事件循环。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

API = "https://launcher.hypergryph.com/api/proxy/batch_proxy"
HEADERS = {
    "x-hg-launcher-device-id": "83a5d5ca-7f0e-4277-ba71-c9e66dafd7e4",
    "x-hg-user-token": "",
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
}
TIMEOUT = 20
STATE_NAME = "notify_state.json"


def _body(version: str) -> dict[str, Any]:
    """构造启动器的 get_latest_game 请求体。"""
    return {
        "proxy_reqs": [
            {
                "kind": "get_latest_game",
                "get_latest_game_req": {
                    "appcode": "6LL0KJuqHBVz33WK",
                    "channel": "1",
                    "sub_channel": "1",
                    "version": version,
                    "launcher_appcode": "abYeZZ16BPluCFyT",
                    "launcher_sub_channel": "1",
                    "disk_type": 0,
                    "patch_encrypt": True,
                },
            }
        ]
    }


def _post(version: str) -> dict[str, Any]:
    """POST 一次接口，返回 get_latest_game_rsp 节点。"""
    request = urllib.request.Request(
        API,
        data=json.dumps(_body(version)).encode("utf-8"),
        headers=HEADERS,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    responses = payload.get("proxy_rsps") or [{}]
    return (responses[0] or {}).get("get_latest_game_rsp") or {}


def _sum_size(packs: Any) -> int:
    """把 packs[].package_size 求和，忽略脏数据。"""
    if not isinstance(packs, list):
        return 0
    total = 0
    for item in packs:
        try:
            total += int((item or {}).get("package_size") or 0)
        except (TypeError, ValueError):
            continue
    return total


def fetch_status(stored_main: str) -> dict[str, Any]:
    """查询最新版本与包体大小。

    Args:
        stored_main: 已知的正式版本号，用于换取增量包与预下载信息；空串表示首次查询。

    Returns:
        含 main、pre、total、incremental、pre_incremental 的字典。
    """
    latest = _post("")
    main = str(latest.get("version") or "")
    if not main:
        raise RuntimeError("接口没有返回版本号")
    status: dict[str, Any] = {
        "main": main,
        "pre": "",
        "total": _sum_size((latest.get("pkg") or {}).get("packs")),
        "incremental": 0,
        "pre_incremental": 0,
    }
    if stored_main:
        detail = _post(stored_main)
        pre_patch = detail.get("pre_patch") or {}
        status["pre"] = str(pre_patch.get("version") or "")
        status["incremental"] = _sum_size((detail.get("patch") or {}).get("patches"))
        status["pre_incremental"] = _sum_size(pre_patch.get("patches"))
    return status


def format_size(value: int | float | None) -> str:
    """把字节数转成可读文本，0 或无效值返回空串。"""
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.1f} {units[index]}"


def is_newer(current: str, old: str) -> bool:
    """按数字段比较版本号，例如 1.6.3 < 1.6.10。"""
    import re

    def key(value: str) -> tuple[list[int], str]:
        return ([int(x) for x in re.findall(r"\d+", value or "")], (value or "").lower())

    return key(current) > key(old)


def render(kind: str, new: str, old: str, total: int, incremental: int) -> str:
    """生成推送文案。

    Args:
        kind: main / pre / pre-remove。
        new: 新版本号。
        old: 旧版本号。
        total: 完整包字节数。
        incremental: 增量包字节数。

    Returns:
        纯文本消息。
    """
    if kind == "pre-remove":
        return f"【终末地】预下载已关闭\n正式版本 {old} 即将上线。"
    if kind == "pre":
        lines = ["【终末地】预下载已开放", f"预下载版本：{new}"]
        sizes = [format_size(total), format_size(incremental)]
        if any(sizes):
            lines.append(
                "完整包 {}｜增量 {}".format(sizes[0] or "未知", sizes[1] or "未知")
            )
        lines.append("可提前下载，正式上线后直接更新。")
        return "\n".join(lines)

    lines = ["【终末地】版本更新", f"{old} → {new}"]
    sizes = [format_size(total), format_size(incremental)]
    if any(sizes):
        lines.append(
            "完整包 {}｜增量 {}".format(sizes[0] or "未知", sizes[1] or "未知")
        )
    lines.append("客户端需更新后方可进入。")
    return "\n".join(lines)


def load_state(path: Path) -> dict[str, str]:
    """读取通知状态，文件不存在或损坏时返回空字典。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, str]) -> None:
    """原子写入通知状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
