from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, TelegramObject

from bot.config import settings


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        if user is None and isinstance(event, CallbackQuery):
            user = event.from_user
        return bool(user and user.id in settings.admins)
