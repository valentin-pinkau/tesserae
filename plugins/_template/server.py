"""__NAME__ (__ID__) server-side data fetch.

OPTIONAL. Keep this only if the widget needs data the browser can't reach
(APIs, files, cross-origin). Delete the file if client.js renders from
ctx.cell.options alone.

Contract:
  * fetch(options, settings, *, ctx) -> any JSON-serialisable value -> ctx.data
  * ctx = {"panel_w", "panel_h", "preview", "data_dir"}  (NO cell size — return
    the full dataset and slice/branch by ctx.cell.size in client.js).
  * NEVER raise. Return {"error": "friendly message"} on failure; the string
    lands directly in the cell, so write it for the person editing the widget.
  * Called fresh on every render (~6s composer budget) — cache in data_dir and
    keep timeouts tight.

This template returns built-in demo data when no ``api_url`` is configured, so
the widget renders offline out of the box. Replace ``_fetch_live`` with your
real source.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.plugin_http import fetch_json

CACHE_TTL_S = 600  # 10 min; copy from weather_now for the canonical pattern
HTTP_TIMEOUT_S = 6
USER_AGENT = "tesserae/0.1 (+__ID__)"


def _cached(path: Path) -> dict[str, Any] | None:
    if not path.exists() or time.time() - path.stat().st_mtime >= CACHE_TTL_S:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _demo() -> dict[str, Any]:
    return {
        "title": "",
        "items": [
            {"icon": "check-circle", "label": "Online", "value": "OK", "accent": 3},
            {"icon": "warning", "label": "Queue depth", "value": "12", "accent": 2},
            {"icon": "clock", "label": "Latency", "value": "84 ms", "accent": 4},
            {"icon": "database", "label": "Records", "value": "1,240", "accent": 5},
        ],
    }


def _fetch_live(api_url: str, api_key: str) -> dict[str, Any]:
    """Adapt this to your source. fetch_json is GET-only JSON with a
    timeout + retries + User-Agent; use urllib.request directly for other
    verbs / request bodies."""
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = fetch_json(api_url, headers=headers, timeout=HTTP_TIMEOUT_S, retries=0)
    # Map the upstream shape into {title, items:[{icon,label,value,accent}]}.
    items = [
        {"label": str(k), "value": str(v), "icon": "circle", "accent": 4}
        for k, v in list((payload or {}).items())[:16]
    ]
    return {"title": "", "items": items}


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    api_url = str(settings.get("api_url") or "").strip()
    api_key = str(settings.get("api_key") or "").strip()  # already decrypted

    # No source configured → demo data so the template renders offline.
    if not api_url:
        return _demo()

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_path = data_dir / "data.json"
    cached = _cached(cache_path)
    if cached is not None:
        return cached

    try:
        result = _fetch_live(api_url, api_key)
    except Exception:
        # Translate to something actionable when you can (auth vs input vs
        # upstream-down); a tame fallback otherwise.
        return {"error": "Couldn't load data right now."}

    try:
        cache_path.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return result
