from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.config import settings
from bot.content import texts
from bot.db import repo
from bot.db.session import SessionLocal

log = logging.getLogger(__name__)

NOTICE_COOLDOWN = 60.0
THROTTLE_INTERVAL = 0.25
THROTTLE_CLEANUP = 5000

_notice_at: dict[int, float] = {}
_last_action: dict[int, float] = {}


def _user_id(event: TelegramObject) -> int | None:
    user = getattr(event, "from_user", None)
    return user.id if user else None


def _throttled(user_id: int, now: float) -> bool:
    last = _last_action.get(user_id)
    if last is not None and now - last < THROTTLE_INTERVAL:
        return True
    _last_action[user_id] = now
    if len(_last_action) > THROTTLE_CLEANUP:
        cutoff = now - 300.0
        for key, value in list(_last_action.items()):
            if value < cutoff:
                del _last_action[key]
    return False


async def _notify_maintenance(event: TelegramObject) -> None:
    if isinstance(event, CallbackQuery):
        try:
            await event.answer(texts.MAINTENANCE_SHORT, show_alert=True)
        except Exception:
            pass
        return

    if not isinstance(event, Message):
        return

    user_id = _user_id(event)
    now = time.monotonic()
    if user_id is not None:
        if now - _notice_at.get(user_id, 0.0) < NOTICE_COOLDOWN:
            return
        _notice_at[user_id] = now
        if len(_notice_at) > THROTTLE_CLEANUP:
            cutoff = now - NOTICE_COOLDOWN
            for key, value in list(_notice_at.items()):
                if value < cutoff:
                    del _notice_at[key]

    text = texts.MAINTENANCE.format(
        support=f'<a href="{settings.support_url}">поддержка</a>'
    )
    try:
        await event.answer(text)
    except Exception:
        pass


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        started = time.perf_counter()
        user_id = _user_id(event)
        if user_id is not None and user_id not in settings.admins:
            if _throttled(user_id, time.monotonic()):
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer()
                    except Exception:
                        pass
                return None

        async with SessionLocal() as session:
            data["session"] = session
            if user_id not in settings.admins:
                maintenance = await repo.get_kv(session, "maintenance")
                if maintenance == "1":
                    await _notify_maintenance(event)
                    return None
            try:
                return await handler(event, data)
            finally:
                elapsed = time.perf_counter() - started
                if elapsed > 1.5:
                    log.warning("Обработка апдейта заняла %.2f с", elapsed)
