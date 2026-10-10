"""ostrom_prices smoke test, parametrized over sizes.

Mocks all three network hops (OAuth token POST, the spot-prices GET, then the
Open-Meteo weather GET) — all go through ``urllib.request.urlopen`` (the token
POST directly, the GETs via ``app.plugin_http.fetch_json``), so a single patch
with an ordered ``side_effect`` covers them. Each test gets a fresh ``tmp_path`` data root (see
the root conftest), so the on-disk token/price caches never bleed between runs.
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

PLUGIN_ID = "ostrom_prices"


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *a: object) -> bool:
        return False


def _skip_if_not_loaded(app: Flask) -> None:
    if app.config["PLUGIN_REGISTRY"].get(PLUGIN_ID) is None:
        pytest.skip(f"{PLUGIN_ID!r} not loaded")


def _token_body() -> bytes:
    return json.dumps(
        {"access_token": "tok-123", "token_type": "Bearer", "expires_in": 3600}
    ).encode("utf-8")


def _price(day_offset: int, hour: int) -> float:
    return 25.0 + hour * 0.5 + day_offset * 0.1


def _prices_body(*, days_back: int = 28, tomorrow: bool = False) -> bytes:
    # today + the prior weeks of hourly rows, so the p25/p75 band and the
    # outlook model have history to work from. UTC-midnight rows bucket
    # cleanly into local hours under the pinned Europe/Berlin tz.
    rows = []
    for day_offset in range(-days_back, 2 if tomorrow else 1):
        day = (date.today() + timedelta(days=day_offset)).isoformat()
        for hour in range(24):
            gross = _price(day_offset, hour)
            rows.append(
                {
                    "date": f"{day}T{hour:02d}:00:00.000Z",
                    "netKwhPrice": gross - 5,
                    "grossKwhPrice": gross,
                }
            )
    return json.dumps({"data": rows}).encode("utf-8")


def _weather_body() -> bytes:
    """Open-Meteo multi-location shape: a list, one entry per point."""
    times = []
    wind = []
    solar = []
    for day_offset in range(-29, 6):
        day = date.today() + timedelta(days=day_offset)
        for hour in range(24):
            times.append(f"{day.isoformat()}T{hour:02d}:00")
            wind.append(float((day_offset * 7 + hour) % 15))
            solar.append(max(0.0, 600.0 - abs(hour - 12) * 100.0))
    loc = {"hourly": {"time": times, "wind_speed_100m": wind, "shortwave_radiation": solar}}
    return json.dumps([loc, loc]).encode("utf-8")


def _responses() -> list[_FakeResp]:
    return [_FakeResp(_token_body()), _FakeResp(_prices_body()), _FakeResp(_weather_body())]


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_renders(app: Flask, client: FlaskClient, size: str) -> None:
    _skip_if_not_loaded(app)

    with app.app_context():
        store = app.config["SETTINGS_STORE"]
        store.update_section("app", {"timezone": "Europe/Berlin"})  # deterministic bucketing
        store.update_section(
            "plugins",
            {
                PLUGIN_ID: {
                    "client_id_secret": "cid",
                    "client_secret_secret": "csec",
                    "zip": "10115",
                }
            },
        )

    with patch("urllib.request.urlopen", side_effect=_responses()):
        resp = client.get(f"/_test/render?plugin={PLUGIN_ID}&size={size}")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'data-plugin="{PLUGIN_ID}"' in body
    # The server payload (ctx.data) is embedded in the page; the client builds
    # the canvas in-browser. Assert on values only the happy path produces,
    # including the percentile band computed from the prior week.
    assert "ct/kWh" in body
    assert '"nowIndex"' in body
    assert '"hasBand"' in body
    assert '"p25"' in body and '"p75"' in body
    assert '"outlook"' in body and '"forecast"' in body


def test_missing_credentials_shows_friendly_error(app: Flask, client: FlaskClient) -> None:
    _skip_if_not_loaded(app)

    with app.app_context():
        store = app.config["SETTINGS_STORE"]
        store.update_section("plugins", {PLUGIN_ID: {"zip": "10115"}})

    resp = client.get(f"/_test/render?plugin={PLUGIN_ID}&size=md")
    assert resp.status_code == 200
    assert "Client ID and Secret" in resp.get_data(as_text=True)


def test_stale_old_schema_cache_is_ignored(app: Flask, tmp_path) -> None:
    """A cache file from an older plugin version (here the pre-band shape
    with ``prices`` as a list) must not be served — that produced an empty
    "No price data" cell. fetch() should skip it and refetch."""
    _skip_if_not_loaded(app)
    from importlib import import_module

    srv = import_module("plugins.ostrom_prices.server")

    data_dir = tmp_path / "pdir"
    data_dir.mkdir()
    # Old-schema entry at BOTH the old and versioned paths.
    today = date.today().isoformat()
    old = {"labels": ["00", "01"], "prices": [25.0, 26.0], "unit": "ct/kWh"}
    (data_dir / f"prices_10115_{today}.json").write_text(json.dumps(old))
    (data_dir / f"prices_{srv.CACHE_SCHEMA}_10115_{today}.json").write_text(json.dumps(old))

    with app.app_context():
        app.config["SETTINGS_STORE"].update_section("app", {"timezone": "Europe/Berlin"})
        with patch("urllib.request.urlopen", side_effect=_responses()):
            result = srv.fetch(
                {},
                {"client_id": "cid", "client_secret": "csec", "zip": "10115"},
                ctx={"data_dir": str(data_dir), "panel_w": 800, "panel_h": 480, "preview": False},
            )

    assert "error" not in result, result
    assert isinstance(result.get("today"), list) and len(result["today"]) >= 2
    assert result.get("hasBand") is True


def _seed_caches(srv, data_dir, *, fetched_ago_s: float, has_tomorrow: bool) -> Path:
    """Write today's price cache (no tomorrow rows) + a fresh weather cache."""
    zone = srv.app_timezone()
    today = datetime.now(zone).date()
    prices = {}
    for d in range(-28, 1):
        for h in range(24):
            local = datetime.combine(today + timedelta(days=d), datetime.min.time(), tzinfo=zone)
            prices[srv._hour_key(local + timedelta(hours=h))] = _price(d, h)
    cache_path = data_dir / f"prices_{srv.CACHE_SCHEMA}_10115_{today.isoformat()}.json"
    cache_path.write_text(
        json.dumps(
            {
                "prices": prices,
                "fetched_at": time.time() - fetched_ago_s,
                "has_tomorrow": has_tomorrow,
            }
        )
    )
    weather = {}
    for d in range(-29, 6):
        for h in range(24):
            local = datetime.combine(today + timedelta(days=d), datetime.min.time(), tzinfo=zone)
            weather[srv._hour_key(local + timedelta(hours=h))] = [((d + h) % 5) / 5, 0.1]
    (data_dir / "weather_v1.json").write_text(json.dumps({"hours": weather}))
    return cache_path


def test_price_cache_lasts_the_whole_day(app: Flask, tmp_path) -> None:
    """Day-ahead prices never change once published, so once today's cache
    holds tomorrow's prices it is served no matter how old — only the
    now/nowIndex pointer and the outlook get recomputed, never a fresh
    network fetch."""
    _skip_if_not_loaded(app)
    from importlib import import_module

    srv = import_module("plugins.ostrom_prices.server")

    data_dir = tmp_path / "pdir"
    data_dir.mkdir()

    def fail_if_called(*a, **kw):
        pytest.fail("urlopen called even though today's caches were still valid")

    with app.app_context():
        app.config["SETTINGS_STORE"].update_section("app", {"timezone": "Europe/Berlin"})
        _seed_caches(srv, data_dir, fetched_ago_s=6 * 3600, has_tomorrow=True)
        with patch("urllib.request.urlopen", side_effect=fail_if_called):
            result = srv.fetch(
                {},
                {"client_id": "cid", "client_secret": "csec", "zip": "10115"},
                ctx={"data_dir": str(data_dir), "panel_w": 800, "panel_h": 480, "preview": False},
            )

    assert "error" not in result, result
    assert result["today"] == [_price(0, h) for h in range(24)]
    # now/nowIndex recomputed from the current local hour.
    now_hour = datetime.now(srv.app_timezone()).hour
    assert result["nowIndex"] == now_hour
    assert result["now"] == _price(0, now_hour)


def test_afternoon_refetches_until_tomorrow_is_published(app: Flask, tmp_path) -> None:
    """Before the auction result (~13:00) today's cache is good as is; after
    it, a cache without tomorrow is retried every TOMORROW_RETRY_S, and a
    failed retry falls back to the stale cache instead of an error."""
    _skip_if_not_loaded(app)
    from importlib import import_module

    srv = import_module("plugins.ostrom_prices.server")
    data_dir = tmp_path / "pdir"
    data_dir.mkdir()
    settings = {"client_id": "cid", "client_secret": "csec", "zip": "10115"}

    with app.app_context():
        app.config["SETTINGS_STORE"].update_section("app", {"timezone": "Europe/Berlin"})
        zone = srv.app_timezone()
        today = datetime.now(zone).date()
        morning = datetime.combine(today, datetime.min.time(), tzinfo=zone).replace(hour=10)
        afternoon = morning.replace(hour=15)
        _seed_caches(srv, data_dir, fetched_ago_s=2 * 3600, has_tomorrow=False)

        with patch("urllib.request.urlopen", side_effect=AssertionError("no fetch expected")):
            assert isinstance(srv._load_prices(settings, zone, morning, data_dir), dict)

        down = urllib.error.HTTPError(srv.AUTH_URL, 503, "down", {}, io.BytesIO(b""))
        with patch("urllib.request.urlopen", side_effect=down) as mock:
            stale = srv._load_prices(settings, zone, afternoon, data_dir)
        assert mock.call_count == 1
        assert isinstance(stale, dict) and stale  # stale cache, not an error

        (data_dir / "cooldown.json").unlink()
        body = _prices_body(tomorrow=True)
        with patch(
            "urllib.request.urlopen", side_effect=[_FakeResp(_token_body()), _FakeResp(body)]
        ):
            fresh = srv._load_prices(settings, zone, afternoon, data_dir)
        assert isinstance(fresh, dict)
        cached = json.loads(
            (data_dir / f"prices_{srv.CACHE_SCHEMA}_10115_{today.isoformat()}.json").read_text()
        )
        assert cached["has_tomorrow"] is True


def test_outlook_uses_real_tomorrow_then_forecast(app: Flask, tmp_path) -> None:
    """Tomorrow's published prices are shown as real; later days come from
    the weather model, which must recover a clean synthetic relation
    (price = 30 - 10 * wind) closely."""
    _skip_if_not_loaded(app)
    from importlib import import_module

    srv = import_module("plugins.ostrom_prices.server")
    with app.app_context():
        app.config["SETTINGS_STORE"].update_section("app", {"timezone": "Europe/Berlin"})
        zone = srv.app_timezone()
    today = datetime.now(zone).date()
    now = datetime.combine(today, datetime.min.time(), tzinfo=zone).replace(hour=8)

    def wind(key: str) -> float:
        return (int(key[8:10]) * 3 + int(key[11:13])) % 7 / 7

    weather = {}
    prices = {}
    for d in range(-28, 5):
        for h in range(24):
            key = srv._hour_key(
                datetime.combine(today + timedelta(days=d), datetime.min.time(), tzinfo=zone)
                + timedelta(hours=h)
            )
            weather[key] = [wind(key), 0.0]
            if d <= 1:
                prices[key] = round(30 - 10 * wind(key), 2)

    result = srv._shape(prices, weather, zone, now)

    outlook = result["outlook"]
    assert [d["kind"] for d in outlook] == ["actual", "forecast", "forecast", "forecast"]
    for d in outlook[1:]:
        day = date.fromisoformat(d["date"])
        for h, got in enumerate(d["prices"]):
            key = srv._hour_key(
                datetime.combine(day, datetime.min.time(), tzinfo=zone) + timedelta(hours=h)
            )
            assert got == pytest.approx(30 - 10 * wind(key), abs=0.05)
        assert d["best"]["avg"] <= d["avg"]
    candidates = [result["todayBest"]] + [d["best"] for d in outlook]
    assert result["bestDay"] in [today.isoformat()] + [d["date"] for d in outlook]
    best_avg = min(c["avg"] for c in candidates if c)
    picked = (
        result["todayBest"]
        if result["bestDay"] == today.isoformat()
        else next(d["best"] for d in outlook if d["date"] == result["bestDay"])
    )
    assert picked["avg"] == best_avg


def test_weather_outage_still_renders_real_prices(app: Flask, tmp_path) -> None:
    """Open-Meteo down: today + tomorrow still render, the outlook just stops
    at the last real day, and the failure isn't retried every render."""
    _skip_if_not_loaded(app)
    from importlib import import_module

    srv = import_module("plugins.ostrom_prices.server")
    data_dir = tmp_path / "pdir"
    data_dir.mkdir()
    settings = {"client_id": "cid", "client_secret": "csec", "zip": "10115"}
    ctx = {"data_dir": str(data_dir), "panel_w": 800, "panel_h": 480, "preview": False}

    with app.app_context():
        app.config["SETTINGS_STORE"].update_section("app", {"timezone": "Europe/Berlin"})
        with patch(
            "urllib.request.urlopen",
            side_effect=[
                _FakeResp(_token_body()),
                _FakeResp(_prices_body(tomorrow=True)),
                OSError("open-meteo down"),
            ],
        ):
            first = srv.fetch({}, settings, ctx=ctx)
            second = srv.fetch({}, settings, ctx=ctx)  # would StopIteration if it refetched

    assert "error" not in first, first
    assert [d["kind"] for d in first["outlook"]] == ["actual"]
    assert second["outlook"] == first["outlook"]


def test_cooldown_prevents_hammering_after_rate_limit(app: Flask, tmp_path) -> None:
    """A 429 from Ostrom must not be retried on every subsequent fetch() call
    within the cooldown window — that's exactly what turns a single rate-limit
    response into a sustained lockout (each retry is itself another request
    against the same per-minute limit)."""
    _skip_if_not_loaded(app)
    from importlib import import_module

    srv = import_module("plugins.ostrom_prices.server")

    data_dir = tmp_path / "pdir"
    data_dir.mkdir()
    settings = {"client_id": "cid", "client_secret": "csec", "zip": "10115"}
    ctx = {"data_dir": str(data_dir), "panel_w": 800, "panel_h": 480, "preview": False}

    calls = {"n": 0}

    def fake_urlopen(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                srv.AUTH_URL,
                429,
                "Too Many Requests",
                {"Retry-After": "120"},
                io.BytesIO(b'{"type":"too_many_requests","detail":"API request limit reached"}'),
            )
        pytest.fail("urlopen called again while a cooldown from the prior 429 was active")

    with app.app_context():
        app.config["SETTINGS_STORE"].update_section("app", {"timezone": "Europe/Berlin"})
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            first = srv.fetch({}, settings, ctx=ctx)
            second = srv.fetch({}, settings, ctx=ctx)

    assert "429" in first["error"]
    assert first["error"] == second["error"]
    assert calls["n"] == 1


def test_concurrent_cold_cache_fetches_only_once(app: Flask, tmp_path) -> None:
    """The composer fetches every cell's data on its own thread, all launched
    together, so a cold price cache can be hit by several threads at the same
    instant. Without the in-process lock each thread independently repeats
    the OAuth exchange + prices request — enough on its own to trip a
    per-minute rate limit even with correct on-disk caching."""
    _skip_if_not_loaded(app)
    from importlib import import_module

    srv = import_module("plugins.ostrom_prices.server")

    data_dir = tmp_path / "pdir"
    data_dir.mkdir()
    settings = {"client_id": "cid", "client_secret": "csec", "zip": "10115"}
    ctx = {"data_dir": str(data_dir), "panel_w": 800, "panel_h": 480, "preview": False}

    calls = {"n": 0}
    counter_lock = threading.Lock()

    def fake_urlopen(req, timeout=None):
        with counter_lock:
            calls["n"] += 1
        if "open-meteo" in req.full_url:
            return _FakeResp(_weather_body())
        if "oauth2/token" in req.full_url:
            # Widen the race window so a second thread's initial (unlocked)
            # cache check has time to also observe a miss before the winner
            # finishes and writes the cache.
            time.sleep(0.05)
            return _FakeResp(_token_body())
        return _FakeResp(_prices_body())

    with app.app_context():
        app.config["SETTINGS_STORE"].update_section("app", {"timezone": "Europe/Berlin"})
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            results: list[dict | None] = [None, None]

            def worker(i: int) -> None:
                results[i] = srv.fetch({}, settings, ctx=ctx)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

    assert "error" not in results[0], results[0]
    assert "error" not in results[1], results[1]
    # One token exchange + one prices GET + one weather GET total, not a set
    # per thread.
    assert calls["n"] == 3
