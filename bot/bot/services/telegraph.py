from __future__ import annotations

import hashlib
import logging
import re

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.content.docs import doc_text
from bot.db.repo import get_kv, set_kv

log = logging.getLogger(__name__)

API = "https://api.telegra.ph"

_INLINE_RE = re.compile(r"(\*\*.+?\*\*|__.+?__|\[[^\]]+\]\([^)]+\))")


def _inline(text: str) -> list:
    out: list = []
    pos = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > pos:
            out.append(text[pos : match.start()])
        token = match.group(0)
        if token.startswith("**"):
            out.append({"tag": "b", "children": [token[2:-2]]})
        elif token.startswith("__"):
            out.append({"tag": "i", "children": [token[2:-2]]})
        else:
            label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", token).groups()
            out.append({"tag": "a", "attrs": {"href": url}, "children": [label]})
        pos = match.end()
    if pos < len(text):
        out.append(text[pos:])
    return out or [""]


def _content(markdown: str) -> list[dict]:
    nodes: list[dict] = []
    lines = markdown.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# "):
            nodes.append({"tag": "h3", "children": _inline(line[2:])})
        elif line.startswith("## "):
            nodes.append({"tag": "h4", "children": _inline(line[3:])})
        elif line.startswith("### "):
            nodes.append({"tag": "h5", "children": _inline(line[4:])})
        elif line.startswith(("- ", "* ")):
            nodes.append({"tag": "p", "children": ["• "] + _inline(line[2:])})
        else:
            nodes.append({"tag": "p", "children": _inline(line)})
    return nodes


def _title_and_body(markdown: str) -> tuple[str, str]:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:256], markdown
    return "Документ", markdown


class TelegraphError(RuntimeError):
    pass


async def _account_token(session: AsyncSession, client: httpx.AsyncClient) -> str:
    token = await get_kv(session, "telegraph_token")
    if token:
        return token

    resp = await client.post(
        f"{API}/createAccount",
        data={
            "short_name": "melloky",
            "author_name": settings.shop_name[:128],
        },
    )
    data = resp.json()
    if not data.get("ok"):
        raise TelegraphError(f"createAccount: {data}")
    token = data["result"]["access_token"]
    await set_kv(session, "telegraph_token", token)
    await session.commit()
    return token


async def publish_doc(session: AsyncSession, key: str) -> str:
    markdown = doc_text(key)
    checksum = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    cached_hash = await get_kv(session, f"telegraph:{key}:hash")
    cached_url = await get_kv(session, f"telegraph:{key}:url")
    if cached_hash == checksum and cached_url:
        return cached_url

    title, body = _title_and_body(markdown)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            token = await _account_token(session, client)
            payload = {
                "access_token": token,
                "title": title,
                "author_name": settings.shop_name,
                "content": _content(body),
                "return_content": "false",
            }
            path = await get_kv(session, f"telegraph:{key}:path")
            if path:
                resp = await client.post(f"{API}/editPage/{path}", json=payload)
                data = resp.json()
                if data.get("ok"):
                    url = data["result"]["url"]
                    await set_kv(session, f"telegraph:{key}:hash", checksum)
                    await set_kv(session, f"telegraph:{key}:url", url)
                    await session.commit()
                    return url

            resp = await client.post(f"{API}/createPage", json=payload)
            data = resp.json()
            if not data.get("ok"):
                raise TelegraphError(str(data))
            result = data["result"]
            await set_kv(session, f"telegraph:{key}:path", result["path"])
            await set_kv(session, f"telegraph:{key}:url", result["url"])
            await set_kv(session, f"telegraph:{key}:hash", checksum)
            await session.commit()
            return result["url"]
    except Exception as exc:
        log.warning("Telegraph publish failed for %s: %s", key, exc)
        return cached_url or ""


async def get_doc_url(session: AsyncSession, key: str) -> str:
    configured = settings.docs_links.get(key) or ""
    if configured:
        return configured
    return await publish_doc(session, key)
