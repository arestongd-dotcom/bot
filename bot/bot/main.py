from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from bot.config import settings
from bot.db.seed import seed_plans
from bot.db.session import SessionLocal, engine, init_db
from bot.handlers import admin, devices, shop, user
from bot.middlewares.db import DbSessionMiddleware
from bot.services.subscriptions import expiry_worker
from bot.services.subserver import start_subserver

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Меню"),
]


async def on_error(event: ErrorEvent) -> bool:
    log.error("Ошибка обработки апдейта", exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer(
                "Что-то пошло не так. Попробуй ещё раз.", show_alert=True
            )
        elif update.message:
            await update.message.answer("Что-то пошло не так. Попробуй ещё раз.")
    except Exception:
        pass
    return True


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    session = AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else None
    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.errors.register(on_error)

    dp.message.outer_middleware(DbSessionMiddleware())
    dp.callback_query.outer_middleware(DbSessionMiddleware())

    dp.include_router(admin.router)
    dp.include_router(shop.router)
    dp.include_router(devices.router)
    dp.include_router(user.router)

    await init_db()
    async with SessionLocal() as session:
        await seed_plans(session)

    runner = await start_subserver()
    worker = asyncio.create_task(expiry_worker(bot))

    try:
        await bot.set_my_commands(COMMANDS)
        me = await bot.get_me()
        log.info("Бот @%s запущен", me.username)
        await dp.start_polling(bot, tasks_concurrency_limit=16)
    finally:
        worker.cancel()
        if runner:
            await runner.cleanup()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
