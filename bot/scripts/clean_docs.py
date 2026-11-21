import asyncio
from pathlib import Path

from bot.db.session import SessionLocal
from bot.services.telegraph import publish_doc

ROOT = Path(__file__).resolve().parent.parent


def clean() -> None:
    for path in (ROOT / "docs").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        cleaned = text.replace(" — ", ", ").replace("—", "-").replace(" – ", ", ")
        if cleaned != text:
            path.write_text(cleaned, encoding="utf-8")
            print("cleaned", path.name)


async def main() -> None:
    clean()
    async with SessionLocal() as session:
        for key in ("privacy", "terms"):
            print(key, await publish_doc(session, key))


asyncio.run(main())
