from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from math import ceil
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

from bot.config import settings

log = logging.getLogger(__name__)

TIMEOUT = 20.0

_UUID_RE = re.compile(
    r"/sub/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_CLIENT_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HWID_RE = re.compile(r"^[0-9a-f]{16}$")


class PanelError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _bases() -> list[str]:
    bases = [settings.panel_api_url.rstrip("/")]
    direct = settings.panel_api_url_direct.rstrip("/")
    if direct and direct not in bases:
        bases.append(direct)
    return bases


async def _request(method: str, path: str, payload: dict | None = None) -> dict:
    if not settings.panel_enabled:
        raise PanelError("panel is not configured")

    headers = {"Authorization": f"Bearer {settings.panel_api_token.strip()}"}
    bases = _bases()
    last_error: Exception | None = None

    for index, base in enumerate(bases):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.request(
                    method, base + path, headers=headers, json=payload
                )
        except httpx.HTTPError as exc:
            last_error = exc
            continue

        if response.status_code >= 500 and index < len(bases) - 1:
            last_error = PanelError(
                f"http_{response.status_code}", response.status_code
            )
            continue

        try:
            data = response.json()
        except ValueError as exc:
            raise PanelError("bad_response", response.status_code) from exc

        if response.status_code >= 400 or not data.get("ok"):
            raise PanelError(
                str(data.get("error") or f"http_{response.status_code}"),
                response.status_code,
            )
        return data

    raise PanelError("panel_unreachable") from last_error


def _client_name(client: dict) -> str:
    name = client.get("name")
    return name if isinstance(name, str) else ""


def _timestamp(moment: datetime) -> int:
    value = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _days_until(moment: datetime) -> int:
    seconds = _timestamp(moment) - int(datetime.now(timezone.utc).timestamp())
    return max(1, ceil(seconds / 86400))


def _payload_expiry(expires_at: datetime) -> dict:
    return {"expires_at": _timestamp(expires_at), "days": _days_until(expires_at)}


def _sub_base() -> str:
    url = settings.panel_api_url.strip().rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    return url


def valid_link(link: str | None) -> bool:
    if not isinstance(link, str):
        return False
    value = link.strip()
    if not value or len(value) > 2048 or any(char.isspace() for char in value):
        return False
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname or ""
    return (
        "." in host
        and not host.startswith(".")
        and not host.endswith(".")
        and ".." not in host
    )


def client_title(username: str | None, full_name: str | None) -> str:
    if username:
        return f"@{username.strip().lstrip('@')}"[:64]
    return (full_name or "").strip()[:64]


async def get_client(name: str) -> dict | None:
    try:
        data = await _request("GET", f"/clients/{quote(name, safe='')}")
    except PanelError as exc:
        if exc.status == 404:
            return None
        raise
    client = data.get("client")
    return client if isinstance(client, dict) else None


async def list_clients() -> list[dict]:
    data = await _request("GET", "/clients")
    clients = data.get("clients")
    return [item for item in clients if isinstance(item, dict)] if clients else []


def _uuid_from_link(link: str | None) -> str | None:
    if not link:
        return None
    match = _UUID_RE.search(link)
    return match.group(1) if match else None


async def resolve_client(tg_id: int, link: str | None) -> dict | None:
    uuid = _uuid_from_link(link)
    if uuid:
        for item in await list_clients():
            if item.get("uuid") == uuid:
                return item

    for name in (str(tg_id), f"tg_{tg_id}"):
        client = await get_client(name)
        if client:
            return client
    return None


async def create_client(
    name: str,
    *,
    expires_at: datetime,
    device_limit: int,
    traffic_limit_gb: int = 0,
    title: str = "",
) -> dict:
    payload = {
        "name": name,
        "uuid": str(uuid4()),
        "device_limit": max(0, int(device_limit)),
        "traffic_limit_gb": max(0, int(traffic_limit_gb)),
        "manual": True,
        **_payload_expiry(expires_at),
    }
    if title:
        payload["title"] = title
    data = await _request("POST", "/create", payload)
    client = data.get("client")
    return client if isinstance(client, dict) else {}


async def update_client(name: str, payload: dict) -> dict:
    if not payload:
        raise PanelError("empty_payload")
    data = await _request(
        "PATCH", f"/clients/{quote(name, safe='')}", payload
    )
    client = data.get("client")
    return client if isinstance(client, dict) else {}


async def delete_client(name: str) -> bool:
    data = await _request("DELETE", f"/clients/{quote(name, safe='')}")
    return bool(data.get("ok"))


async def add_days(name: str, days: int) -> dict:
    return await update_client(name, {"days": max(1, int(days))})


async def set_device_limit(name: str, limit: int) -> dict:
    return await update_client(name, {"device_limit": max(0, int(limit))})


async def set_traffic_limit(name: str, gb: int) -> dict:
    return await update_client(name, {"traffic_limit_gb": max(0, int(gb))})


async def list_devices(name: str) -> dict:
    return await _request("GET", f"/clients/{quote(name, safe='')}/devices")


async def delete_device(name: str, device_id: str) -> bool:
    data = await _request(
        "DELETE",
        f"/clients/{quote(name, safe='')}/devices/{quote(device_id, safe='')}",
    )
    return bool(data.get("removed"))


async def reset_devices(name: str) -> int:
    info = await list_devices(name)
    removed = 0
    for device in info.get("devices") or []:
        device_id = device.get("id")
        if isinstance(device_id, str) and await delete_device(name, device_id):
            removed += 1
    return removed


async def list_inbounds() -> list[dict]:
    data = await _request("GET", "/inbounds")
    inbounds = data.get("inbounds")
    return [item for item in inbounds if isinstance(item, dict)] if inbounds else []


async def list_nodes() -> list[dict]:
    data = await _request("GET", "/fed/lagg")
    nodes = data.get("nodes")
    return [item for item in nodes if isinstance(item, dict)] if nodes else []


async def sync_subscription(
    tg_id: int,
    *,
    expires_at: datetime,
    device_limit: int,
    traffic_limit_gb: int = 0,
    link: str | None = None,
    username: str | None = None,
    full_name: str | None = None,
) -> dict:
    name = str(tg_id)
    title = client_title(username, full_name)
    payload = {
        "device_limit": max(0, int(device_limit)),
        **_payload_expiry(expires_at),
    }
    if traffic_limit_gb > 0:
        payload["traffic_limit_gb"] = int(traffic_limit_gb)
    if title:
        payload["title"] = title

    client = await resolve_client(tg_id, link)
    if client:
        current_name = _client_name(client)
        if not current_name:
            raise PanelError("bad_client")
        current = client.get("device_limit")
        if isinstance(current, int) and current == 0:
            payload["device_limit"] = 0
        elif isinstance(current, int) and current > int(device_limit):
            payload["device_limit"] = current
        if current_name.startswith("tg_") and current_name != name:
            try:
                return await update_client(
                    current_name, {**payload, "new_name": name}
                )
            except PanelError as exc:
                if exc.status not in (400, 409):
                    raise
        return await update_client(current_name, payload)

    try:
        return await create_client(
            name,
            title=title,
            expires_at=expires_at,
            device_limit=device_limit,
            traffic_limit_gb=traffic_limit_gb,
        )
    except PanelError as exc:
        if exc.status != 409:
            raise
        existing = await get_client(name) or await get_client(f"tg_{tg_id}")
        if not existing:
            raise
        return await update_client(_client_name(existing), payload)


def client_link(client: dict) -> str | None:
    for key in ("sub_url", "subscription_url"):
        value = client.get(key)
        if isinstance(value, str) and valid_link(value):
            return value.strip()

    uuid = client.get("uuid")
    if isinstance(uuid, str) and _CLIENT_UUID_RE.fullmatch(uuid):
        base = _sub_base()
        if base:
            return f"{base}/sub/{uuid}"
    return None


def apply_client(sub, client: dict) -> None:
    expires = client.get("expires_at")
    if isinstance(expires, (int, float)) and expires > 0:
        sub.expires_at = datetime.fromtimestamp(int(expires), tz=timezone.utc)

    limit = client.get("device_limit")
    if isinstance(limit, int) and limit >= 0:
        sub.device_limit = limit

    used = client.get("traffic_used_bytes")
    if isinstance(used, (int, float)):
        sub.traffic_used_bytes = int(used)
    else:
        used_gb = client.get("traffic_used_gb")
        if isinstance(used_gb, (int, float)):
            sub.traffic_used_bytes = int(float(used_gb) * 1024**3)


def valid_device_token(token: str | None) -> bool:
    return bool(token and _HWID_RE.fullmatch(token))


def device_token(device: dict) -> str | None:
    hwid = device.get("hwid")
    if isinstance(hwid, str) and _HWID_RE.fullmatch(hwid):
        return hwid

    device_id = device.get("id")
    if isinstance(device_id, str) and device_id.startswith("hwid:"):
        suffix = device_id[5:]
        if len(suffix) >= 16:
            return suffix[:16]
    return None


def find_device(devices: list[dict], token: str) -> dict | None:
    if not valid_device_token(token):
        return None

    for device in devices:
        if device.get("hwid") == token:
            return device
        device_id = str(device.get("id") or "")
        if device_id.startswith("hwid:") and device_id[5:].startswith(token):
            return device
    return None
