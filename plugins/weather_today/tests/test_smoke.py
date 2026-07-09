"""weather_today smoke: cell renders for every supported size, with today's
24 hourly slots (temperature / rain probability / UV index) threaded through
ctx.data."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from flask.testing import FlaskClient

# One calendar day of hourly slots (00:00–23:00); current.time falls at 09:00
# so nowIndex resolves to slot 9.
_FAKE_PAYLOAD = json.dumps(
    {
        "current": {
            "time": "2026-06-01T09:00",
            "temperature_2m": 14.0,
        },
        "hourly": {
            "time": [f"2026-06-01T{h:02d}:00" for h in range(24)],
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


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_weather_today_renders(client: FlaskClient, size: str) -> None:
    opts = '{"location":{"name":"Melbourne","latitude":-37.8136,"longitude":144.9631},"label":"Melbourne"}'
    with patch("urllib.request.urlopen", return_value=_FakeResp()):
        resp = client.get(f"/_test/render?plugin=weather_today&size={size}&opts={opts}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-plugin="weather_today"' in body
    assert "Melbourne" in body


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_weather_today_unconfigured_shows_hint(client: FlaskClient, size: str) -> None:
    resp = client.get(f"/_test/render?plugin=weather_today&size={size}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-plugin="weather_today"' in body
    assert "Pick a location" in body
