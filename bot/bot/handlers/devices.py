from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.content import texts
from bot.db import repo
from bot.db.models import PaymentKind, Subscription, User
from bot.handlers.common import ensure_user, show_invoice
from bot.services import panel
from bot.services.subscriptions import (
    ensure_subscription_access,
    start_payment,
)
from bot.ui import Btn, photo_for, show
from bot.utils.fmt import cb_int, fmt_dt

router = Router(name="vpn")

PAGE_SIZE = 8
MAX_BUY = 5


def _fmt_ts(value) -> str:
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return "нет данных"
    if stamp <= 0:
        return "нет данных"
    return fmt_dt(datetime.fromtimestamp(stamp, tz=timezone.utc))


def _device_text(device: dict) -> tuple[str, str, str]:
    model = escape(
        str(device.get("model") or device.get("label") or "Устройство"),
        quote=False,
    )
    os_name = " ".join(
        str(part) for part in (device.get("os"), device.get("os_ver")) if part
    )
    os_name = escape(os_name or "неизвестно", quote=False)
    ip = escape(str(device.get("ip") or "нет данных"), quote=False)
    return model, os_name, ip


def _support_rows() -> list[list[Btn]]:
    return [
        [("Поддержка", f"url:{settings.support_url}")],
        [("В меню", "screen:main")],
    ]


def _device_caption(info: dict) -> str:
    devices = info.get("devices") or []
    limit = info.get("device_limit") or 0
    count = info.get("devices_count", len(devices))
    limit_text = str(limit) if limit else "без лимита"
    lines = [texts.DEVICES_LIST_HEADER.format(count=count, limit=limit_text)]
    for index, device in enumerate(devices[:PAGE_SIZE], 1):
        model, os_name, ip = _device_text(device)
        lines.append(
            texts.DEVICE_LINE.format(
                index=index,
                model=model,
                os=os_name,
                ip=ip,
                seen=_fmt_ts(device.get("last_seen")),
            )
        )
    if len(devices) > PAGE_SIZE:
        lines.append(f"Показаны первые {PAGE_SIZE} из {len(devices)}.")
    return "\n".join(lines).strip()


def _device_rows(info: dict) -> list[list[Btn]]:
    rows: list[list[Btn]] = []
    for index, device in enumerate((info.get("devices") or [])[:PAGE_SIZE], 1):
        token = panel.device_token(device)
        if token:
            rows.append([(f"Отключить {index}", f"dev:del:{token}")])
    rows.append([("Обновить", "dev:list")])
    rows.append([("В меню", "screen:main")])
    return rows


async def _panel_context(
    callback: CallbackQuery, session: AsyncSession, photo: str | None
) -> tuple[User, Subscription, str, dict] | None:
    user = await ensure_user(session, callback.from_user)
    sub = await repo.get_active_subscription(session, user.id)
    if not sub:
        await callback.answer("Сначала оформи подписку", show_alert=True)
        return None

    if not settings.panel_enabled:
        await show(callback, texts.DEVICES_UNAVAILABLE, _support_rows(), photo)
        return None

    link = await ensure_subscription_access(session, user, sub)
    if not link:
        await show(callback, texts.DEVICES_NO_LINK, _support_rows(), photo)
        return None

    try:
        client = await panel.resolve_client(user.tg_id, link)
        if not client:
            await show(callback, texts.DEVICES_NO_CLIENT, _support_rows(), photo)
            return None
        name = str(client.get("name") or "")
        if not name:
            await show(callback, texts.DEVICES_NO_CLIENT, _support_rows(), photo)
            return None
        info = await panel.list_devices(name)
    except panel.PanelError:
        await show(callback, texts.DEVICES_PANEL_ERROR, _support_rows(), photo)
        return None

    return user, sub, name, info


async def _render_devices(
    callback: CallbackQuery,
    session: AsyncSession,
    answer_text: str | None = None,
) -> None:
    photo = photo_for("del_devices")
    ctx = await _panel_context(callback, session, photo)
    if not ctx:
        return

    _, _, _, info = ctx
    devices = info.get("devices") or []
    if not devices:
        await show(
            callback,
            texts.DEVICES_EMPTY,
            _support_rows(),
            photo,
            answer_text=answer_text,
        )
        return

    await show(
        callback,
        _device_caption(info),
        _device_rows(info),
        photo,
        answer_text=answer_text,
    )


@router.callback_query(F.data == "vpn:connect")
async def cb_connect(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await ensure_user(session, callback.from_user)
    sub = await repo.get_active_subscription(session, user.id)
    if not sub:
        await show(
            callback,
            texts.KEYS_NO_SUB,
            [
                [("Купить VPN", "buy:open")],
                [("В меню", "screen:main")],
            ],
            photo_for("keys"),
        )
        return

    link = await ensure_subscription_access(session, user, sub)
    caption = (
        texts.KEYS_LINK.format(link=escape(link, quote=False))
        if link
        else texts.KEYS_NO_LINK
    )
    await show(
        callback,
        caption,
        [
            [("Обновить", "vpn:connect")],
            [("В меню", "screen:main")],
        ],
        photo_for("keys"),
    )


@router.callback_query(F.data == "dev:buy")
async def cb_dev_buy(callback: CallbackQuery, session: AsyncSession) -> None:
    photo = photo_for("buy_devices")
    ctx = await _panel_context(callback, session, photo)
    if not ctx:
        return

    _, _, _, info = ctx
    active = info.get("devices_count", 0)
    limit = info.get("device_limit") or 0
    if not limit:
        await show(callback, texts.DEVICES_UNLIMITED, _support_rows(), photo)
        return

    price = settings.extra_device_price
    caption = texts.DEVICES_BUY_INTRO.format(
        active=active,
        limit=limit,
        price=price,
    )

    rows: list[list[Btn]] = [
        [(f"{count} · {price * count} ₽", f"dev:qty:{count}")]
        for count in range(1, MAX_BUY + 1)
    ]
    rows.append([("В меню", "screen:main")])
    await show(callback, caption, rows, photo)


@router.callback_query(F.data.startswith("dev:qty:"))
async def cb_dev_qty(callback: CallbackQuery, session: AsyncSession) -> None:
    count = cb_int(callback.data, 2)
    if count is None or not 1 <= count <= MAX_BUY:
        await callback.answer("Неверное количество", show_alert=True)
        return

    photo = photo_for("buy_devices")
    ctx = await _panel_context(callback, session, photo)
    if not ctx:
        return

    user, _, _, _ = ctx
    payment = await start_payment(
        session,
        user,
        amount=Decimal(settings.extra_device_price * count),
        kind=PaymentKind.DEVICES,
        quantity=count,
        description=f"{settings.shop_name} / устройства +{count} / {user.tg_id}",
    )
    await show_invoice(callback, payment)


@router.callback_query(F.data == "dev:list")
async def cb_dev_list(callback: CallbackQuery, session: AsyncSession) -> None:
    await _render_devices(callback, session)


@router.callback_query(F.data.startswith("dev:del:"))
async def cb_dev_del(callback: CallbackQuery, session: AsyncSession) -> None:
    token = callback.data.split(":", 2)[2]
    if not panel.valid_device_token(token):
        await callback.answer(texts.STALE_BUTTON, show_alert=True)
        return

    photo = photo_for("del_devices")
    ctx = await _panel_context(callback, session, photo)
    if not ctx:
        return

    _, _, _, info = ctx
    device = panel.find_device(info.get("devices") or [], token)
    if not device:
        await callback.answer(texts.DEVICES_ALREADY_GONE, show_alert=True)
        return

    model, os_name, ip = _device_text(device)
    await show(
        callback,
        texts.DEVICES_DELETE_CONFIRM.format(model=model, os=os_name, ip=ip),
        [
            [("Отключить", f"dev:delok:{token}")],
            [("Назад", "dev:list")],
        ],
        photo,
    )


@router.callback_query(F.data.startswith("dev:delok:"))
async def cb_dev_delok(callback: CallbackQuery, session: AsyncSession) -> None:
    token = callback.data.split(":", 2)[2]
    if not panel.valid_device_token(token):
        await callback.answer(texts.STALE_BUTTON, show_alert=True)
        return

    photo = photo_for("del_devices")
    ctx = await _panel_context(callback, session, photo)
    if not ctx:
        return

    _, _, name, info = ctx
    device = panel.find_device(info.get("devices") or [], token)
    if not device:
        await callback.answer(texts.DEVICES_ALREADY_GONE, show_alert=True)
        await _render_devices(callback, session)
        return

    try:
        removed = await panel.delete_device(name, str(device.get("id")))
    except panel.PanelError:
        await callback.answer(texts.DEVICES_DELETE_FAILED, show_alert=True)
        return

    if not removed:
        await callback.answer(texts.DEVICES_ALREADY_GONE, show_alert=True)
    await _render_devices(callback, session, answer_text=texts.DEVICES_DELETED)


@router.callback_query(F.data.startswith("dev:pay:"))
async def cb_dev_pay(callback: CallbackQuery) -> None:
    await callback.answer(texts.STALE_BUTTON, show_alert=True)


@router.callback_query(F.data == "dev:count")
async def cb_dev_count(callback: CallbackQuery) -> None:
    await callback.answer(texts.STALE_BUTTON, show_alert=True)
