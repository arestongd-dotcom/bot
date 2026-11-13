import asyncio

from bot.db.session import SessionLocal
from bot.services.telegraph import publish_doc


async def main() -> None:
    async with SessionLocal() as session:
        url = await publish_doc(session, "privacy")
        print("privacy:", url or "FAILED")
        url2 = await publish_doc(session, "terms")
        print("terms:", url2 or "FAILED")


asyncio.run(main())
