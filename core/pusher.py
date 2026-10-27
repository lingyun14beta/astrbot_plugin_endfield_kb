"""版本推送：订阅管理、定时检查、消息生成与发送。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from .. import notify

MAX_PUSH_RETRY = 3


class Pusher:
    """版本推送服务，依赖插件的配置、上下文与索引目录。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.task: asyncio.Task | None = None

    @property
    def config(self) -> dict[str, Any]:
        """插件配置。"""
        return self.plugin.config

    def state_path(self) -> Path:
        """通知状态的落盘位置，与索引目录同级。"""
        return self.plugin.index_dir.parent / notify.STATE_NAME

    def conf(self) -> dict[str, Any]:
        """读取版本推送配置；兼容早期写在顶层的几个键。"""
        group = self.config.get("notify_group")
        if isinstance(group, dict) and group:
            return group
        legacy: dict[str, Any] = {}
        for new_key, old_key in (
            ("enable", "notify_enable"),
            ("interval", "notify_interval"),
            ("pre_download", "notify_pre_download"),
            ("umos", "notify_umos"),
        ):
            if old_key in self.config:
                legacy[new_key] = self.config.get(old_key)
        return legacy

    def umos(self) -> list[str]:
        """读取推送目标，过滤掉空值和异常类型。"""
        raw = self.conf().get("umos") or []
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    def set_umos(self, umos: list[str]) -> None:
        """把订阅列表写回配置并落盘。"""
        group = dict(self.conf())
        group["umos"] = umos
        self.config["notify_group"] = group
        try:
            self.config.save_config()
        except Exception as exc:  # noqa: BLE001 - 保存失败不影响当前会话
            logger.warning(f"[终末地KB] 配置保存失败：{exc}")

    def subscribe(self, umo: str) -> str:
        """把会话加入订阅，返回回执文本。"""
        umos = self.umos()
        if umo in umos:
            return f"本会话已订阅：\n{umo}"
        umos.append(umo)
        self.set_umos(umos)
        return f"已订阅：{umo}\n当前共 {len(umos)} 个会话。"

    def unsubscribe(self, umo: str) -> str:
        """把会话移出订阅，返回回执文本。"""
        umos = self.umos()
        if umo not in umos:
            return f"本会话不在订阅列表里：\n{umo}"
        umos = [item for item in umos if item != umo]
        self.set_umos(umos)
        return f"已退订。\n当前共 {len(umos)} 个会话。"

    def status_text(self) -> str:
        """拼出推送状态。"""
        state = notify.load_state(self.state_path())
        umos = self.umos()
        conf = self.conf()
        interval = max(5, int(conf.get("interval", 60) or 60))
        lines = [
            "版本推送",
            f"开关：{'开' if conf.get('enable', True) else '关'}",
            f"间隔：{interval} 分钟",
            f"预下载通知：{'开' if conf.get('pre_download', True) else '关'}",
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
        return "\n".join(lines)

    # ---------------------------------------------------------------- 循环

    def start(self) -> None:
        """启动后台检查。"""
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止后台检查。"""
        if self.task is None:
            return
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        finally:
            self.task = None

    async def _loop(self) -> None:
        """按间隔检查版本；异常只记日志，不退出循环。"""
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[终末地KB] 版本检查失败：{exc}")
            interval = max(5, int(self.conf().get("interval", 60) or 60))
            await asyncio.sleep(interval * 60)

    async def check_once(self) -> None:
        """查一次版本，有变化就推送到已订阅的会话。"""
        if not self.conf().get("enable", True):
            return
        path = self.state_path()
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
        if self.conf().get("pre_download", True):
            old_pre = str(state.get("pre") or "")
            if pre and pre != old_pre:
                messages.append(
                    notify.render(
                        "pre", pre, old_pre, status["total"], status["pre_incremental"]
                    )
                )
            elif not pre and old_pre:
                messages.append(notify.render("pre-remove", "", old_pre, 0, 0))

        umos = self.umos()
        if messages and umos:
            sent = 0
            for umo in umos:
                for text in messages:
                    try:
                        if await self.plugin.context.send_message(
                            umo, MessageChain([Plain(text=text)])
                        ):
                            sent += 1
                    except Exception as exc:  # noqa: BLE001 - 单个会话失败不影响其他
                        logger.warning(f"[终末地KB] 推送到 {umo} 失败：{exc}")
            if sent:
                logger.info(f"[终末地KB] 版本通知已发送 {sent} 条")
            else:
                attempts = int(state.get("retry") or 0) + 1
                if attempts < MAX_PUSH_RETRY:
                    notify.save_state(
                        path,
                        {
                            "main": stored,
                            "pre": str(state.get("pre") or ""),
                            "checked_at": stamp,
                            "retry": str(attempts),
                        },
                    )
                    logger.warning(
                        f"[终末地KB] 版本通知全部失败（第 {attempts}/{MAX_PUSH_RETRY} 次），"
                        "保留旧状态下轮重试"
                    )
                    return
                logger.error(
                    f"[终末地KB] 版本通知连续 {MAX_PUSH_RETRY} 次失败，放弃本轮，"
                    f"版本记为 {main}"
                )

        notify.save_state(path, {"main": main, "pre": pre, "checked_at": stamp})
