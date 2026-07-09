"""bvg_departures smoke test, parametrized over sizes.

Mocks the single upstream hop (departures GET via
``app.plugin_http.fetch_json`` -> ``urllib.request.urlopen``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

PLUGIN_ID = "bvg_departures"


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


def _departures_body() -> bytes:
    now = datetime.now().astimezone()

    def iso(minutes: int) -> str:
        return (now + timedelta(minutes=minutes)).isoformat()

    departures = [
        {
            "line": {"name": "S1", "product": "suburban"},
            "direction": "S Wannsee Bhf (Berlin)",
            "when": iso(4),
            "plannedWhen": iso(4),
            "delay": None,
            "cancelled": False,
            "platform": "1",
        },
        {
            "line": {"name": "M43", "product": "bus"},
            "direction": "U Berliner Str.",
            "when": iso(11),
            "plannedWhen": iso(8),
            "delay": 180,
            "cancelled": False,
            "platform": None,
        },
        {
            "line": {"name": "S1", "product": "suburban"},
            "direction": "S Wannsee Bhf (Berlin)",
            "when": None,
            "plannedWhen": iso(-2),
            "delay": None,
            "cancelled": True,
            "platform": None,
        },
    ]
    return json.dumps({"departures": departures}).encode("utf-8")


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_renders(app: Flask, client: FlaskClient, size: str) -> None:
    _skip_if_not_loaded(app)

    with app.app_context():
        store = app.config["SETTINGS_STORE"]
        store.update_section(
            "plugins",
            {PLUGIN_ID: {"stop_id": "900057104", "stop_name": "S Julius-Leber-Brücke"}},
        )

    with patch("urllib.request.urlopen", side_effect=[_FakeResp(_departures_body())]):
        resp = client.get(f"/_test/render?plugin={PLUGIN_ID}&size={size}")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'data-plugin="{PLUGIN_ID}"' in body
    assert "S Julius-Leber-Br" in body
    assert '"line": "S1"' in body or '"line":"S1"' in body
    assert '"line": "M43"' in body or '"line":"M43"' in body
    # The cancelled S1 must not appear alongside the on-time one.
    assert body.count('"line": "S1"') + body.count('"line":"S1"') == 1


def test_no_departures_shows_friendly_empty_state(app: Flask, client: FlaskClient) -> None:
    _skip_if_not_loaded(app)

    with app.app_context():
        app.config["SETTINGS_STORE"].update_section(
            "plugins", {PLUGIN_ID: {"stop_id": "900057104"}}
        )

    with patch(
        "urllib.request.urlopen",
        side_effect=[_FakeResp(json.dumps({"departures": []}).encode("utf-8"))],
    ):
        resp = client.get(f"/_test/render?plugin={PLUGIN_ID}&size=md")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '"departures": []' in body or '"departures":[]' in body


def test_upstream_error_shows_friendly_message(app: Flask, client: FlaskClient) -> None:
    _skip_if_not_loaded(app)

    with app.app_context():
        app.config["SETTINGS_STORE"].update_section(
            "plugins", {PLUGIN_ID: {"stop_id": "bad-id"}}
        )

    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        resp = client.get(f"/_test/render?plugin={PLUGIN_ID}&size=md")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "OSError" in body or "boom" in body
