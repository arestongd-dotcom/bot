from __future__ import annotations

import uuid as uuidlib
from urllib.parse import quote

from bot.config import settings


def new_uuid() -> str:
    return str(uuidlib.uuid4())


def vless_link(device_uuid: str, label: str | None = None) -> str:
    params = (
        "type=tcp&security=reality&encryption=none"
        f"&sni={settings.server_sni}"
        f"&fp={settings.server_fp}"
        f"&pbk={settings.server_public_key}"
        f"&sid={settings.server_sid}"
    )
    if settings.server_flow:
        params += f"&flow={settings.server_flow}"
    name = label or settings.server_name
    return f"vless://{device_uuid}@{settings.server_host}:{settings.server_port}?{params}#{quote(name)}"


def subscription_url(token: str) -> str:
    return f"{settings.public_sub_base}/sub/{token}"
