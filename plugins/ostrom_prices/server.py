"""Electricity Price (ostrom_prices) server-side fetch.

Pulls hourly day-ahead spot prices from the Ostrom API and returns them as
parallel arrays for the client's chart. The gross price (``grossKwhPrice``,
cents per kWh, incl. tax + levies) is what we chart.

Ostrom needs two round trips:
  1. OAuth2 client-credentials token exchange (POST, Basic auth). No in-repo
     helper does this and ``fetch_json`` is GET-only, so we POST with
     ``urllib.request`` directly (mirrors ha_core's POST).
  2. GET /spot-prices for the window [today - TRAIN_DAYS, tomorrow end].

Beyond tomorrow no market publishes hourly prices, so the multi-day outlook
is a forecast: a linear model of price ~ wind power + solar irradiance +
weekend + hour-of-day, fitted on the prior ``TRAIN_DAYS`` of Ostrom prices
against Open-Meteo weather for the same hours, then applied to Open-Meteo's
forecast. Wind and sun drive most of the German day-ahead price swing, so
this tells cheap days from dear ones well enough to plan around (backtested
on 2026 DE-LU prices: daily mean off by ~1.8 ct/kWh, cheapest of the next
5 days picked right ~3 in 4 times).

Both responses are disk-cached: the token until its own expiry, prices per
calendar day (refetched in the afternoon until tomorrow's auction result
shows up), weather for a few hours. Warm renders inside the composer's ~6s
budget make zero network calls; the model fit and the "now" pointer are
recomputed per render from the cached inputs (sub-millisecond).

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
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import numpy as np

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


HTTP_TIMEOUT_S = 6  # short-fail so a slow upstream can't blow the ~6s render budget
USER_AGENT = "tesserae/0.1 (+ostrom_prices)"

# How many days before today the p25/p75 comparison band covers. The range is
# computed per hour-of-day across these prior days, so today's line can be
# read as high/low vs. the recent norm.
HISTORY_DAYS = 7

# How many days of past prices the outlook model is fitted on. Long enough
# to average out a single odd day, short enough to track gas/CO2 price drift.
# The same single prices request covers it, so this doesn't add a round trip.
TRAIN_DAYS = 28
# Below this many paired (price, weather) hours the fit is too noisy to show.
MIN_TRAIN_HOURS = 7 * 24

# Days after today shown in the outlook (tomorrow is real once published).
OUTLOOK_DAYS = 4

# Length of the "cheapest window" each day is summarised by, roughly one
# washing-machine or dishwasher cycle.
WINDOW_HOURS = 3

# The day-ahead auction result is published around 12:45 CET. Until
# tomorrow's prices are in the cache, refetch at most this often after that.
TOMORROW_PUBLISH_HOUR = 13
TOMORROW_RETRY_S = 30 * 60

# Weather forecast cache lifetime; Open-Meteo's models update every few hours.
WEATHER_TTL_S = 3 * 3600
WEATHER_FAILURE_COOLDOWN_S = 10 * 60

# Bumped whenever the cached result's shape changes, so a file written by an
# older version of this plugin is ignored rather than served with missing
# fields. Old files orphan harmlessly and rotate out daily.
CACHE_SCHEMA = "v3"

AUTH_URL = "https://auth.production.ostrom-api.io/oauth2/token"
PRICES_URL = "https://production.ostrom-api.io/spot-prices"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# Points spread over Germany's wind belt (north coast / plains) and the
# solar-heavy south, averaged to a national wind/solar signal. Germany sets
# the DE-LU price, so the user's own location doesn't matter here.
WEATHER_POINTS = (
    (53.6, 8.0),
    (54.3, 10.0),
    (53.5, 12.5),
    (52.4, 13.5),
    (51.8, 7.8),
    (51.0, 10.5),
    (49.5, 8.7),
    (48.5, 11.5),
)

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


def _hour_key(dt: datetime) -> str:
    """UTC hour key shared by price rows and weather rows."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H")


def _read_prices_cache(path: Path) -> dict[str, Any] | None:
    """The price-cache entry if it has the current schema, else None."""
    cached = _read_cache(path, float("inf"))
    if isinstance(cached, dict) and isinstance(cached.get("prices"), dict):
        return cached
    return None


def _read_weather_cache(path: Path) -> dict[str, list[float]] | None:
    cached = _read_cache(path, WEATHER_TTL_S)
    if isinstance(cached, dict) and isinstance(cached.get("hours"), dict):
        hours: dict[str, list[float]] = cached["hours"]
        return hours
    return None


def _prices_fresh(cached: dict[str, Any], now: datetime) -> bool:
    """Today's file is good for the whole day once it holds tomorrow's prices.
    Before the auction publishes, it's good until early afternoon; after
    that, retry every TOMORROW_RETRY_S until tomorrow shows up."""
    if cached.get("has_tomorrow") or now.hour < TOMORROW_PUBLISH_HOUR:
        return True
    fetched_at = cached.get("fetched_at")
    return isinstance(fetched_at, int | float) and time.time() - fetched_at < TOMORROW_RETRY_S


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
_weather_lock = threading.Lock()


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


def _request_prices(
    client_id: str, client_secret: str, zip_code: str, zone: tzinfo, today: date, data_dir: Path
) -> dict[str, float] | str:
    """One token exchange (if needed) + one spot-prices GET covering the
    training window through tomorrow. Returns {utc hour key: gross ct/kWh},
    or a friendly error string (with a cooldown already started)."""
    cooldown_path = data_dir / "cooldown.json"
    token_path = data_dir / "token.json"
    try:
        token = _access_token(client_id, client_secret, token_path)
    except urllib.error.HTTPError as err:
        if err.code in (400, 401, 403):
            message = "Check your Ostrom Client ID and Secret."
            _start_cooldown(cooldown_path, message, seconds=FAILURE_COOLDOWN_S)
            return message
        detail = _http_detail(err)
        logger.warning("ostrom_prices: token exchange failed: %s", detail)
        message = f"Ostrom sign-in failed ({detail})."
        _start_cooldown(cooldown_path, message, seconds=_retry_after_s(err) or FAILURE_COOLDOWN_S)
        return message
    except Exception as err:
        logger.warning("ostrom_prices: token exchange error: %r", err)
        message = f"Couldn't reach Ostrom: {type(err).__name__}: {err}"
        _start_cooldown(cooldown_path, message, seconds=FAILURE_COOLDOWN_S)
        return message

    start = local_midnight_utc(today - timedelta(days=TRAIN_DAYS), zone)
    end = local_midnight_utc(today + timedelta(days=2), zone)
    query = {"startDate": _iso_utc(start), "endDate": _iso_utc(end), "resolution": "HOUR"}
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
            # Token may have gone stale under us; drop it so the next render
            # re-exchanges instead of serving the same bad token from cache.
            with contextlib.suppress(OSError):
                token_path.unlink()
            message = "Check your Ostrom Client ID and Secret."
            _start_cooldown(cooldown_path, message, seconds=FAILURE_COOLDOWN_S)
            return message
        detail = _http_detail(err)
        logger.warning("ostrom_prices: spot-prices failed: %s", detail)
        message = f"Ostrom prices request failed ({detail})."
        _start_cooldown(cooldown_path, message, seconds=_retry_after_s(err) or FAILURE_COOLDOWN_S)
        return message
    except Exception as err:
        logger.warning("ostrom_prices: spot-prices error: %r", err)
        message = f"Couldn't load Ostrom prices: {type(err).__name__}: {err}"
        _start_cooldown(cooldown_path, message, seconds=FAILURE_COOLDOWN_S)
        return message

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        message = "Ostrom returned an unexpected response."
        _start_cooldown(cooldown_path, message, seconds=FAILURE_COOLDOWN_S)
        return message

    prices: dict[str, float] = {}
    for row in rows:
        iso = row.get("date") if isinstance(row, dict) else None
        gross = row.get("grossKwhPrice") if isinstance(row, dict) else None
        if not isinstance(iso, str) or not isinstance(gross, int | float):
            continue
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        prices[_hour_key(dt)] = round(float(gross), 2)
    return prices


def _load_prices(
    settings: dict[str, Any], zone: tzinfo, now: datetime, data_dir: Path
) -> dict[str, float] | str:
    """Cached prices map, refetched per the _prices_fresh policy. A failed
    refresh falls back to the stale same-day cache rather than an error."""
    client_id = str(settings.get("client_id") or "").strip()
    client_secret = str(settings.get("client_secret") or "").strip()
    zip_code = str(settings.get("zip") or "").strip()
    today = now.date()
    prices_path = data_dir / f"prices_{CACHE_SCHEMA}_{zip_code or 'none'}_{today.isoformat()}.json"

    cached = _read_prices_cache(prices_path)
    if cached is not None and _prices_fresh(cached, now):
        prices: dict[str, float] = cached["prices"]
        return prices

    # Cold cache: serialize concurrent callers (see _fetch_lock) so only one
    # of them hits the network; everyone else blocks here and then re-reads
    # whatever the winner (or a cooldown from a just-failed winner) left.
    with _fetch_lock:
        cached = _read_prices_cache(prices_path)
        stale: dict[str, float] | None = cached["prices"] if cached is not None else None
        if cached is not None and stale is not None and _prices_fresh(cached, now):
            return stale

        # A prior failure (bad credentials, rate limit, upstream outage) is
        # remembered for a cooldown window so a host that calls fetch() often
        # (e.g. a live settings-editor preview) can't retry a doomed request
        # every time and keep re-tripping Ostrom's per-minute rate limit.
        cooldown_error = _active_cooldown(data_dir / "cooldown.json")
        if cooldown_error is not None:
            return stale if stale is not None else cooldown_error

        fetched = _request_prices(client_id, client_secret, zip_code, zone, today, data_dir)
        if isinstance(fetched, str):
            return stale if stale is not None else fetched

        tomorrow = today + timedelta(days=1)
        has_tomorrow = (
            sum(
                1
                for k in fetched
                if datetime.strptime(k, "%Y-%m-%dT%H").replace(tzinfo=UTC).astimezone(zone).date()
                == tomorrow
            )
            >= 20
        )
        _write_cache(
            prices_path,
            {"prices": fetched, "fetched_at": time.time(), "has_tomorrow": has_tomorrow},
        )
        return fetched


def _wind_power(speed_ms: float) -> float:
    """Rough normalised turbine output for a 100 m wind speed: nothing below
    cut-in (~3 m/s), roughly cubic ramp, flat at rated (~12 m/s)."""
    return min(1.0, max(0.0, (speed_ms - 3.0) / 9.0)) ** 2


def _load_weather(data_dir: Path) -> dict[str, list[float]] | None:
    """{utc hour key: [wind power 0..1, solar W/m² / 1000]} averaged over
    WEATHER_POINTS, covering the training window plus the forecast days.
    None when Open-Meteo is unavailable; the outlook then shows real
    prices only."""
    path = data_dir / "weather_v1.json"
    cached = _read_weather_cache(path)
    if cached is not None:
        return cached
    with _weather_lock:  # same cold-cache stampede as _fetch_lock guards
        cached = _read_weather_cache(path)
        if cached is not None:
            return cached
        return _request_weather(path, data_dir)


def _request_weather(path: Path, data_dir: Path) -> dict[str, list[float]] | None:
    fail_path = data_dir / "weather_failed.json"
    if _read_cache(fail_path, WEATHER_FAILURE_COOLDOWN_S) is not None:
        return None

    query = {
        "latitude": ",".join(str(lat) for lat, _ in WEATHER_POINTS),
        "longitude": ",".join(str(lon) for _, lon in WEATHER_POINTS),
        "hourly": "wind_speed_100m,shortwave_radiation",
        "wind_speed_unit": "ms",
        "timezone": "GMT",
        "past_days": TRAIN_DAYS + 1,
        "forecast_days": OUTLOOK_DAYS + 2,
    }
    try:
        payload = fetch_json(
            f"{WEATHER_URL}?{urllib.parse.urlencode(query)}",
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT_S,
            retries=0,
        )
        locations = payload if isinstance(payload, list) else [payload]
        sums: dict[str, list[float]] = {}
        counts: dict[str, int] = {}
        for loc in locations:
            hourly = loc["hourly"]
            for t, ws, sr in zip(
                hourly["time"],
                hourly["wind_speed_100m"],
                hourly["shortwave_radiation"],
                strict=True,
            ):
                if ws is None or sr is None:
                    continue
                key = t[:13]  # "2026-10-10T14:00" (GMT) -> "2026-10-10T14"
                acc = sums.setdefault(key, [0.0, 0.0])
                acc[0] += _wind_power(float(ws))
                acc[1] += float(sr) / 1000.0
                counts[key] = counts.get(key, 0) + 1
        hours = {
            k: [round(v[0] / counts[k], 4), round(v[1] / counts[k], 4)] for k, v in sums.items()
        }
    except Exception as err:
        logger.warning("ostrom_prices: weather fetch failed: %r", err)
        _write_cache(fail_path, {"at": time.time()})
        return None
    if not hours:
        return None
    _write_cache(path, {"hours": hours})
    return hours


def _features(key: str, wx: list[float], zone: tzinfo) -> list[float]:
    local = datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=UTC).astimezone(zone)
    hod = [0.0] * 24
    hod[local.hour] = 1.0
    return [wx[0], wx[1], 1.0 if local.weekday() >= 5 else 0.0, *hod]


def _forecast(
    prices: dict[str, float], weather: dict[str, list[float]] | None, zone: tzinfo
) -> dict[str, float]:
    """Model prices for every weather hour that has no real price yet.
    Empty when there's no weather or too little paired history to fit."""
    if not weather:
        return {}
    train = [k for k in prices if k in weather]
    if len(train) < MIN_TRAIN_HOURS:
        return {}
    x = np.array([_features(k, weather[k], zone) for k in train])
    y = np.array([prices[k] for k in train])
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    last_real = max(prices)
    todo = sorted(k for k in weather if k > last_real)
    if not todo:
        return {}
    pred = np.array([_features(k, weather[k], zone) for k in todo]) @ coef
    return {k: round(float(v), 2) for k, v in zip(todo, pred, strict=True)}


def _by_local_day(series: dict[str, float], zone: tzinfo) -> dict[date, dict[int, float]]:
    out: dict[date, dict[int, float]] = {}
    for key, val in series.items():
        local = datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=UTC).astimezone(zone)
        out.setdefault(local.date(), {})[local.hour] = val
    return out


def _cheapest_window(by_hour: dict[int, float], from_hour: int = 0) -> dict[str, Any] | None:
    """Cheapest run of WINDOW_HOURS consecutive hours starting at or after
    ``from_hour``: {"start": hour, "avg": ct}."""
    best: dict[str, Any] | None = None
    for start in range(from_hour, 24 - WINDOW_HOURS + 1):
        vals = [by_hour.get(h) for h in range(start, start + WINDOW_HOURS)]
        if any(v is None for v in vals):
            continue
        avg = round(sum(vals) / WINDOW_HOURS, 2)  # type: ignore[arg-type]
        if best is None or avg < best["avg"]:
            best = {"start": start, "avg": avg}
    return best


def _shape(
    prices: dict[str, float],
    weather: dict[str, list[float]] | None,
    zone: tzinfo,
    now: datetime,
) -> dict[str, Any]:
    """Today's hourly price line, a per-hour-of-day p25/p75 comparison band
    over the prior ``HISTORY_DAYS``, and an outlook for the following days
    (real prices where published, modelled beyond).

    Prices are bucketed by *local* date/hour so the chart's x-axis and the
    "now" pointer land in the viewer's timezone regardless of the UTC dates
    Ostrom returns.
    """
    today = now.date()
    real = _by_local_day(prices, zone)
    today_by_hour = real.get(today, {})
    if not today_by_hour:
        return {"error": "Ostrom returned no prices for today."}

    hist_by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
    for d in range(1, HISTORY_DAYS + 1):
        for h, v in real.get(today - timedelta(days=d), {}).items():
            hist_by_hour[h].append(v)

    hours = sorted(today_by_hour)
    today_prices = [today_by_hour[h] for h in hours]
    p25 = [_pct(hist_by_hour[h], 25) for h in hours]
    p75 = [_pct(hist_by_hour[h], 75) for h in hours]
    has_band = any(v is not None for v in p25)

    modelled = _by_local_day(_forecast(prices, weather, zone), zone)
    outlook: list[dict[str, Any]] = []
    for offset in range(1, OUTLOOK_DAYS + 1):
        day = today + timedelta(days=offset)
        by_hour, kind = real.get(day, {}), "actual"
        if len(by_hour) < 20:
            by_hour, kind = modelled.get(day, {}), "forecast"
        if len(by_hour) < 20:
            break  # past the weather horizon; later days can't be any better
        outlook.append(
            {
                "date": day.isoformat(),
                "kind": kind,
                "prices": [by_hour.get(h) for h in range(24)],
                "avg": round(sum(by_hour.values()) / len(by_hour), 2),
                "best": _cheapest_window(by_hour),
            }
        )

    now_hour = now.astimezone(zone).hour
    now_index = hours.index(now_hour) if now_hour in hours else -1

    # Which day (today's remaining hours included) has the cheapest window.
    today_best = _cheapest_window(today_by_hour, from_hour=now_hour)
    candidates = [(today.isoformat(), today_best)] + [(d["date"], d["best"]) for d in outlook]
    candidates = [(d, b) for d, b in candidates if b is not None]
    best_day = min(candidates, key=lambda c: c[1]["avg"])[0] if candidates else None

    return {
        "date": today.isoformat(),
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
        "todayBest": today_best,
        "outlook": outlook,
        "bestDay": best_day,
        "windowHours": WINDOW_HOURS,
    }


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del options  # title / outlook are cell options, read client-side
    if (
        not str(settings.get("client_id") or "").strip()
        or not str(settings.get("client_secret") or "").strip()
    ):
        return {"error": "Add your Ostrom Client ID and Secret in the plugin settings."}

    zone = app_timezone()
    now = datetime.now(zone)
    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)

    prices = _load_prices(settings, zone, now, data_dir)
    if isinstance(prices, str):
        return {"error": prices}
    try:
        return _shape(prices, _load_weather(data_dir), zone, now)
    except Exception as err:  # the model must never take the whole cell down
        logger.warning("ostrom_prices: shaping failed: %r", err)
        return _shape(prices, None, zone, now)
