from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import settings

PREFIX = "sqlite+aiosqlite:///"


def db_path() -> Path:
    url = settings.db_url
    if url.startswith(PREFIX):
        return Path(url[len(PREFIX) :])
    return Path("data/melloky.db")


def main() -> None:
    path = db_path()
    if not path.exists():
        print(f"База не найдена: {path}")
        return

    conn = sqlite3.connect(path)
    try:
        deleted = conn.execute("delete from kv where key like 'photo:%'").rowcount
        conn.commit()
    finally:
        conn.close()

    print(f"Кеш фото очищен: удалено записей {deleted}.")
    print("При следующем открытии экранов фото загрузятся заново (один раз).")


if __name__ == "__main__":
    main()
