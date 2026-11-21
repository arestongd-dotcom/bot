import asyncio

from aiogram import Bot

from bot.config import settings


async def main() -> None:
    bot = Bot(settings.bot_token)
    me = await bot.get_me()
    print(me.id, me.username, me.first_name)
    await bot.session.close()


asyncio.run(main())
