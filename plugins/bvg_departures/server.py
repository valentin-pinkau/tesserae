"""bvg_departures, next departures for a stop via v6.bvg.transport.rest.

Free BVG/VBB journey-planner API, no key. Caches per stop_id for a short
TTL since departure boards go stale fast, unlike weather/prices this is
meant to look "live" on every composer render.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.plugin_http import fetch_json

CACHE_TTL_S = 45
HTTP_TIMEOUT_S = 6
USER_AGENT = "tesserae/0.1 (+bvg_departures)"
BASE = "https://v6.bvg.transport.rest"
DEFAULT_STOP_ID = "900057104"  # S Julius-Leber-Brücke (Berlin)


def _cached(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime >= CACHE_TTL_S:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    stop_id = str(options.get("stop_id") or DEFAULT_STOP_ID).strip() or DEFAULT_STOP_ID
    stop_name = str(options.get("stop_name") or "").strip() or "S Julius-Leber-Brücke"
    max_rows = max(1, min(20, int(options.get("max_rows") or 6)))

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_path = data_dir / f"departures_{stop_id}.json"
    cached = _cached(cache_path)
    if cached is not None:
        cached["stop_name"] = stop_name
        return cached

    # Ask for more than max_rows since cancelled trips get filtered out
    # client-side of the fetch (i.e. here), not by the upstream API.
    url = f"{BASE}/stops/{stop_id}/departures?duration=60&results={max_rows * 3}&linesOfStops=false"
    try:
        payload = fetch_json(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT_S, retries=0)
    except Exception as err:
        return {"error": f"{type(err).__name__}: {err}", "stop_name": stop_name}

    if isinstance(payload, dict) and payload.get("error"):
        return {"error": str(payload.get("msg") or "Stop not found."), "stop_name": stop_name}

    raw = payload.get("departures") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return {"error": "Unexpected response from BVG.", "stop_name": stop_name}

    now = datetime.now().astimezone()
    departures = []
    for dep in raw:
        if not isinstance(dep, dict) or dep.get("cancelled"):
            continue
        line = dep.get("line") or {}
        when_iso = dep.get("when") or dep.get("plannedWhen")
        planned_iso = dep.get("plannedWhen")
        if not when_iso:
            continue
        try:
            when = datetime.fromisoformat(when_iso)
        except ValueError:
            continue
        in_min = round((when - now).total_seconds() / 60)
        if in_min < 0:
            continue
        delay_s = dep.get("delay")
        departures.append(
            {
                "line": line.get("name") or "?",
                "product": line.get("product") or "regional",
                "direction": dep.get("direction") or "",
                "time": when.strftime("%H:%M"),
                "planned_time": _hhmm(planned_iso),
                "in_min": in_min,
                "delay_min": round(delay_s / 60) if isinstance(delay_s, (int, float)) else 0,
                "platform": dep.get("platform") or dep.get("plannedPlatform"),
            }
        )
        if len(departures) >= max_rows:
            break

    result: dict[str, Any] = {"stop_name": stop_name, "departures": departures}
    with contextlib.suppress(OSError):
        cache_path.write_text(json.dumps(result), encoding="utf-8")
    return result


def _hhmm(iso: Any) -> str:
    if not isinstance(iso, str):
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return ""
