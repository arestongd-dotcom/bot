from __future__ import annotations

from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from bot.config import BASE_DIR

SCREEN_DIR = BASE_DIR / "assets" / "screens"

PHOTOS: dict[str, list[str]] = {
    "main": [
        str(SCREEN_DIR / "start.png"),
        str(SCREEN_DIR / "main.jpg"),
        str(SCREEN_DIR / "main.png"),
    ],
    "shop": [
        str(SCREEN_DIR / "tarif.png"),
        str(SCREEN_DIR / "shop.jpg"),
        str(SCREEN_DIR / "shop.png"),
    ],
    "keys": [
        str(SCREEN_DIR / "connect.png"),
        str(SCREEN_DIR / "keys.jpg"),
        str(SCREEN_DIR / "keys.png"),
    ],
    "buy_devices": [str(SCREEN_DIR / "buyother.png"), str(SCREEN_DIR / "buyother.jpg")],
    "del_devices": [str(SCREEN_DIR / "delother.png"), str(SCREEN_DIR / "delother.jpg")],
    "referral": [
        str(SCREEN_DIR / "partnerka.png"),
        str(SCREEN_DIR / "referral.jpg"),
        str(SCREEN_DIR / "referral.png"),
    ],
    "gift": [
        str(SCREEN_DIR / "giftsub.png"),
        str(SCREEN_DIR / "gift.jpg"),
        str(SCREEN_DIR / "gift.png"),
    ],
    "profile": [str(SCREEN_DIR / "profile.jpg"), str(SCREEN_DIR / "profile.png")],
    "pay": [
        str(SCREEN_DIR / "oplata.png"),
        str(SCREEN_DIR / "pay.jpg"),
        str(SCREEN_DIR / "pay.png"),
    ],
    "docs": [
        str(SCREEN_DIR / "documents.png"),
        str(SCREEN_DIR / "docs.jpg"),
        str(SCREEN_DIR / "docs.png"),
    ],
}

CAPTION_LIMIT = 1024

Btn = tuple[str, str]

_PHOTO_CACHE: dict[str, str] = {}


def _cache_key(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return path.as_posix()
    return f"{path.as_posix()}:{int(stat.st_mtime)}:{stat.st_size}"


def _remember(message: Message | None, path: Path) -> None:
    if message and getattr(message, "photo", None):
        _PHOTO_CACHE[_cache_key(path)] = message.photo[-1].file_id


async def _cached_file_id(path: Path) -> str | None:
    key = _cache_key(path)
    if key in _PHOTO_CACHE:
        return _PHOTO_CACHE[key]
    from bot.db import repo
    from bot.db.session import SessionLocal

    async with SessionLocal() as session:
        value = await repo.get_kv(session, f"photo:{key}")
    if value:
        _PHOTO_CACHE[key] = value
    return value


async def _store_file_id(path: Path, file_id: str) -> None:
    key = _cache_key(path)
    _PHOTO_CACHE[key] = file_id
    try:
        from bot.db import repo
        from bot.db.session import SessionLocal

        async with SessionLocal() as session:
            await repo.set_kv(session, f"photo:{key}", file_id)
            await session.commit()
    except Exception:
        pass


def photo_for(key: str) -> str | None:
    for candidate in PHOTOS.get(key, []):
        if Path(candidate).is_file():
            return candidate
    return None


def build_kb(rows: list[list[Btn]]) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []
    for row in rows:
        line = []
        for text, target in row:
            if target.startswith("url:"):
                line.append(InlineKeyboardButton(text=text, url=target[4:]))
            else:
                line.append(InlineKeyboardButton(text=text, callback_data=target))
        keyboard.append(line)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _fit(text: str) -> str:
    return text if len(text) <= CAPTION_LIMIT else text[: CAPTION_LIMIT - 1] + "…"


async def show(
    event: Message | CallbackQuery,
    caption: str,
    rows: list[list[Btn]],
    photo: str | None = None,
    answer_text: str | None = None,
) -> None:
    markup = build_kb(rows)
    caption = _fit(caption)
    is_cb = isinstance(event, CallbackQuery)
    message: Message | None = event.message if is_cb else event

    if photo:
        photo_path = Path(photo)
        file_id = await _cached_file_id(photo_path)
        media = file_id or FSInputFile(photo_path)
        if message is not None:
            try:
                edited = await message.edit_media(
                    InputMediaPhoto(media=media, caption=caption), reply_markup=markup
                )
                _remember(edited, photo_path)
                if edited and getattr(edited, "photo", None) and not file_id:
                    await _store_file_id(photo_path, edited.photo[-1].file_id)
                if is_cb:
                    try:
                        await event.answer()
                    except TelegramBadRequest:
                        pass
                return
            except TelegramBadRequest:
                try:
                    await message.delete()
                except TelegramBadRequest:
                    pass
            sent = await message.answer_photo(
                media, caption=caption, reply_markup=markup
            )
            _remember(sent, photo_path)
            if sent and getattr(sent, "photo", None) and not file_id:
                await _store_file_id(photo_path, sent.photo[-1].file_id)
        if is_cb:
            try:
                await event.answer()
            except TelegramBadRequest:
                pass
        return

    if message is not None:
        try:
            await message.edit_text(
                caption, reply_markup=markup, disable_web_page_preview=True
            )
        except TelegramBadRequest:
            await message.answer(
                caption, reply_markup=markup, disable_web_page_preview=True
            )
    if is_cb:
        try:
            await event.answer(answer_text or "")
        except TelegramBadRequest:
            pass
