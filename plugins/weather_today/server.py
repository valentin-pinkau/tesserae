"""Current-day temperature / rain-probability / UV-index fetch for
weather_today.

Open-Meteo's hourly array with ``forecast_days=1`` returns exactly today's
24 slots starting at local midnight, so we paint the full calendar day
(00:00–24:00) with no trimming — this is the "over the current day" framing
the widget wants. Cached for 1 hour per (lat, lon) in the plugin's
data_dir — hourly weather doesn't shift enough within an hour to justify
fetching more often.

The "now" fields (``nowIndex``/``nowTemp``/``nowRain``/``nowUv``) are the one
exception: they're recomputed from the current wall clock on *every* call —
including cache hits — using the response's ``utc_offset_seconds`` so the
"now" marker on the chart advances hour to hour even while the underlying
data serves from cache. If the cached day has rolled past local midnight, the
cache is treated as stale and refetched rather than mislabelling yesterday's
slots as today's.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.plugin_http import fetch_json

CACHE_TTL_S = 3600
# See weather_now/server.py for the reasoning; short-fail so the composer's
# hydration cap can't be blown by an Open-Meteo outage.
HTTP_TIMEOUT_S = 5
USER_AGENT = "tesserae/0.1 (+weather_today)"


def _cached(path: Path) -> dict[str, Any] | None:
    if not path.exists() or time.time() - path.stat().st_mtime >= CACHE_TTL_S:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _local_now(utc_offset_s: int) -> tuple[str, str]:
    """Current (hour, date) as ``("HH", "YYYY-MM-DD")`` in the location's
    local time, derived from Open-Meteo's ``utc_offset_seconds`` rather than
    a fresh geocode lookup."""
    local = datetime.now(UTC) + timedelta(seconds=utc_offset_s)
    return local.strftime("%H"), local.strftime("%Y-%m-%d")


def _with_now(result: dict[str, Any]) -> dict[str, Any]:
    """Overlay nowIndex/nowTemp/nowRain/nowUv computed from the current wall
    clock, so they stay live across cache hits instead of freezing at the
    hour the data was fetched."""
    hours: list[str] = result.get("hours") or []
    temps: list[float] = result.get("temps") or []
    rain: list[int] = result.get("rain") or []
    uv: list[float] = result.get("uv") or []
    hour_str, _ = _local_now(int(result.get("utcOffsetS", 0)))
    now_index = hours.index(hour_str) if hour_str in hours else None

    result["nowIndex"] = now_index
    result["nowTemp"] = (
        round(temps[now_index])
        if now_index is not None and now_index < len(temps)
        else None
    )
    result["nowRain"] = (
        rain[now_index] if now_index is not None and now_index < len(rain) else None
    )
    result["nowUv"] = (
        uv[now_index] if now_index is not None and now_index < len(uv) else None
    )
    return result


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    # Coordinates come from the cell's Location pick (composer's
    # ``_resolved_options`` promotes ``location.latitude`` / ``location.longitude``
    # into the top-level options keys). When the user hasn't picked a
    # location yet, surface a friendly empty-state instead of fetching for
    # the equator. The widget's client.js handles the ``error`` key.
    lat_raw = options.get("latitude")
    lon_raw = options.get("longitude")
    if lat_raw in (None, "") or lon_raw in (None, ""):
        return {
            "error": "Pick a location in the cell editor.",
            "label": options.get("label", ""),
        }
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (TypeError, ValueError):
        return {
            "error": "Location has invalid coordinates.",
            "label": options.get("label", ""),
        }

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_path = data_dir / f"today_{lat:.3f}_{lon:.3f}.json"
    cached = _cached(cache_path)
    if cached is not None:
        _, today = _local_now(int(cached.get("utcOffsetS", 0)))
        if cached.get("date") == today:
            # ``label`` / ``place`` are UI strings from the cell editor, not
            # part of the upstream response. Overlay on cache hit so a
            # rename shows up on the next preview instead of waiting out the
            # cache TTL.
            fresh_label = options.get("label", "")
            cached["label"] = fresh_label
            cached["place"] = fresh_label
            return _with_now(cached)
        # Local midnight rolled over since this was fetched — "today" now
        # means a different calendar day than the cached slots cover, so the
        # cache is stale regardless of its TTL. Fall through to refetch.

    # forecast_days=1 + timezone=auto → today's 24 hourly slots aligned to
    # the location's local midnight.
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,precipitation_probability,uv_index"
        "&forecast_days=1&timezone=auto"
    )
    try:
        payload = fetch_json(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT_S,
            retries=0,
        )
    except Exception as err:
        return {"error": f"{type(err).__name__}: {err}"}

    hourly = payload.get("hourly") or {}
    times: list[str] = hourly.get("time") or []
    temps_raw: list[float | None] = hourly.get("temperature_2m") or []
    rain_raw: list[float | None] = hourly.get("precipitation_probability") or []
    uv_raw: list[float | None] = hourly.get("uv_index") or []

    hours: list[str] = []
    temps: list[float] = []
    rain: list[int] = []
    uv: list[float] = []
    for iso, temp, p, u in zip(times, temps_raw, rain_raw, uv_raw, strict=False):
        try:
            hour = datetime.fromisoformat(iso).strftime("%H")
        except (ValueError, TypeError):
            hour = ""
        hours.append(hour)
        temps.append(float(temp) if isinstance(temp, int | float) else 0.0)
        rain.append(int(p) if isinstance(p, int | float) else 0)
        uv.append(round(float(u), 1) if isinstance(u, int | float) else 0.0)

    date = times[0].split("T", 1)[0] if times and "T" in times[0] else ""
    result = {
        "label": options.get("label", ""),
        "place": options.get("label", ""),
        "hours": hours,
        "temps": temps,
        "rain": rain,
        "uv": uv,
        "date": date,
        "utcOffsetS": int(payload.get("utc_offset_seconds") or 0),
    }
    with contextlib.suppress(OSError):
        cache_path.write_text(json.dumps(result), encoding="utf-8")
    return _with_now(result)
