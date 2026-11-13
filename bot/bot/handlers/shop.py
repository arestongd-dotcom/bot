from __future__ import annotations

import asyncio
from decimal import Decimal
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.content import texts
from bot.db import repo
from bot.db.models import PaymentKind
from bot.handlers.common import (
    deal_of,
    ensure_user,
    show_invoice,
    show_main,
)
from bot.services.delivery import deliver_payment
from bot.services.payment_info import kind_label, payment_lines, referred_by_label
from bot.services.subscriptions import check_payment, start_payment
from bot.ui import Btn, photo_for, show
from bot.utils.fmt import cb_int, fmt_dt, fmt_gb, user_link

router = Router(name="shop")

_check_lock = asyncio.Lock()


class GiftStates(StatesGroup):
    target = State()


async def _show_already_subscribed(callback: CallbackQuery, sub) -> None:
    await show(
        callback,
        texts.ALREADY_SUBSCRIBED.format(until=fmt_dt(sub.expires_at)),
        [
            [("Докупить устройства", "dev:buy")],
            [("Подарить подписку", "gift:start")],
            [("В меню", "screen:main")],
        ],
        photo_for("shop"),
    )


async def _owned_payment(callback: CallbackQuery, session: AsyncSession):
    user = await ensure_user(session, callback.from_user)
    payment_id = cb_int(callback.data, 2)
    if payment_id is None:
        await callback.answer("Счёт не найден", show_alert=True)
        return None, None
    payment = await repo.get_payment(session, payment_id)
    if not payment or payment.user_id != user.id:
        await callback.answer("Счёт не найден", show_alert=True)
        return None, None
    return payment, user


@router.callback_query(F.data == "buy:open")
async def cb_buy(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await ensure_user(session, callback.from_user)
    sub = await repo.get_active_subscription(session, user.id)
    if sub:
        await _show_already_subscribed(callback, sub)
        return

    plans = await repo.list_plans(session)
    if not plans:
        await show(
            callback, texts.SHOP_EMPTY, [[("Назад", "screen:main")]], photo_for("shop")
        )
        return
    base = next((p for p in plans if p.code == "month"), None)
    rows: list[list[Btn]] = []
    for plan in plans:
        label = f"{plan.title} · {plan.price_label}"
        if base and deal_of(base.price_rub, base.days, plan):
            label += " · выгодно"
        rows.append([(label, f"buy:plan:{plan.id}")])
    rows.append([("Назад", "screen:main")])
    await show(callback, texts.SHOP_INTRO, rows, photo_for("shop"))


@router.callback_query(F.data.startswith("buy:plan:"))
async def cb_plan(callback: CallbackQuery, session: AsyncSession) -> None:
    plan_id = cb_int(callback.data, 2)
    plan = await repo.get_active_plan(session, plan_id) if plan_id else None
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    caption = texts.PLAN_CARD.format(
        title=escape(plan.title, quote=False),
        days=plan.days,
        price=plan.price_label,
        per_day=plan.per_day_label,
        devices=plan.device_limit,
        traffic=fmt_gb(plan.traffic_gb * 1024**3 if plan.traffic_gb else 0),
    )
    base = next((p for p in await repo.list_plans(session) if p.code == "month"), None)
    if base:
        deal = deal_of(base.price_rub, base.days, plan)
        if deal:
            old, percent = deal
            caption += texts.PLAN_HIGHLIGHT.format(
                old=old, price=plan.price_label, percent=percent
            )
    await show(
        callback,
        caption,
        [
            [
                ("QR (недоступно)", "pay:qr"),
                ("Перевод по номеру", f"buy:transfer:{plan.id}"),
            ],
            [("К тарифам", "buy:open")],
        ],
        photo_for("pay"),
    )


@router.callback_query(F.data.startswith("buy:transfer:"))
async def cb_buy_transfer(callback: CallbackQuery, session: AsyncSession) -> None:
    plan_id = cb_int(callback.data, 2)
    plan = await repo.get_active_plan(session, plan_id) if plan_id else None
    user = await ensure_user(session, callback.from_user)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    sub = await repo.get_active_subscription(session, user.id)
    if sub:
        await _show_already_subscribed(callback, sub)
        return
    payment = await start_payment(
        session,
        user,
        amount=Decimal(plan.price_rub),
        kind=PaymentKind.SUBSCRIPTION,
        plan=plan,
        description=f"{settings.shop_name} / {plan.title} / {user.tg_id}",
    )
    await show(
        callback,
        texts.TRANSFER.format(
            number=payment.id,
            amount=f"{Decimal(payment.amount):.0f}",
            details=settings.transfer_details,
        ),
        [
            [("Проверить оплату", f"pay:check:{payment.id}")],
            [("Назад к счёту", f"pay:invoice:{payment.id}")],
        ],
        photo_for("pay"),
    )


@router.callback_query(F.data == "pay:qr")
async def cb_qr_disabled(callback: CallbackQuery) -> None:
    await callback.answer(
        "Оплата по QR временно недоступна. Переведите по номеру телефона.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("pay:transfer:"))
async def cb_transfer(callback: CallbackQuery, session: AsyncSession) -> None:
    payment, _ = await _owned_payment(callback, session)
    if not payment:
        return
    await show(
        callback,
        texts.TRANSFER.format(
            number=payment.id,
            amount=f"{Decimal(payment.amount):.0f}",
            details=settings.transfer_details,
        ),
        [
            [("Проверить оплату", f"pay:check:{payment.id}")],
            [("Назад к счёту", f"pay:invoice:{payment.id}")],
        ],
        photo_for("pay"),
    )


@router.callback_query(F.data.startswith("pay:invoice:"))
async def cb_invoice_again(callback: CallbackQuery, session: AsyncSession) -> None:
    payment, _ = await _owned_payment(callback, session)
    if not payment:
        return
    await show_invoice(callback, payment)


async def _notify_check(
    callback: CallbackQuery, session: AsyncSession, payment, user
) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Оплачено", callback_data=f"adm:approve:{payment.id}"
                ),
                InlineKeyboardButton(
                    text="Не оплачено", callback_data=f"adm:reject:{payment.id}"
                ),
            ]
        ]
    )

    lines = [
        "▫️ <b>Проверка оплаты</b>",
        "",
        f"Счёт: <b>#{payment.id}</b>",
        f"Сумма: <b>{Decimal(payment.amount):.0f} ₽</b>",
        f"Тип: {kind_label(payment.kind)}",
    ]
    lines += await payment_lines(session, payment)
    lines.append(
        f"Пользователь: {user_link(user.username, user.tg_id, user.display_name)} · "
        f"<code>{user.tg_id}</code>"
        f"{await referred_by_label(session, user)}"
    )
    if payment.kind == PaymentKind.GIFT and payment.target_tg_id:
        lines.append(f"Получатель: <code>{payment.target_tg_id}</code>")

    for admin_id in settings.admins:
        try:
            await callback.bot.send_message(
                admin_id,
                "\n".join(lines),
                reply_markup=markup,
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("pay:check:"))
async def cb_check(callback: CallbackQuery, session: AsyncSession) -> None:
    payment, user = await _owned_payment(callback, session)
    if not payment:
        return

    status = await check_payment(payment)
    if status == "succeeded":
        if not await deliver_payment(callback.bot, session, payment):
            await callback.answer(
                "Не удалось активировать подписку. Напиши в поддержку.",
                show_alert=True,
            )
            return
        await show_main(callback, session, user)
        return
    if status == "canceled":
        await callback.answer(texts.REJECTED, show_alert=True)
        return

    marker = f"check_sent:{payment.id}"
    async with _check_lock:
        already = bool(await repo.get_kv(session, marker))
        if not already:
            await repo.set_kv(session, marker, "1")
            await session.commit()

    if already:
        await callback.answer(texts.CHECK_ALREADY_SENT, show_alert=True)
        return

    await _notify_check(callback, session, payment, user)
    await callback.answer(texts.CHECK_SENT, show_alert=True)


@router.callback_query(F.data == "gift:start")
async def cb_gift_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GiftStates.target)
    await show(
        callback,
        texts.GIFT_PROMPT,
        [[("Отмена", "gift:cancel")]],
        photo_for("gift"),
    )


@router.callback_query(F.data == "gift:cancel")
async def cb_gift_cancel(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    await callback.answer("Отменено")
    user = await ensure_user(session, callback.from_user)
    await show_main(callback, session, user)


@router.message(GiftStates.target, F.text)
async def gift_target(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    raw = message.text.strip()
    target = None
    if raw.isdigit() and len(raw) <= 19 and int(raw) < 2**63:
        target = await repo.get_user(session, int(raw))
    elif raw.startswith("@") and 1 < len(raw) <= 64:
        target = await repo.get_user_by_username(session, raw[1:])
    else:
        await state.clear()
        await message.answer(texts.GIFT_NEED_AT)
        user = await ensure_user(session, message.from_user)
        await show_main(message, session, user)
        return

    if not target or target.tg_id == message.from_user.id:
        await message.answer(texts.GIFT_NOT_FOUND)
        return

    await state.update_data(target_tg_id=target.tg_id)
    plans = await repo.list_plans(session)
    rows = [[(f"{p.title} · {p.price_label}", f"gift:plan:{p.id}")] for p in plans]
    rows.append([("Отмена", "gift:cancel")])
    await show(
        message,
        f"▫️ Получатель: <b>{user_link(target.username, target.tg_id, target.display_name)}</b>\n\n"
        "Выбери срок подписки:",
        rows,
        photo_for("gift"),
    )


@router.callback_query(F.data.startswith("gift:plan:"))
async def cb_gift_plan(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    data = await state.get_data()
    target_tg_id = data.get("target_tg_id")
    if not target_tg_id:
        await callback.answer("Сначала укажи получателя", show_alert=True)
        return
    plan_id = cb_int(callback.data, 2)
    plan = await repo.get_active_plan(session, plan_id) if plan_id else None
    user = await ensure_user(session, callback.from_user)
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    payment = await start_payment(
        session,
        user,
        amount=Decimal(plan.price_rub),
        kind=PaymentKind.GIFT,
        plan=plan,
        target_tg_id=target_tg_id,
        description=f"Подарок {plan.title} для {target_tg_id}",
    )
    await state.clear()
    await show_invoice(callback, payment)
