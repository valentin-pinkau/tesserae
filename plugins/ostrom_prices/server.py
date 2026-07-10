"""Electricity Price (ostrom_prices) server-side fetch.

Pulls today's hourly day-ahead spot prices from the Ostrom API and returns
them as parallel arrays for the client's bar chart. The gross price
(``grossKwhPrice``, cents per kWh, incl. tax + levies) is what we chart.

Ostrom needs two round trips:
  1. OAuth2 client-credentials token exchange (POST, Basic auth). No in-repo
     helper does this and ``fetch_json`` is GET-only, so we POST with
     ``urllib.request`` directly (mirrors ha_core's POST).
  2. GET /spot-prices for the local-day window.

Both responses are disk-cached: the token until its own expiry, and prices
for the entire calendar day (day-ahead prices never change once published;
the cache filename is day+zip scoped so a new day always fetches fresh).
Warm renders inside the composer's ~6s budget make zero network calls; only
the "now" price/index are recomputed per render, from the cached day.

Contract (see plugins/_template/server.py): fetch() is called fresh every
render, must NEVER raise, and returns {"error": "..."} on any failure.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.calendar_time import local_midnight_utc
from app.plugin_http import fetch_json
from app.tz_resolve import app_timezone

logger = logging.getLogger(__name__)


def _http_detail(err: urllib.error.HTTPError) -> str:
    """HTTP status + a short slice of the response body, if any, so the
    surfaced error names the actual upstream complaint instead of a bare code."""
    body = ""
    with contextlib.suppress(Exception):
        body = err.read().decode("utf-8", errors="replace")[:160].strip()
    return f"HTTP {err.code}{f' — {body}' if body else ''}"

# Day-ahead prices for a given date never change once published, and the
# cache filename already encodes the date (see ``prices_path`` below), so a
# cache hit is trusted for as long as the file exists — no wall-clock TTL.
# A new calendar day always misses (different filename) and fetches fresh;
# within a day, only the "now" pointer is recomputed (see _reslice_now),
# never the network fetch itself.
PRICES_CACHE_TTL_S = float("inf")
HTTP_TIMEOUT_S = 6  # short-fail so a slow upstream can't blow the ~6s render budget
USER_AGENT = "tesserae/0.1 (+ostrom_prices)"

# How many days before today to fetch for the comparison band. The p25/p75
# range is computed per hour-of-day across these prior days, so today's line
# can be read as high/low vs. the recent norm. One request covers today + the
# window, so this doesn't add a round trip.
HISTORY_DAYS = 7

# Bumped whenever the cached result's shape changes, so a file written by an
# older version of this plugin is ignored rather than served with missing
# fields (which rendered as an empty "No price data" cell after the line +
# band rework). Old files orphan harmlessly and rotate out daily.
CACHE_SCHEMA = "v2"

AUTH_URL = "https://auth.production.ostrom-api.io/oauth2/token"
PRICES_URL = "https://production.ostrom-api.io/spot-prices"

# Renew a little before the real expiry so a token never dies mid-request.
TOKEN_EXPIRY_SKEW_S = 60

# Default cooldown after ANY failed network attempt (token exchange or prices
# GET), so a failure is remembered and short-circuits the next call instead of
# retrying immediately. Without this, every fetch() that misses the price
# cache retries the full network chain unconditionally — if the host calls
# fetch() more than once a minute (e.g. a live settings-editor preview), that
# alone is enough to keep re-tripping Ostrom's per-minute rate limit forever,
# since a failure was never remembered. Applies to bad-credential errors too:
# those don't get fixed by retrying, so hammering the token endpoint every
# render is pure waste and API-quota risk.
FAILURE_COOLDOWN_S = 60
# Cap how long we honour an upstream Retry-After so a misbehaving header can't
# wedge the widget for hours.
MAX_RETRY_AFTER_S = 300


def _iso_utc(dt: datetime) -> str:
    """Ostrom wants ``2023-11-01T00:00:00.000Z``."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _pct(values: list[float], q: float) -> float | None:
    """Linear-interpolated q-th percentile (same method as numpy's default),
    implemented inline to avoid a hard numpy dependency in a plugin."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return round(s[0], 2)
    pos = (len(s) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return round(s[lo] * (1 - frac) + s[hi] * frac, 2)


def _read_cache(path: Path, ttl_s: float) -> Any | None:
    if not path.exists() or time.time() - path.stat().st_mtime >= ttl_s:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(path: Path, payload: Any) -> None:
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(payload), encoding="utf-8")


def _retry_after_s(err: urllib.error.HTTPError) -> float | None:
    """Parse a numeric ``Retry-After`` header (seconds form; Ostrom's 429
    uses this, not the HTTP-date form), clamped to a sane ceiling."""
    raw = err.headers.get("Retry-After") if err.headers else None
    if not raw:
        return None
    try:
        return min(MAX_RETRY_AFTER_S, max(1.0, float(raw)))
    except (TypeError, ValueError):
        return None


def _active_cooldown(cooldown_path: Path) -> str | None:
    """Return the remembered error message if a prior failure's cooldown
    hasn't elapsed yet, else None (clear to attempt the network again)."""
    cached = _read_cache(cooldown_path, ttl_s=float("inf"))
    if not isinstance(cached, dict):
        return None
    until = cached.get("until")
    message = cached.get("error")
    if isinstance(until, int | float) and isinstance(message, str) and time.time() < until:
        return message
    return None


def _start_cooldown(cooldown_path: Path, message: str, *, seconds: float) -> None:
    _write_cache(cooldown_path, {"until": time.time() + seconds, "error": message})


def _cache_ok(cached: Any) -> bool:
    """True for a price-cache entry worth serving: current schema (a
    partial/old entry rendered an empty "No price data" cell) with at
    least two hourly points."""
    return (
        isinstance(cached, dict)
        and isinstance(cached.get("today"), list)
        and len(cached["today"]) >= 2
    )


# The composer fetches every cell's data in its own thread, all launched
# together, so on a cold cache (first render, or right after the price TTL
# lapses) several threads can all miss the cache in the same instant. This
# lock serializes the network path so only one of them actually calls
# Ostrom; the rest block here and then read the cache the winner just
# populated. Without it, a page with N ostrom_prices cells fires up to 2N
# real requests (token + prices, per cell) in the same moment on a cold
# render — enough on its own to trip a per-minute rate limit even though
# the on-disk cache is otherwise correctly shared and TTL'd.
_fetch_lock = threading.Lock()


def _access_token(client_id: str, client_secret: str, token_path: Path) -> str:
    """Return a valid bearer token, reusing the on-disk one until it nears
    expiry. Raises on exchange failure so the caller can translate it."""
    cached = _read_cache(token_path, ttl_s=float("inf"))
    if isinstance(cached, dict):
        token = cached.get("access_token")
        expires_at = cached.get("expires_at", 0)
        if token and isinstance(expires_at, int | float) and time.time() < expires_at:
            return str(token)

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        AUTH_URL,
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    token = str(payload.get("access_token") or "")
    if not token:
        raise ValueError("no access_token in Ostrom token response")
    expires_in = payload.get("expires_in", 3600)
    if not isinstance(expires_in, int | float):
        expires_in = 3600
    _write_cache(
        token_path,
        {"access_token": token, "expires_at": time.time() + expires_in - TOKEN_EXPIRY_SKEW_S},
    )
    return token


def _shape(rows: list[dict[str, Any]], zone: Any, today: Any, now: datetime) -> dict[str, Any]:
    """From a multi-day Ostrom response build today's hourly price line plus a
    per-hour-of-day p25/p75 comparison band over the prior ``HISTORY_DAYS``.

    Prices are bucketed by *local* date/hour so the chart's x-axis and the
    "now" pointer land in the viewer's timezone regardless of the UTC ``date``
    fields Ostrom returns.
    """
    today_by_hour: dict[int, float] = {}
    hist_by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
    hist_start = today - timedelta(days=HISTORY_DAYS)
    for row in rows:
        iso = row.get("date")
        gross = row.get("grossKwhPrice")
        if not isinstance(iso, str) or not isinstance(gross, int | float):
            continue
        try:
            local = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(zone)
        except (ValueError, TypeError):
            continue
        val = round(float(gross), 2)
        if local.date() == today:
            today_by_hour[local.hour] = val
        elif hist_start <= local.date() < today:
            hist_by_hour[local.hour].append(val)

    if not today_by_hour:
        return {"error": "Ostrom returned no prices for today."}

    hours = sorted(today_by_hour)
    today_prices = [today_by_hour[h] for h in hours]
    p25 = [_pct(hist_by_hour[h], 25) for h in hours]
    p75 = [_pct(hist_by_hour[h], 75) for h in hours]
    has_band = any(v is not None for v in p25)

    now_hour = now.astimezone(zone).hour
    now_index = hours.index(now_hour) if now_hour in hours else -1

    return {
        "labels": [f"{h:02d}" for h in hours],
        "hours": hours,
        "today": today_prices,
        "p25": p25 if has_band else None,
        "p75": p75 if has_band else None,
        "hasBand": has_band,
        "unit": "ct/kWh",
        "nowIndex": now_index,
        "now": today_by_hour.get(now_hour),
        "min": min(today_prices),
        "max": max(today_prices),
        "avg": round(sum(today_prices) / len(today_prices), 2),
    }


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del options  # title is a cell option, read client-side
    client_id = str(settings.get("client_id") or "").strip()
    client_secret = str(settings.get("client_secret") or "").strip()
    zip_code = str(settings.get("zip") or "").strip()

    if not client_id or not client_secret:
        return {"error": "Add your Ostrom Client ID and Secret in the plugin settings."}

    zone = app_timezone()
    now = datetime.now(zone)
    today = now.date()

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    prices_path = data_dir / f"prices_{CACHE_SCHEMA}_{zip_code or 'none'}_{today.isoformat()}.json"

    cached = _read_cache(prices_path, PRICES_CACHE_TTL_S)
    if _cache_ok(cached):
        # Recompute the "now" pointer on cache hit so the highlight tracks the
        # current hour even when the price data itself is unchanged.
        return _reslice_now(cached, zone, now)

    # Cold cache: serialize concurrent callers (see _fetch_lock) so only one
    # of them hits the network; everyone else blocks here and then re-reads
    # whatever the winner (or a cooldown from a just-failed winner) left
    # behind, instead of independently repeating the same request.
    with _fetch_lock:
        cached = _read_cache(prices_path, PRICES_CACHE_TTL_S)
        if _cache_ok(cached):
            return _reslice_now(cached, zone, now)

        # A prior failure (bad credentials, rate limit, upstream outage) is
        # remembered for a cooldown window so a host that calls fetch() more
        # often than the price cache's 10-min TTL (e.g. a live settings-editor
        # preview) can't retry a doomed request every time and keep
        # re-tripping Ostrom's per-minute rate limit.
        cooldown_path = data_dir / "cooldown.json"
        cooldown_error = _active_cooldown(cooldown_path)
        if cooldown_error is not None:
            return {"error": cooldown_error}

        token_path = data_dir / "token.json"
        try:
            token = _access_token(client_id, client_secret, token_path)
        except urllib.error.HTTPError as err:
            if err.code in (400, 401, 403):
                message = "Check your Ostrom Client ID and Secret."
                _start_cooldown(cooldown_path, message, seconds=FAILURE_COOLDOWN_S)
                return {"error": message}
            detail = _http_detail(err)
            logger.warning("ostrom_prices: token exchange failed: %s", detail)
            message = f"Ostrom sign-in failed ({detail})."
            _start_cooldown(
                cooldown_path, message, seconds=_retry_after_s(err) or FAILURE_COOLDOWN_S
            )
            return {"error": message}
        except Exception as err:
            logger.warning("ostrom_prices: token exchange error: %r", err)
            message = f"Couldn't reach Ostrom: {type(err).__name__}: {err}"
            _start_cooldown(cooldown_path, message, seconds=FAILURE_COOLDOWN_S)
            return {"error": message}

        start = local_midnight_utc(today - timedelta(days=HISTORY_DAYS), zone)
        end = local_midnight_utc(today + timedelta(days=1), zone)
        query = {
            "startDate": _iso_utc(start),
            "endDate": _iso_utc(end),
            "resolution": "HOUR",
        }
        if zip_code:
            query["zip"] = zip_code
        url = f"{PRICES_URL}?{urllib.parse.urlencode(query)}"

        try:
            payload = fetch_json(
                url,
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                timeout=HTTP_TIMEOUT_S,
                retries=0,
            )
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                # Token may have gone stale under us; drop it so the next
                # render re-exchanges instead of serving the same bad token
                # from cache.
                with contextlib.suppress(OSError):
                    token_path.unlink()
                message = "Check your Ostrom Client ID and Secret."
                _start_cooldown(cooldown_path, message, seconds=FAILURE_COOLDOWN_S)
                return {"error": message}
            detail = _http_detail(err)
            logger.warning("ostrom_prices: spot-prices failed: %s", detail)
            message = f"Ostrom prices request failed ({detail})."
            _start_cooldown(
                cooldown_path, message, seconds=_retry_after_s(err) or FAILURE_COOLDOWN_S
            )
            return {"error": message}
        except Exception as err:
            logger.warning("ostrom_prices: spot-prices error: %r", err)
            message = f"Couldn't load Ostrom prices: {type(err).__name__}: {err}"
            _start_cooldown(cooldown_path, message, seconds=FAILURE_COOLDOWN_S)
            return {"error": message}

        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            message = "Ostrom returned an unexpected response."
            _start_cooldown(cooldown_path, message, seconds=FAILURE_COOLDOWN_S)
            return {"error": message}

        result = _shape(rows, zone, today, now)
        if "error" not in result:
            _write_cache(prices_path, result)
        return result


def _reslice_now(cached: dict[str, Any], zone: Any, now: datetime) -> dict[str, Any]:
    """Refresh nowIndex/now on a cache hit from the stored ``hours`` list."""
    hours = cached.get("hours")
    today = cached.get("today")
    if not isinstance(hours, list) or not isinstance(today, list):
        return cached
    now_hour = now.astimezone(zone).hour
    now_index = hours.index(now_hour) if now_hour in hours else -1
    updated = dict(cached)
    updated["nowIndex"] = now_index
    updated["now"] = today[now_index] if 0 <= now_index < len(today) else None
    return updated
