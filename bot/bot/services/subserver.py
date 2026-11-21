from __future__ import annotations

import base64
import logging
import os
import re
import time

from aiohttp import web

from bot.config import settings
from bot.db import repo
from bot.db.session import SessionLocal
from bot.services import vpn_core

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_RATE_LIMIT = 120
_RATE_WINDOW = 60.0
_hits: dict[str, list[float]] = {}

_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW
    stamps = _hits.setdefault(ip, [])
    stamps[:] = [stamp for stamp in stamps if stamp >= cutoff]
    if len(stamps) >= _RATE_LIMIT:
        return True
    stamps.append(now)
    if len(_hits) > 2000:
        for key, values in list(_hits.items()):
            if not values or values[-1] < cutoff:
                del _hits[key]
    return False


@web.middleware
async def _security_middleware(
    request: web.Request, handler
) -> web.StreamResponse:
    response = await handler(request)
    response.headers.update(_SECURITY_HEADERS)
    return response


async def handle_sub(request: web.Request) -> web.Response:
    if _rate_limited(request.remote or "unknown"):
        return web.Response(status=429, text="too many requests")

    token = request.match_info["token"]
    if not _TOKEN_RE.fullmatch(token):
        return web.Response(status=404, text="subscription not found")

    async with SessionLocal() as session:
        sub = await repo.get_subscription_by_token(session, token)
        if not sub or not sub.is_active:
            return web.Response(status=404, text="subscription not found")
        devices = await repo.list_devices(session, sub.id)
        links = [vpn_core.vless_link(d.uuid, settings.server_name) for d in devices]

    body = base64.b64encode("\n".join(links).encode()).decode()
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "profile-title": f"base64:{base64.b64encode(settings.shop_name.encode()).decode()}",
        "profile-update-interval": "12",
    }
    return web.Response(text=body, headers=headers)


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(
        text=f"{settings.shop_name} subscription server", content_type="text/plain"
    )


def build_app() -> web.Application:
    app = web.Application(middlewares=[_security_middleware])
    app.router.add_get("/sub/{token}", handle_sub)
    app.router.add_get("/", handle_index)
    return app


async def start_subserver() -> web.AppRunner | None:
    port = int(os.getenv("PORT", settings.sub_server_port))
    if not port:
        return None
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, settings.sub_server_host, port)
    try:
        await site.start()
    except OSError as exc:
        log.warning(
            "Порт %s занят (%s) - страница подписки не запущена. "
            "Бот продолжит работу, ключи выдаются как обычно.",
            port,
            exc,
        )
        await runner.cleanup()
        return None
    log.info(
        "Страница подписки: http://%s:%s/sub/{token}",
        settings.sub_server_host,
        port,
    )
    return runner
