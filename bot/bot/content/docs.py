from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "docs"

DOC_FILES = {
    "privacy": "privacy_policy.md",
    "terms": "user_agreement.md",
}


@lru_cache(maxsize=4)
def doc_text(key: str) -> str:
    path = DOCS_DIR / DOC_FILES[key]
    if not path.exists():
        return "Документ временно недоступен. Напишите в поддержку."
    return path.read_text(encoding="utf-8")
