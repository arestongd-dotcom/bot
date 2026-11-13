from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from urllib.parse import quote, unquote

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.content import texts
from bot.db import repo
from bot.db.models import (
    Payment,
    PaymentKind,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    User,
    utcnow,
)
from bot.filters import IsAdmin
from bot.handlers.common import ensure_user, show_main
from bot.services import panel
from bot.services.delivery import deliver_payment
from bot.services.payment_info import (
    account_lines,
    kind_label,
    latest_paid_days,
    payment_lines,
    referred_by_label,
)
from bot.services.subscriptions import apply_referral_bonus, grant_days, reject_payment
from bot.ui import Btn, photo_for, show
from bot.utils.fmt import cb_int, fmt_dt, user_link, user_url

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminStates(StatesGroup):
    grant = State()
    link = State()
    message = State()
    h1_search = State()
    h1_devices = State()
    h1_traffic = State()


def _panel_rows(awaiting: int = 0, maintenance: bool = False) -> list[list[Btn]]:
    awaiting_label = f"Ожидают ссылку ({awaiting})" if awaiting else "Ожидают ссылку"
    maintenance_label = (
        "Тех работы: включены" if maintenance else "Тех работы: выключены"
    )
    return [
        [("Заявки на оплату", "adm:pending")],
        [(awaiting_label, "adm:awaiting")],
        [("Выдать дни", "adm:grant")],
        [("Выдать ссылку", "adm:link")],
        [("Написать сообщение", "adm:msg")],
        [("Панель H1", "adm:h1")],
        [(maintenance_label, "adm:maint")],
        [("Главное меню", "screen:main")],
    ]


async def _awaiting_link(session: AsyncSession) -> list[dict]:
    payments = list(
        await session.scalars(
            select(Payment)
            .where(
                Payment.status == PaymentStatus.SUCCEEDED,
                Payment.kind.in_((PaymentKind.SUBSCRIPTION, PaymentKind.GIFT)),
            )
            .order_by(Payment.id.desc())
            .limit(500)
        )
    )
    items: list[dict] = []
    seen: set[int] = set()

    for payment in payments:
        buyer = await session.get(User, payment.user_id)
        if not buyer:
            continue

        recipient = buyer
        if payment.kind == PaymentKind.GIFT and payment.target_tg_id:
            recipient = await repo.get_user(session, payment.target_tg_id)
            if not recipient:
                continue

        if recipient.tg_id in seen:
            continue
        seen.add(recipient.tg_id)

        if await repo.get_kv(session, f"sublink:{recipient.tg_id}"):
            continue

        items.append({"payment": payment, "buyer": buyer, "recipient": recipient})

    active_subs = await session.scalars(
        select(Subscription)
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at > utcnow(),
        )
        .order_by(Subscription.expires_at.desc())
        .limit(500)
    )
    for sub in active_subs:
        recipient = await session.get(User, sub.user_id)
        if not recipient or recipient.tg_id in seen:
            continue
        seen.add(recipient.tg_id)
        if await repo.get_kv(session, f"sublink:{recipient.tg_id}"):
            continue
        items.append({"payment": None, "buyer": recipient, "recipient": recipient})

    return items


async def _awaiting_caption(session: AsyncSession, item: dict) -> str:
    payment: Payment | None = item["payment"]
    buyer: User = item["buyer"]
    recipient: User = item["recipient"]

    lines = [
        "▫️ <b>Ожидает ссылку</b>",
        "",
        f"Пользователь: {user_link(recipient.username, recipient.tg_id, recipient.display_name)} · "
        f"<code>{recipient.tg_id}</code>"
        f"{await referred_by_label(session, recipient)}",
    ]
    if payment is not None:
        lines.append(f"Сумма: <b>{Decimal(payment.amount):.0f} ₽</b>")
        if payment.kind == PaymentKind.GIFT:
            lines.append(
                f"Подарок от: {user_link(buyer.username, buyer.tg_id, buyer.display_name)}"
            )
        lines += await payment_lines(session, payment)
    else:
        lines.append("Оплата: <b>вручную или бонус</b>")
    lines += await account_lines(session, recipient)
    return "\n".join(lines)


async def _show_panel(
    event: Message | CallbackQuery, session: AsyncSession
) -> None:
    data = await repo.stats(session)
    pending = (
        await session.scalar(
            select(func.count(Payment.id)).where(
                Payment.status == PaymentStatus.PENDING
            )
        )
        or 0
    )
    awaiting = await _awaiting_link(session)
    maintenance = await repo.get_kv(session, "maintenance") == "1"
    await show(
        event,
        texts.DEFAULT_ADMIN.format(
            pending=pending,
            awaiting=len(awaiting),
            users=data["users"],
            active=data["active_subs"],
            revenue=float(data["revenue"]),
            maintenance="включены" if maintenance else "выключены",
        ),
        _panel_rows(len(awaiting), maintenance),
        photo_for("main"),
    )


@router.callback_query(F.data == "adm:panel")
async def cb_panel(callback: CallbackQuery, session: AsyncSession) -> None:
    await _show_panel(callback, session)


@router.callback_query(F.data == "adm:maint")
async def cb_maint(callback: CallbackQuery, session: AsyncSession) -> None:
    maintenance = await repo.get_kv(session, "maintenance") == "1"
    await repo.set_kv(session, "maintenance", "0" if maintenance else "1")
    await session.commit()
    await callback.answer(
        "Тех работы выключены" if maintenance else "Тех работы включены",
        show_alert=True,
    )
    await _show_panel(callback, session)


@router.callback_query(F.data == "adm:pending")
async def cb_pending(callback: CallbackQuery, session: AsyncSession) -> None:
    total = (
        await session.scalar(
            select(func.count(Payment.id)).where(
                Payment.status == PaymentStatus.PENDING
            )
        )
        or 0
    )
    result = await session.scalars(
        select(Payment)
        .where(Payment.status == PaymentStatus.PENDING)
        .order_by(Payment.id)
        .limit(10)
    )
    payments = list(result)
    if not payments:
        await callback.answer("Заявок нет", show_alert=True)
        return
    await callback.answer()
    for payment in payments:
        buyer = await session.get(User, payment.user_id)
        lines = [
            f"▫️ Счёт <b>#{payment.id}</b> · {kind_label(payment.kind)} · "
            f"<b>{float(payment.amount):.0f} ₽</b>"
        ]
        if buyer:
            lines.append(
                f"Пользователь: {user_link(buyer.username, buyer.tg_id, buyer.display_name)} · "
                f"<code>{buyer.tg_id}</code>"
                f"{await referred_by_label(session, buyer)}"
            )
        if payment.kind == PaymentKind.GIFT and payment.target_tg_id:
            lines.append(f"Получатель: <code>{payment.target_tg_id}</code>")
        lines += await payment_lines(session, payment)
        buttons = [
            [
                InlineKeyboardButton(
                    text="Подтвердить",
                    callback_data=f"adm:approve:{payment.id}",
                ),
                InlineKeyboardButton(
                    text="Не оплачено",
                    callback_data=f"adm:reject:{payment.id}",
                ),
            ]
        ]
        if buyer:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Профиль",
                        url=user_url(buyer.username, buyer.tg_id),
                    )
                ]
            )
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    if total > len(payments):
        await callback.message.answer(
            f"Показаны первые {len(payments)} из {total} заявок."
        )


@router.callback_query(F.data == "adm:awaiting")
async def cb_awaiting(callback: CallbackQuery, session: AsyncSession) -> None:
    items = await _awaiting_link(session)
    if not items:
        await callback.answer("Все ссылки выданы", show_alert=True)
        return
    await callback.answer()
    for item in items[:10]:
        recipient: User = item["recipient"]
        buttons = [
            [
                InlineKeyboardButton(
                    text="Выдать ссылку",
                    callback_data=f"adm:link:{recipient.tg_id}",
                ),
                InlineKeyboardButton(
                    text="Написать",
                    callback_data=f"adm:msg:{recipient.tg_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Профиль",
                    url=user_url(recipient.username, recipient.tg_id),
                )
            ],
        ]
        await callback.message.answer(
            await _awaiting_caption(session, item),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    if len(items) > 10:
        await callback.message.answer(
            f"Показаны первые 10 из {len(items)}. "
            "После выдачи этих ссылок открой раздел снова."
        )


@router.callback_query(F.data.startswith("adm:approve:"))
async def cb_approve(callback: CallbackQuery, session: AsyncSession) -> None:
    payment_id = cb_int(callback.data, 2)
    payment = await repo.get_payment(session, payment_id) if payment_id else None
    if not payment:
        await callback.answer("Счёт не найден", show_alert=True)
        return
    if payment.status == PaymentStatus.SUCCEEDED:
        await callback.answer("Этот счёт уже подтверждён", show_alert=True)
        return

    ok = await deliver_payment(callback.bot, session, payment)
    if not ok:
        await callback.answer("Не удалось применить платёж", show_alert=True)
        return
    await callback.answer(f"Счёт #{payment.id} подтверждён", show_alert=True)

    buyer = await session.get(User, payment.user_id)
    recipient_tg = None
    if payment.kind == PaymentKind.GIFT and payment.target_tg_id:
        recipient_tg = payment.target_tg_id
    elif payment.kind == PaymentKind.SUBSCRIPTION and buyer:
        recipient_tg = buyer.tg_id

    lines = [
        "▫️ <b>Оплата подтверждена</b>",
        "",
        f"Счёт: <b>#{payment.id}</b>",
        f"Сумма: <b>{Decimal(payment.amount):.0f} ₽</b>",
        f"Тип: {kind_label(payment.kind)}",
    ]
    if buyer:
        lines.append(
            f"Пользователь: {user_link(buyer.username, buyer.tg_id, buyer.display_name)} · "
            f"<code>{buyer.tg_id}</code>"
            f"{await referred_by_label(session, buyer)}"
        )
    if payment.kind == PaymentKind.GIFT and payment.target_tg_id:
        lines.append(f"Получатель: <code>{payment.target_tg_id}</code>")
    lines += await payment_lines(session, payment)

    buttons: list[list[InlineKeyboardButton]] = []
    if recipient_tg:
        if await repo.get_kv(session, f"sublink:{recipient_tg}"):
            lines.append("Статус: ссылка уже выдана")
        else:
            lines.append("Статус: <b>ожидает ссылку</b>")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Выдать ссылку",
                        callback_data=f"adm:link:{recipient_tg}",
                    ),
                    InlineKeyboardButton(
                        text="Написать",
                        callback_data=f"adm:msg:{recipient_tg}",
                    ),
                ]
            )
    if buyer:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Профиль",
                    url=user_url(buyer.username, buyer.tg_id),
                )
            ]
        )
    markup = (
        InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    )
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=markup)
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm:reject:"))
async def cb_reject(callback: CallbackQuery, session: AsyncSession) -> None:
    payment_id = cb_int(callback.data, 2)
    payment = await repo.get_payment(session, payment_id) if payment_id else None
    if not payment:
        await callback.answer("Счёт не найден", show_alert=True)
        return
    if not await reject_payment(session, payment):
        if payment.status == PaymentStatus.SUCCEEDED:
            await callback.answer("Счёт уже подтверждён", show_alert=True)
        else:
            await callback.answer("Счёт уже отклонён", show_alert=True)
        return

    buyer = await session.get(User, payment.user_id)
    if buyer:
        try:
            await callback.bot.send_message(
                buyer.tg_id,
                texts.REJECTED.format(number=payment.id),
            )
        except Exception:
            pass

    await callback.answer("Отмечено как неоплаченное", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data == "adm:grant")
async def cb_grant(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.grant)
    await show(
        callback,
        "▫️ <b>Выдать дни</b>\n\n"
        "Отправь одним сообщением: <code>ID дней</code>\n"
        "Например: <code>123456789 30</code>",
        [[("Отмена", "adm:cancel")]],
        photo_for("main"),
    )


@router.callback_query(F.data == "adm:cancel")
async def cb_cancel(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    await callback.answer("Отменено")
    user = await ensure_user(session, callback.from_user)
    await show_main(callback, session, user)


@router.message(AdminStates.grant, F.text)
async def msg_grant(message: Message, state: FSMContext, session: AsyncSession) -> None:
    parts = message.text.split()
    if (
        len(parts) != 2
        or not parts[0].isdigit()
        or not parts[1].isdigit()
        or len(parts[0]) > 19
        or int(parts[0]) >= 2**63
    ):
        await message.answer(
            "Нужно два числа: <code>ID дней</code>. Попробуй ещё раз или нажми «Отмена»."
        )
        return

    tg_id, days = int(parts[0]), int(parts[1])
    if not 1 <= days <= 3650:
        await message.answer("Дней должно быть от 1 до 3650. Попробуй ещё раз.")
        return
    sub = await grant_days(session, tg_id, days)
    await state.clear()
    if not sub:
        await message.answer("Пользователь не найден. Пусть сначала нажмёт /start.")
        return
    await message.answer(
        f"▫️ Выдано <b>{days}</b> дн. пользователю <code>{tg_id}</code>.\n"
        f"Действует до <b>{fmt_dt(sub.expires_at)}</b>."
    )


@router.callback_query(F.data.startswith("adm:link"))
async def cb_link(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    tg_id = cb_int(callback.data, 2)
    await state.set_state(AdminStates.link)
    await state.update_data(link_tg_id=tg_id)

    if tg_id:
        target = await repo.get_user(session, tg_id)
        name = (
            user_link(target.username, target.tg_id, target.display_name)
            if target
            else str(tg_id)
        )
        ref = await referred_by_label(session, target) if target else ""
        caption = f"▫️ <b>Ссылка для {name}{ref}</b>"
        if target:
            block: list[str] = []
            days = await latest_paid_days(session, target)
            if days:
                block.append(f"Оплачено: <b>{days} дн.</b>")
            block += await account_lines(session, target)
            if block:
                caption += "\n\n" + "\n".join(block)
        caption += "\n\nОтправь ссылку на подписку одним сообщением."
    else:
        caption = (
            "▫️ <b>Выдать ссылку</b>\n\n"
            "Отправь одним сообщением: <code>ID ссылка</code>\n"
            "Например: <code>123456789 https://...</code>"
        )

    await show(callback, caption, [[("Отмена", "adm:cancel")]], photo_for("main"))


@router.message(AdminStates.link, F.text)
async def msg_link(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    tg_id = data.get("link_tg_id")
    raw = message.text.strip()

    if tg_id:
        link = raw
    else:
        parts = raw.split(maxsplit=1)
        if (
            len(parts) != 2
            or not parts[0].isdigit()
            or len(parts[0]) > 19
            or int(parts[0]) >= 2**63
        ):
            await message.answer(
                "Нужно: <code>ID ссылка</code>. Попробуй ещё раз или нажми «Отмена»."
            )
            return
        tg_id, link = int(parts[0]), parts[1].strip()

    if not panel.valid_link(link):
        await message.answer(
            "Это не похоже на ссылку. Отправь ссылку на подписку, "
            "например <code>https://panel.example/sub/...</code>."
        )
        return

    target = await repo.get_user(session, tg_id)
    if not target:
        await state.clear()
        await message.answer("Пользователь не найден. Пусть сначала нажмёт /start.")
        return

    await repo.set_kv(session, f"sublink:{tg_id}", link)
    await session.commit()
    await state.clear()

    bonus = await apply_referral_bonus(session, target)
    bonus_note = ""
    if bonus:
        referrer = (
            await repo.get_user(session, target.referred_by)
            if target.referred_by
            else None
        )
        who = (
            user_link(referrer.username, referrer.tg_id, referrer.display_name)
            if referrer
            else "пригласившему"
        )
        bonus_note = f"\nРеферальный бонус {who}: <b>+{bonus} дн.</b>"
        if referrer:
            try:
                await message.bot.send_message(referrer.tg_id, texts.REFERRAL_BONUS)
            except Exception:
                pass

    try:
        await message.bot.send_message(
            target.tg_id,
            "▫️ <b>Ссылка на подключение</b>\n\n"
            "Открой эту ссылку и подключи VPN:\n"
            f"<code>{escape(link, quote=False)}</code>",
        )
    except Exception:
        pass

    await message.answer(
        f"Ссылка отправлена пользователю <code>{tg_id}</code>.{bonus_note}"
    )


@router.callback_query(F.data.startswith("adm:msg"))
async def cb_msg(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    tg_id = cb_int(callback.data, 2)
    await state.set_state(AdminStates.message)
    await state.update_data(msg_tg_id=tg_id)

    if tg_id:
        target = await repo.get_user(session, tg_id)
        name = (
            user_link(target.username, target.tg_id, target.display_name)
            if target
            else str(tg_id)
        )
        caption = (
            f"▫️ <b>Сообщение для {name}</b>\n\n"
            "Отправь текст одним сообщением. Он уйдёт пользователю как есть."
        )
    else:
        caption = (
            "▫️ <b>Написать сообщение</b>\n\n"
            "Отправь одним сообщением: <code>ID текст</code>\n"
            "Например: <code>123456789 Ссылка готова, проверь подключение.</code>"
        )

    await show(callback, caption, [[("Отмена", "adm:cancel")]], photo_for("main"))


@router.message(AdminStates.message, F.text)
async def msg_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    tg_id = data.get("msg_tg_id")
    raw = message.text.strip()

    if tg_id:
        text = raw
    else:
        parts = raw.split(maxsplit=1)
        if (
            not parts
            or not parts[0].isdigit()
            or len(parts[0]) > 19
            or int(parts[0]) >= 2**63
        ):
            await state.clear()
            await message.answer("Отменено. Сообщение не отправлено.")
            await _show_panel(message, session)
            return
        if len(parts) != 2 or not parts[1].strip():
            await message.answer(
                "Нужно: <code>ID текст</code>. Добавь текст сообщения "
                "или нажми «Отмена»."
            )
            return
        tg_id, text = int(parts[0]), parts[1].strip()

    if not text:
        await message.answer("Текст пустой. Напиши сообщение ещё раз.")
        return

    target = await repo.get_user(session, tg_id)
    if not target:
        await state.clear()
        await message.answer("Пользователь не найден. Пусть сначала нажмёт /start.")
        return

    try:
        await message.bot.send_message(target.tg_id, text, parse_mode=None)
    except Exception as exc:
        await message.answer(
            f"Не удалось отправить сообщение: {escape(str(exc), quote=False)}"
        )
        return

    await state.clear()
    await message.answer(f"Сообщение отправлено пользователю <code>{tg_id}</code>.")


def _h1_ref(prefix: str, name: str) -> str | None:
    data = prefix + quote(name, safe="")
    return data if len(data.encode("utf-8")) <= 64 else None


def _h1_arg(data: str, prefix: str) -> str:
    return unquote(data[len(prefix) :])


def _h1_expiry(client: dict) -> datetime | None:
    value = client.get("expires_at")
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    return None


def _h1_caption(client: dict) -> str:
    name = str(client.get("name") or "")
    uuid = str(client.get("uuid") or "")
    status = str(client.get("status") or "")
    lines = [
        "▫️ <b>Клиент панели</b>",
        "",
        f"Имя: <code>{escape(name, quote=False)}</code>",
        f"UUID: <code>{escape(uuid, quote=False)}</code>",
        f"Статус: <b>{escape(status, quote=False)}</b>",
    ]

    title = str(client.get("title") or "")
    if title:
        lines.insert(3, f"Название: {escape(title, quote=False)}")

    expires = _h1_expiry(client)
    if expires:
        left = client.get("left_days")
        tail = f" · осталось {left} дн." if isinstance(left, int) else ""
        lines.append(f"Срок: <b>{fmt_dt(expires)}</b>{tail}")

    limit = client.get("device_limit")
    count = client.get("devices_count", 0)
    limit_text = str(limit) if isinstance(limit, int) and limit else "без лимита"
    lines.append(f"Устройства: <b>{count}</b> из <b>{limit_text}</b>")

    used = client.get("traffic_used_gb")
    traffic = (
        f"<b>{float(used):.2f} ГБ</b>"
        if isinstance(used, (int, float))
        else "нет данных"
    )
    total = client.get("traffic_limit_gb")
    if isinstance(total, (int, float)) and total:
        traffic += f" из <b>{float(total):.0f} ГБ</b>"
    lines.append(f"Трафик: {traffic}")

    tags = [
        str(item.get("tag"))
        for item in client.get("inbound_links") or []
        if isinstance(item, dict) and item.get("tag")
    ]
    if tags:
        lines.append("Локации: " + escape(", ".join(tags), quote=False))

    link = panel.client_link(client)
    if link:
        lines.append(f"\nСсылка:\n<code>{escape(link, quote=False)}</code>")
    return "\n".join(lines)


def _h1_rows(name: str) -> list[list[Btn]]:
    rows: list[list[Btn]] = []

    send = _h1_ref("adm:h1:send:", name)
    if send:
        rows.append([("Отправить ссылку", send)])

    extend: list[Btn] = []
    for days in (30, 90, 180):
        ref = _h1_ref(f"adm:h1:ext:{days}:", name)
        if ref:
            extend.append((f"+{days}", ref))
    if extend:
        rows.append(extend)

    for label, prefix in (
        ("Лимит устройств", "adm:h1:dev:"),
        ("Лимит трафика", "adm:h1:trf:"),
        ("Сбросить устройства", "adm:h1:rst:"),
        ("Удалить клиента", "adm:h1:del:"),
    ):
        ref = _h1_ref(prefix, name)
        if ref:
            rows.append([(label, ref)])

    back = _h1_ref("adm:h1:c:", name)
    if back:
        rows.append([("Обновить", back)])
    rows.append([("К панели", "adm:h1")])
    return rows


async def _h1_fail(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer("Панель не ответила", show_alert=True)
    else:
        await event.answer("Панель не ответила. Попробуй позже.")


async def _h1_show_client(event: Message | CallbackQuery, client: dict) -> None:
    name = str(client.get("name") or "")
    await show(event, _h1_caption(client), _h1_rows(name), photo_for("main"))


async def _h1_sync_local(
    session: AsyncSession, name: str, client: dict
) -> None:
    if not name.startswith("tg_") or not name[3:].isdigit():
        return
    user = await repo.get_user(session, int(name[3:]))
    if not user:
        return
    sub = await repo.get_active_subscription(session, user.id)
    if not sub:
        return
    panel.apply_client(sub, client)
    await session.commit()


@router.callback_query(F.data == "adm:h1")
async def cb_h1(callback: CallbackQuery) -> None:
    if not settings.panel_enabled:
        await callback.answer("Панель не настроена", show_alert=True)
        return
    await show(
        callback,
        "▫️ <b>Панель H1</b>\n\n"
        "Клиенты, сроки, лимиты и устройства.\n"
        "Выбери действие.",
        [
            [("Найти по ID", "adm:h1:find"), ("Клиенты", "adm:h1:list")],
            [("Локации", "adm:h1:ib"), ("Узлы", "adm:h1:nodes")],
            [("Админка", "adm:panel")],
        ],
        photo_for("main"),
    )


@router.callback_query(F.data == "adm:h1:find")
async def cb_h1_find(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.h1_search)
    await show(
        callback,
        "▫️ <b>Поиск клиента</b>\n\n"
        "Отправь Telegram ID пользователя одним сообщением.\n"
        "Например: <code>123456789</code>",
        [[("Отмена", "adm:cancel")]],
        photo_for("main"),
    )


@router.message(AdminStates.h1_search, F.text)
async def msg_h1_find(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    raw = message.text.strip()
    if not raw.isdigit() or len(raw) > 19 or int(raw) >= 2**63:
        await message.answer(
            "Нужен числовой Telegram ID. Попробуй ещё раз или нажми «Отмена»."
        )
        return

    tg_id = int(raw)
    await state.clear()
    link = await repo.get_kv(session, f"sublink:{tg_id}")
    try:
        client = await panel.resolve_client(tg_id, link)
    except panel.PanelError:
        await _h1_fail(message)
        return

    if client:
        await _h1_show_client(message, client)
        return

    user = await repo.get_user(session, tg_id)
    sub = await repo.get_active_subscription(session, user.id) if user else None
    if not sub:
        await message.answer(
            "Клиент не найден, активной подписки у пользователя тоже нет."
        )
        return

    await show(
        message,
        "▫️ Клиента в панели нет.\n"
        "Можно создать его по данным подписки.",
        [
            [("Создать клиента", f"adm:h1:new:{tg_id}")],
            [("К панели", "adm:h1")],
        ],
        photo_for("main"),
    )


@router.callback_query(F.data.startswith("adm:h1:new:"))
async def cb_h1_new(callback: CallbackQuery, session: AsyncSession) -> None:
    tg_id = cb_int(callback.data, 3)
    user = await repo.get_user(session, tg_id) if tg_id else None
    sub = await repo.get_active_subscription(session, user.id) if user else None
    if not sub:
        await callback.answer("Нет активной подписки", show_alert=True)
        return

    try:
        client = await panel.sync_subscription(
            tg_id,
            expires_at=sub.expires_at,
            device_limit=sub.device_limit,
            username=user.username,
            full_name=user.full_name,
        )
    except panel.PanelError:
        await _h1_fail(callback)
        return

    panel.apply_client(sub, client)
    link = panel.client_link(client)
    if link:
        await repo.set_kv(session, f"sublink:{tg_id}", link)
    await session.commit()
    await _h1_show_client(callback, client)


@router.callback_query(F.data == "adm:h1:list")
async def cb_h1_list(callback: CallbackQuery) -> None:
    try:
        clients = await panel.list_clients()
    except panel.PanelError:
        await _h1_fail(callback)
        return

    if not clients:
        await callback.answer("Клиентов нет", show_alert=True)
        return

    rows: list[list[Btn]] = []
    for client in clients[:10]:
        name = str(client.get("name") or "")
        title = str(client.get("title") or "").strip()
        ref = _h1_ref("adm:h1:c:", name)
        if ref:
            label = f"{title} · {name}" if title else name
            rows.append([(label[:60] or "без имени", ref)])
    rows.append([("К панели", "adm:h1")])
    await show(
        callback,
        "▫️ <b>Клиенты панели</b>\n\n"
        f"Всего: <b>{len(clients)}</b>\n"
        "Показаны первые 10.",
        rows,
        photo_for("main"),
    )


@router.callback_query(F.data.startswith("adm:h1:c:"))
async def cb_h1_client(callback: CallbackQuery) -> None:
    name = _h1_arg(callback.data, "adm:h1:c:")
    try:
        client = await panel.get_client(name)
    except panel.PanelError:
        await _h1_fail(callback)
        return
    if not client:
        await callback.answer("Клиент не найден", show_alert=True)
        return
    await _h1_show_client(callback, client)


@router.callback_query(F.data.startswith("adm:h1:send:"))
async def cb_h1_send(callback: CallbackQuery, session: AsyncSession) -> None:
    name = _h1_arg(callback.data, "adm:h1:send:")
    if name.isdigit():
        tg_id = int(name)
    elif name.startswith("tg_") and name[3:].isdigit():
        tg_id = int(name[3:])
    else:
        await callback.answer("Это не клиент бота", show_alert=True)
        return

    user = await repo.get_user(session, tg_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    try:
        client = await panel.get_client(name)
    except panel.PanelError:
        await _h1_fail(callback)
        return
    if not client:
        await callback.answer("Клиент не найден", show_alert=True)
        return

    link = panel.client_link(client)
    if not link:
        await callback.answer("У клиента нет ссылки", show_alert=True)
        return

    await repo.set_kv(session, f"sublink:{tg_id}", link)
    await session.commit()
    try:
        await callback.bot.send_message(
            tg_id,
            texts.KEYS_LINK.format(link=escape(link, quote=False)),
        )
    except Exception:
        await callback.answer("Не удалось отправить", show_alert=True)
        return
    await callback.answer("Ссылка отправлена")


@router.callback_query(F.data.startswith("adm:h1:ext:"))
async def cb_h1_extend(callback: CallbackQuery, session: AsyncSession) -> None:
    rest = callback.data[len("adm:h1:ext:") :]
    raw_days, _, raw_name = rest.partition(":")
    if not raw_days.isdigit():
        await callback.answer("Неверный срок", show_alert=True)
        return
    days = int(raw_days)
    if not 1 <= days <= 3650:
        await callback.answer("Неверный срок", show_alert=True)
        return

    name = unquote(raw_name)
    try:
        client = await panel.add_days(name, days)
    except panel.PanelError:
        await _h1_fail(callback)
        return

    await _h1_sync_local(session, name, client)
    await callback.answer(f"Продлено на {days} дн.")
    await _h1_show_client(callback, client)


@router.callback_query(F.data.startswith("adm:h1:dev:"))
async def cb_h1_devices(
    callback: CallbackQuery, state: FSMContext
) -> None:
    name = _h1_arg(callback.data, "adm:h1:dev:")
    await state.set_state(AdminStates.h1_devices)
    await state.update_data(h1_name=name)
    await show(
        callback,
        "▫️ <b>Лимит устройств</b>\n\n"
        f"Клиент: <code>{escape(name, quote=False)}</code>\n\n"
        "Отправь число от 0 до 100.\n"
        "0 означает без лимита.",
        [[("Отмена", "adm:cancel")]],
        photo_for("main"),
    )


@router.message(AdminStates.h1_devices, F.text)
async def msg_h1_devices(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    raw = message.text.strip()
    if not raw.isdigit() or int(raw) > 100:
        await message.answer("Нужно число от 0 до 100. Попробуй ещё раз.")
        return

    data = await state.get_data()
    name = str(data.get("h1_name") or "")
    try:
        client = await panel.set_device_limit(name, int(raw))
    except panel.PanelError:
        await _h1_fail(message)
        return

    await _h1_sync_local(session, name, client)
    await state.clear()
    await _h1_show_client(message, client)


@router.callback_query(F.data.startswith("adm:h1:trf:"))
async def cb_h1_traffic(
    callback: CallbackQuery, state: FSMContext
) -> None:
    name = _h1_arg(callback.data, "adm:h1:trf:")
    await state.set_state(AdminStates.h1_traffic)
    await state.update_data(h1_name=name)
    await show(
        callback,
        "▫️ <b>Лимит трафика</b>\n\n"
        f"Клиент: <code>{escape(name, quote=False)}</code>\n\n"
        "Отправь лимит в ГБ от 0 до 100000.\n"
        "0 означает без лимита.",
        [[("Отмена", "adm:cancel")]],
        photo_for("main"),
    )


@router.message(AdminStates.h1_traffic, F.text)
async def msg_h1_traffic(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    raw = message.text.strip()
    if not raw.isdigit() or int(raw) > 100000:
        await message.answer("Нужно число от 0 до 100000. Попробуй ещё раз.")
        return

    data = await state.get_data()
    name = str(data.get("h1_name") or "")
    try:
        client = await panel.set_traffic_limit(name, int(raw))
    except panel.PanelError:
        await _h1_fail(message)
        return

    await _h1_sync_local(session, name, client)
    await state.clear()
    await _h1_show_client(message, client)


@router.callback_query(F.data.startswith("adm:h1:rst:"))
async def cb_h1_reset(callback: CallbackQuery) -> None:
    name = _h1_arg(callback.data, "adm:h1:rst:")
    try:
        removed = await panel.reset_devices(name)
        client = await panel.get_client(name)
    except panel.PanelError:
        await _h1_fail(callback)
        return

    await callback.answer(f"Отключено устройств: {removed}")
    if not client:
        await show(
            callback,
            "▫️ Клиент не найден.",
            [[("К панели", "adm:h1")]],
            photo_for("main"),
        )
        return
    await _h1_show_client(callback, client)


@router.callback_query(F.data.startswith("adm:h1:del:"))
async def cb_h1_delete(callback: CallbackQuery) -> None:
    name = _h1_arg(callback.data, "adm:h1:del:")
    confirm = _h1_ref("adm:h1:delok:", name)
    back = _h1_ref("adm:h1:c:", name)
    if not confirm or not back:
        await callback.answer("Имя клиента слишком длинное", show_alert=True)
        return
    await show(
        callback,
        "▫️ <b>Удалить клиента</b>\n\n"
        f"<code>{escape(name, quote=False)}</code>\n\n"
        "Доступ пропадёт сразу. Продолжить?",
        [[("Удалить", confirm)], [("Назад", back)]],
        photo_for("main"),
    )


@router.callback_query(F.data.startswith("adm:h1:delok:"))
async def cb_h1_delete_confirm(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    name = _h1_arg(callback.data, "adm:h1:delok:")
    try:
        ok = await panel.delete_client(name)
    except panel.PanelError:
        await _h1_fail(callback)
        return
    if not ok:
        await callback.answer("Клиент не найден", show_alert=True)
        return

    if name.startswith("tg_") and name[3:].isdigit():
        tg_id = int(name[3:])
        await repo.set_kv(session, f"sublink:{tg_id}", "")
        user = await repo.get_user(session, tg_id)
        if user:
            sub = await repo.get_active_subscription(session, user.id)
            if sub:
                sub.status = SubscriptionStatus.CANCELED
        await session.commit()

    await callback.answer("Клиент удалён")
    await show(
        callback,
        "▫️ Клиент удалён.",
        [[("К панели", "adm:h1")]],
        photo_for("main"),
    )


@router.callback_query(F.data == "adm:h1:ib")
async def cb_h1_inbounds(callback: CallbackQuery) -> None:
    try:
        inbounds = await panel.list_inbounds()
    except panel.PanelError:
        await _h1_fail(callback)
        return

    lines = ["▫️ <b>Локации панели</b>", ""]
    for item in inbounds[:20]:
        tag = escape(str(item.get("tag") or "без названия"), quote=False)
        ident = escape(str(item.get("id") or ""), quote=False)
        protocol = escape(str(item.get("protocol") or ""), quote=False)
        network = escape(str(item.get("network") or ""), quote=False)
        lines.append(
            f"<b>{tag}</b>\n<code>{ident}</code>\n"
            f"{protocol}/{network} · порт {item.get('port')}"
        )
    if not inbounds:
        lines.append("Инбаундов нет.")

    await show(
        callback,
        "\n\n".join(lines),
        [[("К панели", "adm:h1")]],
        photo_for("main"),
    )


@router.callback_query(F.data == "adm:h1:nodes")
async def cb_h1_nodes(callback: CallbackQuery) -> None:
    try:
        nodes = await panel.list_nodes()
    except panel.PanelError:
        await _h1_fail(callback)
        return

    lines = ["▫️ <b>Узлы панели</b>", ""]
    for item in nodes[:20]:
        name = escape(str(item.get("name") or item.get("id") or ""), quote=False)
        ident = escape(str(item.get("id") or ""), quote=False)
        status = escape(str(item.get("status") or ""), quote=False)
        lines.append(f"<b>{name}</b>\n<code>{ident}</code> · {status}")
    if not nodes:
        lines.append("Подключённых узлов нет.")

    await show(
        callback,
        "\n\n".join(lines),
        [[("К панели", "adm:h1")]],
        photo_for("main"),
    )
