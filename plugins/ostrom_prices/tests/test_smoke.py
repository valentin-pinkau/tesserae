"""ostrom_prices smoke test, parametrized over sizes.

Mocks both network hops (OAuth token POST, then the spot-prices GET) — both go
through ``urllib.request.urlopen`` (the token POST directly, the prices GET via
``app.plugin_http.fetch_json``), so a single patch with an ordered
``side_effect`` covers them. Each test gets a fresh ``tmp_path`` data root (see
the root conftest), so the on-disk token/price caches never bleed between runs.
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
from datetime import date, timedelta
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

    def __enter__(self) -> "_FakeResp":
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


def _prices_body() -> bytes:
    # today + the prior 7 days of hourly rows, so the p25/p75 comparison band
    # has history to compute from. UTC-midnight rows bucket cleanly into local
    # hours under the pinned Europe/Berlin tz.
    rows = []
    for day_offset in range(-7, 1):
        day = (date.today() + timedelta(days=day_offset)).isoformat()
        for hour in range(24):
            rows.append(
                {
                    "date": f"{day}T{hour:02d}:00:00.000Z",
                    "netKwhPrice": 20.0 + hour + day_offset,
                    "grossKwhPrice": 25.0 + hour + day_offset,
                }
            )
    return json.dumps({"data": rows}).encode("utf-8")


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

    with patch(
        "urllib.request.urlopen",
        side_effect=[_FakeResp(_token_body()), _FakeResp(_prices_body())],
    ):
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


def test_missing_credentials_shows_friendly_error(app: Flask, client: FlaskClient) -> None:
    _skip_if_not_loaded(app)

    with app.app_context():
        store = app.config["SETTINGS_STORE"]
        store.update_section("plugins", {PLUGIN_ID: {"zip": "10115"}})

    resp = client.get(f"/_test/render?plugin={PLUGIN_ID}&size=md")
    assert resp.status_code == 200
    assert "Client ID and Secret" in resp.get_data(as_text=True)


def test_stale_old_schema_cache_is_ignored(app: Flask, tmp_path) -> None:
    """A cache file from before the line+band rework (``prices``/no ``today``)
    must not be served — that produced an empty "No price data" cell. fetch()
    should skip it and refetch."""
    _skip_if_not_loaded(app)
    from importlib import import_module

    srv = import_module("plugins.ostrom_prices.server")

    data_dir = tmp_path / "pdir"
    data_dir.mkdir()
    # Old-schema entry at BOTH the old and versioned paths, well within TTL.
    today = date.today().isoformat()
    old = {"labels": ["00", "01"], "prices": [25.0, 26.0], "unit": "ct/kWh"}
    (data_dir / f"prices_10115_{today}.json").write_text(json.dumps(old))
    (data_dir / f"prices_{srv.CACHE_SCHEMA}_10115_{today}.json").write_text(json.dumps(old))

    with app.app_context():
        app.config["SETTINGS_STORE"].update_section("app", {"timezone": "Europe/Berlin"})
        with patch(
            "urllib.request.urlopen",
            side_effect=[_FakeResp(_token_body()), _FakeResp(_prices_body())],
        ):
            result = srv.fetch(
                {},
                {"client_id": "cid", "client_secret": "csec", "zip": "10115"},
                ctx={"data_dir": str(data_dir), "panel_w": 800, "panel_h": 480, "preview": False},
            )

    assert "error" not in result, result
    assert isinstance(result.get("today"), list) and len(result["today"]) >= 2
    assert result.get("hasBand") is True


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
    # One token exchange + one prices GET total, not one pair per thread.
    assert calls["n"] == 2
