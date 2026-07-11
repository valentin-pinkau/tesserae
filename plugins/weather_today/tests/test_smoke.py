"""weather_today smoke: cell renders for every supported size, with today's
24 hourly slots (temperature / rain probability / UV index) threaded through
ctx.data. The "now" marker is computed from the wall clock (not the mocked
payload), so these tests only assert it's present, not which hour it lands
on; a separate test confirms it survives a cache hit without re-hitting the
network."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from flask.testing import FlaskClient

# One calendar day of hourly slots (00:00-23:00) in UTC (utc_offset_seconds:
# 0), dated "today" so the server's date-rollover check never treats this
# fixture as stale regardless of when the test runs.
_TODAY = datetime.now(UTC).strftime("%Y-%m-%d")
_FAKE_PAYLOAD = json.dumps(
    {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": [f"{_TODAY}T{h:02d}:00" for h in range(24)],
            "temperature_2m": [10 + (h % 12) for h in range(24)],
            "precipitation_probability": [(h * 5) % 100 for h in range(24)],
            "uv_index": [round((h - 6) * 0.8, 1) if 6 <= h <= 18 else 0.0 for h in range(24)],
        },
    }
).encode()


class _FakeResp:
    def read(self) -> bytes:
        return _FAKE_PAYLOAD

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


def _cell_data(body: str) -> dict:
    """client.js never runs in this harness — /_test/render just embeds
    fetch()'s return value as JSON for the browser to hydrate later — so
    inspect that payload directly rather than rendered markup."""
    m = re.search(r"data-data='(.*?)'", body, re.S)
    assert m, "no data-data attribute in rendered page"
    return json.loads(m.group(1))


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_weather_today_renders(client: FlaskClient, size: str) -> None:
    opts = '{"location":{"name":"Melbourne","latitude":-37.8136,"longitude":144.9631},"label":"Melbourne"}'
    with patch("urllib.request.urlopen", return_value=_FakeResp()):
        resp = client.get(f"/_test/render?plugin=weather_today&size={size}&opts={opts}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-plugin="weather_today"' in body
    assert "Melbourne" in body


def test_weather_today_cache_hit_skips_network_but_updates_now(
    client: FlaskClient,
) -> None:
    opts = '{"location":{"name":"Melbourne","latitude":-37.8136,"longitude":144.9631},"label":"Melbourne"}'
    with patch("urllib.request.urlopen", return_value=_FakeResp()):
        resp1 = client.get(f"/_test/render?plugin=weather_today&size=md&opts={opts}")
    assert resp1.status_code == 200
    data1 = _cell_data(resp1.get_data(as_text=True))
    assert data1["label"] == "Melbourne"
    # Every current UTC hour is present in the fixture's 00-23 slots, so
    # "now" always resolves to a real index — confirms _with_now() ran.
    assert data1["nowIndex"] is not None
    assert data1["nowTemp"] is not None

    # Second render within the 1h TTL must be served from cache — no network
    # call — yet still carry a live "now" marker (recomputed from the wall
    # clock on every call, not frozen from the first fetch).
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("cache hit should not touch the network")

    with patch("urllib.request.urlopen", side_effect=_boom):
        resp2 = client.get(f"/_test/render?plugin=weather_today&size=md&opts={opts}")
    assert resp2.status_code == 200
    data2 = _cell_data(resp2.get_data(as_text=True))
    assert data2["label"] == "Melbourne"
    assert data2["nowIndex"] is not None
    assert data2["nowTemp"] is not None


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_weather_today_unconfigured_shows_hint(client: FlaskClient, size: str) -> None:
    resp = client.get(f"/_test/render?plugin=weather_today&size={size}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-plugin="weather_today"' in body
    assert "Pick a location" in body
