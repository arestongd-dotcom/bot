from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.content import texts
from bot.db import repo
from bot.handlers.common import ensure_user, show_main, show_profile
from bot.services.telegraph import get_doc_url
from bot.ui import Btn, photo_for, show

router = Router(name="user")

BTN_PROFILE = "Профиль"
BTN_PARTNER = "Партнёрка"


@router.message(CommandStart())
async def cmd_start(
    message: Message, session: AsyncSession, command: CommandObject
) -> None:
    referred_by = None
    if command.args and command.args.startswith("ref_"):
        raw = command.args[4:]
        if raw.isdigit() and len(raw) <= 19:
            candidate = int(raw)
            if candidate < 2**63:
                referred_by = candidate

    if referred_by is not None:
        referrer = await repo.get_user(session, referred_by)
        if not referrer or referrer.tg_id == message.from_user.id:
            referred_by = None

    user, created = await repo.get_or_create_user(
        session,
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        referred_by=referred_by,
    )
    await session.commit()

    if created and referred_by:
        try:
            await message.bot.send_message(
                referred_by,
                "▫️ По твоей ссылке пришёл новый пользователь.\n"
                "Бонус начислим, когда он оформит подписку.",
            )
        except Exception:
            pass

    await show_main(message, session, user)


@router.message(F.text == BTN_PROFILE)
async def msg_profile(message: Message, session: AsyncSession) -> None:
    user = await ensure_user(session, message.from_user)
    await show_profile(message, session, user)


@router.message(F.text == BTN_PARTNER)
async def msg_partner(message: Message, session: AsyncSession) -> None:
    await _show_referral(message, session, message.from_user.id)


@router.callback_query(F.data == "screen:main")
async def cb_main(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    user = await ensure_user(session, callback.from_user)
    await show_main(callback, session, user)


@router.callback_query(F.data == "screen:profile")
async def cb_profile(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await ensure_user(session, callback.from_user)
    await show_profile(callback, session, user)


@router.callback_query(F.data == "docs:open")
async def cb_docs(callback: CallbackQuery, session: AsyncSession) -> None:
    privacy = await get_doc_url(session, "privacy")
    terms = await get_doc_url(session, "terms")
    rows: list[list[Btn]] = []
    if privacy:
        rows.append([("Политика конфиденциальности", f"url:{privacy}")])
    if terms:
        rows.append([("Пользовательское соглашение", f"url:{terms}")])
    rows.append([("Назад", "screen:main")])

    if not privacy and not terms:
        await callback.answer(texts.DOCS_UNAVAILABLE, show_alert=True)
        return

    await show(
        callback,
        texts.DOCS_INTRO,
        rows,
        photo_for("docs"),
    )


async def _show_referral(
    event: Message | CallbackQuery, session: AsyncSession, tg_id: int
) -> None:
    user = await ensure_user(session, event.from_user)
    invited = await repo.count_referrals(session, user.tg_id)
    link = f"https://t.me/{settings.bot_username}?start=ref_{user.tg_id}"
    rows: list[list[Btn]] = [
        [("Поделиться ссылкой", f"url:https://t.me/share/url?url={link}")],
        [("Назад", "screen:main")],
    ]
    await show(
        event,
        texts.REFERRAL.format(
            percent=settings.referral_percent, link=link, invited=invited
        ),
        rows,
        photo_for("referral"),
    )


@router.callback_query(F.data == "screen:referral")
async def cb_referral(callback: CallbackQuery, session: AsyncSession) -> None:
    await _show_referral(callback, session, callback.from_user.id)
