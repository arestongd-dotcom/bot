import asyncio
import time

from aiogram import Bot

from bot.config import settings


async def main() -> None:
    bot = Bot(settings.bot_token)
    try:
        for i in range(5):
            started = time.perf_counter()
            await bot.get_me()
            print(f"{i + 1}: за {time.perf_counter() - started:.2f} с")
    finally:
        await bot.session.close()


asyncio.run(main())
