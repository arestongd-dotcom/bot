from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape


def user_url(username: str | None, tg_id: int) -> str:
    name = (username or "").strip().lstrip("@")
    if name:
        return f"https://t.me/{name}"
    return f"tg://user?id={int(tg_id)}"


def user_link(username: str | None, tg_id: int, text: str | None = None) -> str:
    name = (username or "").strip().lstrip("@")
    label = text or (f"@{name}" if name else str(tg_id))
    return (
        f'<a href="{escape(user_url(name, tg_id), quote=True)}">'
        f"{escape(label, quote=False)}</a>"
    )


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "нет данных"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    msk = value.astimezone(timezone(timedelta(hours=3)))
    return msk.strftime("%d.%m.%Y, %H:%M (МСК)")


def fmt_gb(value_bytes: int | None) -> str:
    if not value_bytes:
        return "без лимита"
    return f"{value_bytes / 1024**3:.1f} ГБ"


def fmt_used_gb(value_bytes: int | None) -> str:
    if not value_bytes:
        return "0 ГБ"
    return f"{value_bytes / 1024**3:.1f} ГБ"


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    count = abs(count)
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def cb_int(data: str | None, index: int) -> int | None:
    try:
        value = int((data or "").split(":")[index])
    except (IndexError, ValueError):
        return None
    return value if -2**63 <= value < 2**63 else None
