"""Tiny i18n: per-request lang + dict-based translation, no extra deps.

Bundles live as JSON in `web/translations/<lang>.json` and are loaded once at
import. `t(lang, key, **params)` returns the translated string (falling back
to the EN bundle, then to the key itself). The same bundle is shipped to the
client as a JSON blob in the page so JS can use the same keys.

Language is picked per request, in priority order:
  1. `?lang=` query string (also sets the cookie + clears the query via redirect
     in the FastAPI handler — that part lives in `web/main.py`)
  2. `lang` cookie
  3. `Accept-Language` header — first prefix-match against the supported list
  4. `DEFAULT_LANG` (English, since the project is now public)
"""

from __future__ import annotations

import json
from pathlib import Path

LANGS: tuple[str, ...] = ("en", "fr")
DEFAULT_LANG = "en"

_DIR = Path(__file__).parent / "translations"
_BUNDLES: dict[str, dict[str, str]] = {}
for _lang in LANGS:
    _path = _DIR / f"{_lang}.json"
    _BUNDLES[_lang] = json.loads(_path.read_text(encoding="utf-8")) if _path.exists() else {}


def t(lang: str, key: str, **params: object) -> str:
    """Look up `key` in `lang`'s bundle. Fall back to EN, then to the key.

    Params (e.g. `t("en", "recap.samples", count=42)`) are substituted via
    plain `str.format` — translations use `{count}` placeholders.
    """
    s = (
        _BUNDLES.get(lang, {}).get(key)
        or _BUNDLES.get(DEFAULT_LANG, {}).get(key)
        or key
    )
    return s.format(**params) if params else s


def bundle(lang: str) -> dict[str, str]:
    """Return the full key→string map for `lang` (merged with EN as fallback).

    JS uses this on the client — gets the EN base + everything overridden for
    the active lang. That way a missing FR key still resolves to something
    sensible rather than the raw key.
    """
    merged = dict(_BUNDLES.get(DEFAULT_LANG, {}))
    merged.update(_BUNDLES.get(lang, {}))
    return merged


def detect(cookie: str | None, accept_language: str | None) -> str:
    """Pick a supported lang from a cookie or an Accept-Language header."""
    if cookie in LANGS:
        return cookie  # type: ignore[return-value]
    for chunk in (accept_language or "").split(","):
        code = chunk.split(";", 1)[0].strip().lower()
        for lang in LANGS:
            if code.startswith(lang):
                return lang
    return DEFAULT_LANG
